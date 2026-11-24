"""Agent API security and staging negatives (handoff §14, §18, §19):
bearer-token auth on every endpoint, command/result ownership and
idempotency, and the template vocabulary's path-safety refusals."""
import base64
import hashlib
import json
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from django.test import TestCase, override_settings
from django.urls import reverse

from agent import agent as workhorse_agent
from portal.apps.labs import services
from django.core.files.uploadedfile import SimpleUploadedFile
from portal.apps.testutils import complete, host_token, make_host, make_lab_challenge, make_user
from portal.apps.accounts.models import User
from portal.apps.labs.models import Host, HostCommand, LabInstance


class AgentApiAuthTests(TestCase):
    def setUp(self):
        self.host = make_host("auth-host")
        self.token = host_token(self.host)
        self.other = make_host("auth-host-2")
        self.other_token = host_token(self.other)

    def _post(self, name, token=None, body=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        return self.client.post(
            reverse(name), data=json.dumps(body or {}), content_type="application/json", **headers
        )

    def test_missing_and_invalid_tokens_are_rejected(self):
        for endpoint in ("agent-heartbeat",):
            self.assertEqual(self._post(endpoint).status_code, 401, endpoint)
            self.assertEqual(self._post(endpoint, token="wrong").status_code, 401, endpoint)
        # The poll endpoint is GET (the agent fetches its pending commands).
        self.assertEqual(self.client.get(reverse("agent-poll")).status_code, 401)
        self.assertEqual(
            self.client.get(reverse("agent-poll"), HTTP_AUTHORIZATION="Bearer wrong").status_code,
            401,
        )

    def test_result_requires_the_owning_host(self):
        """A host may only report results for its OWN commands."""
        user = make_user("500000000000000001", "Alex")
        challenge = make_lab_challenge()
        services.request_lab(user, challenge.id)
        lab = LabInstance.objects.get(user=user, challenge=challenge)
        services.claim(user, challenge.id)
        command = HostCommand.objects.get(lab=lab, status=HostCommand.Status.PENDING)

        url = reverse("agent-result", args=[command.op_id])
        wrong = self.client.post(url, data=json.dumps({"status": "ok", "result": {}}),
                                 content_type="application/json",
                                 HTTP_AUTHORIZATION=f"Bearer {self.other_token}")
        self.assertIn(wrong.status_code, (401, 403, 404))
        command.refresh_from_db()
        self.assertEqual(command.status, HostCommand.Status.PENDING)  # untouched

    def test_result_is_idempotent(self):
        user = make_user("500000000000000002", "Alex")
        challenge = make_lab_challenge()
        services.request_lab(user, challenge.id)
        services.claim(user, challenge.id)
        command = HostCommand.objects.filter(host=self.host, status=HostCommand.Status.PENDING).first()
        url = reverse("agent-result", args=[command.op_id])
        payload = {"status": "ok", "result": {"domains": ["a", "b"]}}
        first = self.client.post(url, data=json.dumps(payload), content_type="application/json",
                                 HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(first.status_code, 200)
        second = self.client.post(url, data=json.dumps(payload), content_type="application/json",
                                  HTTP_AUTHORIZATION=f"Bearer {self.token}")
        self.assertEqual(second.status_code, 200)  # idempotent replay


class TemplateStagingSecurityTests(TestCase):
    """The staging vocabulary must refuse everything that is not exactly a
    pinned (role, version, file) template chunk."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.executor = workhorse_agent.LibvirtExecutor(
            images_dir=Path(self._tmp.name) / "images",
            disks_dir=Path(self._tmp.name) / "disks",
        )

    def _put(self, *, role="target", version="v1", file="disk-template",
             seq=1, total=1, data=b"payload", sha=None, **overrides):
        payload = {
            "role": role, "version": version, "file": file,
            "seq": seq, "total": total,
            "data_b64": base64.b64encode(data).decode("ascii"),
            "sha256": sha or hashlib.sha256(data).hexdigest(),
        }
        payload.update(overrides)
        command = {"op_id": f"tpl-{uuid.uuid4().hex[:8]}", "operation": "template-put", "payload": payload}
        return workhorse_agent.execute(self.executor, command)

    def test_rejects_path_traversal_in_version_and_role(self):
        for bad in ("../evil", "..", "a/b", "", "/abs", "."):
            with self.assertRaises(workhorse_agent.AgentError, msg=bad):
                self._put(version=bad)
            with self.assertRaises(workhorse_agent.AgentError, msg=bad):
                self._put(role=bad)

    def test_rejects_unknown_roles_and_files(self):
        with self.assertRaises(workhorse_agent.AgentError):
            self._put(role="hypervisor")
        with self.assertRaises(workhorse_agent.AgentError):
            self._put(file="authorized_keys")
        with self.assertRaises(workhorse_agent.AgentError):
            self._put(file="../../etc/passwd")

    def test_rejects_bad_chunk_metadata(self):
        with self.assertRaises(workhorse_agent.AgentError):
            self._put(seq=0, total=1)
        with self.assertRaises(workhorse_agent.AgentError):
            self._put(seq=3, total=2)
        with self.assertRaises(workhorse_agent.AgentError):
            self._put(seq="one", total=1)
        with self.assertRaises(workhorse_agent.AgentError):
            self._put(data_b64="!!!not-base64!!!")
        with self.assertRaises(workhorse_agent.AgentError):
            self._put(sha="deadbeef")  # not 64 hex
        with self.assertRaises(workhorse_agent.AgentError):
            self._put(sha="A" * 64)   # uppercase hex refused

    def test_staging_never_leaves_parts_behind_on_failure(self):
        """A failed reassembly cleans its staging directory: no disk litter."""
        staging_root = self.executor.disks_dir / workhorse_agent.LibvirtExecutor.STAGING_DIRNAME
        good_sha = hashlib.sha256(b"payload").hexdigest()
        detail = self._put(seq=1, total=2, data=b"payload", sha=good_sha)
        self.assertFalse(detail["complete"])  # parts wait for the next chunk
        # A corrupted final chunk is refused and the staging dir is removed.
        with self.assertRaises(workhorse_agent.AgentError):
            self._put(seq=2, total=2, data=b"WRONG", sha=good_sha)
        self.assertFalse((staging_root / f"target.v1.disk-template.{good_sha[:12]}").exists())
        self.assertFalse((self.executor.images_dir / "target" / "v1").exists())

    def test_verify_missing_file_and_remove_paths(self):
        with self.assertRaises(workhorse_agent.AgentError):
            workhorse_agent.execute(self.executor, {
                "op_id": "v1", "operation": "template-verify",
                "payload": {"role": "target", "version": "ghost",
                            "sha256": hashlib.sha256(b"").hexdigest()},
            })
        # template-remove refuses to touch the bare role directory (demo mode).
        (self.executor.images_dir / "attacker").mkdir(parents=True)
        (self.executor.images_dir / "attacker" / "domain.xml").write_text("<domain/>")
        with self.assertRaises(workhorse_agent.AgentError):
            workhorse_agent.execute(self.executor, {
                "op_id": "rm-bare", "operation": "template-remove",
                "payload": {"role": "attacker", "version": ""},
            })
        self.assertTrue((self.executor.images_dir / "attacker" / "domain.xml").exists())

    def test_allocate_refuses_malformed_lab_ids_and_version_names(self):
        """allocate payload validation runs before any host interaction —
        the recording executor (no virsh) proves the refusals are payload-side."""
        from .test_agent import AgentExecutorTests

        with TemporaryDirectory() as tmp:
            executor = AgentExecutorTests._executor(AgentExecutorTests(), tmp)
            for bad_lab in (None, "zero", 0, -1):
                command = {"op_id": "alloc-bad", "operation": "allocate",
                           "payload": {"lab": bad_lab, "attacker_version": "", "target_version": ""}}
                with self.assertRaises(workhorse_agent.AgentError, msg=bad_lab):
                    workhorse_agent.execute(executor, command)
            for bad in ("../escape", "a/b", "."):
                command = {"op_id": "alloc-bad-v", "operation": "allocate",
                           "payload": {"lab": 1, "attacker_version": bad, "target_version": bad}}
                with self.assertRaises(workhorse_agent.AgentError, msg=bad):
                    workhorse_agent.execute(executor, command)
            # Empty versions are legitimate: demo-mode fallback to bare role dirs.
            for role in ("attacker", "target"):
                (executor.images_dir / role).mkdir(parents=True, exist_ok=True)
                (executor.images_dir / role / "domain.xml").write_text("<domain/>")
            command = {"op_id": "alloc-demo", "operation": "allocate",
                       "payload": {"lab": 1, "attacker_version": "", "target_version": ""}, "lab": 1}
            detail = workhorse_agent.execute(executor, command)
            self.assertEqual(detail["domains"], ["ctf-lab1-atk", "ctf-lab1-tgt"])


class AgentArtifactEndpointTests(TestCase):
    """The Path-A v2 transfer: agents pull staged files from the controller
    (token-authed streaming GET) and push captured builds back (chunked POST)."""

    def setUp(self):
        self.host = make_host("artifact-host")
        self.token = host_token(self.host)

    def test_template_artifact_requires_token(self):
        response = self.client.get(reverse("agent-template-artifact"),
                                   {"role": "target", "version": "v1", "file": "disk-template"})
        self.assertEqual(response.status_code, 401)

    def test_template_artifact_streams_domain_and_disk(self):
        from portal.apps.labs.models import MachineImage
        xml = ("<domain><name>@NAME@</name><disk>@DISK@</disk>"
               "<vnc>@VNC_PORT@</vnc><net>@LAB_NET@</net></domain>")
        image, _ = services.save_machine_image(
            make_user("510000000000000001", "Op", is_admin=True, status=User.Status.APPROVED),
            role="attacker", version="kali-stream", domain_xml=xml,
            disk=SimpleUploadedFile("d", b"QFI\xfb\x00\x03streamed-disk-bytes"))
        auth = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}
        response = self.client.get(reverse("agent-template-artifact"),
                                   {"role": "attacker", "version": "kali-stream", "file": "domain.xml"},
                                   **auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, xml.encode())
        response = self.client.get(reverse("agent-template-artifact"),
                                   {"role": "attacker", "version": "kali-stream", "file": "disk-template"},
                                   **auth)
        self.assertEqual(b"".join(response.streaming_content), b"QFI\xfb\x00\x03streamed-disk-bytes")
        # Host-local capture (no controller file) is refused, never invented.
        MachineImage.objects.create(role="target", version="local-only", domain_xml="<x/>")
        response = self.client.get(reverse("agent-template-artifact"),
                                   {"role": "target", "version": "local-only", "file": "disk-template"},
                                   **auth)
        self.assertEqual(response.status_code, 409)

    def test_build_artifact_upload_assembles_verifies_and_attaches(self):
        from portal.apps.labs.models import MachineBuild, MachineImage
        from portal.apps.testutils import make_lab_challenge
        admin = make_user("520000000000000001", "Op", is_admin=True, status=User.Status.APPROVED)
        challenge = make_lab_challenge()
        services.request_lab(admin, challenge.id)
        lab = LabInstance.objects.get(user=admin, challenge=challenge)
        build = MachineBuild.objects.create(host=self.host, role="attacker",
                                            version="kali-up", base_version="")
        import base64 as b64
        import hashlib as _h
        content = b"QFI\xfb\x00\x03captured-disk"
        sha = _h.sha256(content).hexdigest()
        auth = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}
        response = self.client.post(reverse("agent-build-artifact"), data=json.dumps({
            "build_id": build.id, "role": "attacker", "version": "kali-up",
            "file": "disk-template", "seq": 1, "total": 1, "sha256": sha,
            "size": len(content), "data_b64": b64.b64encode(content).decode(),
        }), content_type="application/json", **auth)
        self.assertEqual(response.status_code, 200)
        image = MachineImage.objects.get(role="attacker", version="kali-up")
        self.assertTrue(image.disk_file)
        self.assertEqual(image.disk_sha256, sha)
        # Tampered chunk is refused wholesale.
        bad = self.client.post(reverse("agent-build-artifact"), data=json.dumps({
            "build_id": build.id, "role": "attacker", "version": "kali-bad",
            "file": "disk-template", "seq": 1, "total": 1, "sha256": "0" * 64,
            "size": 4, "data_b64": b64.b64encode(b"QFI\xfbdata").decode(),
        }), content_type="application/json", **auth)
        self.assertEqual(bad.status_code, 409)
        self.assertFalse(MachineImage.objects.filter(version="kali-bad").exists())

    def test_build_artifact_endpoint_offsets_multi_chunk_and_requires_chunk_bytes(self):
        """Multi-chunk uploads write at (seq-1)·chunk_bytes; the endpoint
        refuses multi-chunk transfers without chunk_bytes (the controller
        cannot place chunks without it)."""
        from portal.apps.labs.models import MachineBuild, MachineImage
        build = MachineBuild.objects.create(host=self.host, role="attacker",
                                            version="kali-multi", base_version="")
        import base64 as b64
        import hashlib as _h
        content = b"QFI\xfb\x00\x03aa-bb-cc"  # 14 bytes → three 5-byte chunks (last: 4)
        chunk_bytes = 5
        sha = _h.sha256(content).hexdigest()
        auth = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}
        chunks = [content[0:5], content[5:10], content[10:14]]

        def post(seq, blob, **overrides):
            payload = {"build_id": build.id, "role": "attacker", "version": "kali-multi",
                       "file": "disk-template", "seq": seq, "total": 3, "sha256": sha,
                       "size": len(content), "chunk_bytes": chunk_bytes,
                       "data_b64": b64.b64encode(blob).decode()}
            payload.update(overrides)
            return self.client.post(reverse("agent-build-artifact"),
                                    data=json.dumps(payload),
                                    content_type="application/json", **auth)

        self.assertEqual(post(2, chunks[1]).json()["complete"], False)
        self.assertEqual(post(3, chunks[2]).json()["complete"], False)
        # Missing chunk_bytes on a multi-chunk transfer is a 400.
        self.assertEqual(post(1, chunks[0], chunk_bytes=None).status_code, 400)
        self.assertEqual(post(1, chunks[0]).json()["complete"], True)
        image = MachineImage.objects.get(role="attacker", version="kali-multi")
        image.disk_file.open("rb")
        self.assertEqual(image.disk_file.read(), content)
        image.disk_file.close()
        self.assertEqual(image.disk_file.name, "images/attacker-kali-multi-disk-template")

    def test_build_artifact_rejects_foreign_host_and_files(self):
        other = make_host("artifact-host-2")
        other_token = host_token(other)
        auth = {"HTTP_AUTHORIZATION": f"Bearer {other_token}"}
        response = self.client.post(reverse("agent-build-artifact"), data=json.dumps({
            "build_id": 99999, "role": "attacker", "version": "x",
            "file": "disk-template", "seq": 1, "total": 1, "sha256": "0" * 64,
            "size": 1, "data_b64": "aGk=",
        }), content_type="application/json", **auth)
        self.assertEqual(response.status_code, 404)
