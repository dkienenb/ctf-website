"""Agent API and restricted-executor tests (handoff §14, §18)."""
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from agent import agent as workhorse_agent  # repo-root agent package
from portal.apps.labs.models import Host, HostCommand, LabInstance, LabTemplate
from portal.apps.labs.services import ServiceError

from portal.apps.testutils import make_user
from portal.apps.testutils import make_host, make_lab_challenge


class AgentApiTests(TestCase):
    def setUp(self):
        self.host = make_host("agent-host")
        self.token = "agent-token-" + uuid.uuid4().hex
        from django.utils.crypto import get_random_string
        import hashlib as _h
        self.host.token_hash = _h.sha256(self.token.encode()).hexdigest()
        self.host.save()
        self.user = make_user("800000000000000001", "Alex")
        self.challenge = make_lab_challenge()
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.token}"}

    def _request_lab_and_claim(self):
        from portal.apps.labs import services

        services.request_lab(self.user, self.challenge.id)
        services.claim(self.user, self.challenge.id)
        return LabInstance.objects.get(user=self.user, challenge=self.challenge)

    def test_auth_required(self):
        response = self.client.get(reverse("agent-poll"))
        self.assertEqual(response.status_code, 401)
        response = self.client.get(reverse("agent-poll"), **{"HTTP_AUTHORIZATION": "Bearer wrong"})
        self.assertEqual(response.status_code, 401)
        other_host = make_host("other")
        other_host.token_hash = "0" * 64
        other_host.save()

    def test_poll_returns_own_pending_commands_only(self):
        lab = self._request_lab_and_claim()
        other_host = make_host("agent-host-2")
        other_token = "other-" + uuid.uuid4().hex
        import hashlib as _h
        other_host.token_hash = _h.sha256(other_token.encode()).hexdigest()
        other_host.save()
        response = self.client.get(reverse("agent-poll"), **self.auth)
        commands = json.loads(response.content)["commands"]
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]["operation"], "allocate")
        self.assertEqual(commands[0]["lab"], lab.id)
        other_response = self.client.get(reverse("agent-poll"), HTTP_AUTHORIZATION=f"Bearer {other_token}")
        self.assertEqual(json.loads(other_response.content)["commands"], [])

    def test_result_completes_command_and_transitions_lab(self):
        lab = self._request_lab_and_claim()
        command = HostCommand.objects.filter(lab=lab, operation="allocate").first()
        response = self.client.post(
            reverse("agent-result", args=[command.op_id]),
            data=json.dumps({"status": "ok", "result": {"domains": ["a", "b"]}}),
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        lab.refresh_from_db()
        self.assertEqual(lab.state, "running")
        command.refresh_from_db()
        self.assertEqual(command.status, HostCommand.Status.COMPLETED)
        # Idempotent replay.
        response = self.client.post(
            reverse("agent-result", args=[command.op_id]),
            data=json.dumps({"status": "ok", "result": {}}),
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(json.loads(response.content)["status"], "completed")

    def test_heartbeat_updates_capacity_and_reserve(self):
        response = self.client.post(
            reverse("agent-heartbeat"),
            data=json.dumps({
                "agent_version": "test-1",
                "free_disk_gb": 10, "total_disk_gb": 100,
                "free_ram_gb": 20, "total_ram_gb": 32,
                "running_labs": 1,
            }),
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.host.refresh_from_db()
        self.assertEqual(self.host.agent_version, "test-1")
        # 10/100 disk = 10% < 20% reserve: admissions stop (owner decision).
        self.assertFalse(self.host.admits)
        # The heartbeat returns acknowledged lease deadlines for local enforcement.
        self.assertIn("lease_deadlines", json.loads(response.content))

    def test_stale_heartbeat_blocks_admission_but_never_assumes_stop(self):
        self.host.last_heartbeat = timezone.now() - timezone.timedelta(seconds=300)
        self.host.save()
        self.assertTrue(self.host.heartbeat_stale)
        self.assertFalse(self.host.admits)


class AgentExecutorTests(TestCase):
    def _executor(self, tmp):
        class RecordingExecutor(workhorse_agent.LibvirtExecutor):
            def __init__(self, inner):
                self.calls = []
                self.inner = inner
                # Per-probe (returncode, stdout) responses; default (1, "") =
                # "not defined / not active", which drives the define+start paths.
                self.probes = {}

            def virsh(self, *args):
                self.calls.append(args)

            def virsh_tolerant(self, *args):
                self.calls.append(args)
                # A destroy/shutdown call models the guest leaving "running".

            def domain_state(self, name):
                destroyed = any(
                    call[0] in ("destroy", "shutdown") and len(call) > 1 and call[1] == name
                    for call in self.calls
                )
                if destroyed:
                    return "shut off"
                return "running" if name in getattr(self, "existing", set()) else "unknown"

            def virsh_probe(self, *args):
                self.calls.append(args)
                return self.probes.get(args[0], (1, ""))

            def domain_exists(self, name):
                return name in getattr(self, "existing", set())

            def _ensure_disk(self, source, template_disk, payload):
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(b"disk")

        executor = RecordingExecutor(None)
        executor.uri = "qemu://test"
        executor.images_dir = Path(tmp) / "images"
        executor.disks_dir = Path(tmp) / "disks"
        executor.disks_dir.mkdir(parents=True)
        return executor

    def test_build_lifecycle_ops(self):
        """build-start boots a scratch guest on the build network with the
        base template; build-discard tears it down without capturing."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            for role in ("attacker", "target"):
                (executor.images_dir / role).mkdir(parents=True)
                (executor.images_dir / role / "domain.xml").write_text(
                    "<domain><name>@NAME@</name><disk>@DISK@</disk>"
                    "<vnc>@VNC_PORT@</vnc><net>@LAB_NET@</net></domain>")
                (executor.images_dir / role / "disk-template").write_bytes(b"base")
            detail = workhorse_agent.execute(executor, {
                "op_id": "b1", "operation": "build-start",
                "payload": {"role": "attacker", "version": "kali-v2", "base_version": ""},
            })
            self.assertEqual(detail["vnc_port"], "6911")  # outside the lab port range
            defines = [call for call in executor.calls if call[0] == "define"]
            xml = (executor.disks_dir / "ctf-build-attacker.xml").read_text()
            self.assertIn("ctf-build-attacker", xml)
            self.assertIn("ctf-build-net", xml)
            self.assertIn("6911", xml)
            # A second start while the slot is busy is refused.
            executor.existing = {"ctf-build-attacker"}
            with self.assertRaises(workhorse_agent.AgentError) as error:
                workhorse_agent.execute(executor, {
                    "op_id": "b2", "operation": "build-start",
                    "payload": {"role": "attacker", "version": "kali-v3", "base_version": ""}})
            self.assertIn("build slot busy", str(error.exception))
            # Discard destroys the guest and its scratch disk.
            (executor.disks_dir / "ctf-build-attacker.qcow2").write_bytes(b"d")
            detail = workhorse_agent.execute(executor, {
                "op_id": "b3", "operation": "build-discard", "payload": {"role": "attacker"}})
            self.assertEqual(detail["discarded"], "ctf-build-attacker")
            self.assertFalse((executor.disks_dir / "ctf-build-attacker.qcow2").exists())

    def test_build_exec_requires_running_guest_and_script(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            with self.assertRaises(workhorse_agent.AgentError):
                workhorse_agent.execute(executor, {
                    "op_id": "e1", "operation": "build-exec",
                    "payload": {"role": "attacker", "script": "ls"}})
            executor.existing = set()
            executor.domain_state = lambda name: "running"
            executor._build_guest_ip = lambda: "10.250.0.10"
            with self.assertRaises(workhorse_agent.AgentError):
                workhorse_agent.execute(executor, {
                    "op_id": "e2", "operation": "build-exec",
                    "payload": {"role": "attacker", "script": "   "}})
            captured = {}
            executor._run_ssh = lambda ip, user, pw, script, timeout=300: (
                captured.update(ip=ip, user=user, script=script) or (0, "done", ""))
            detail = workhorse_agent.execute(executor, {
                "op_id": "e3", "operation": "build-exec",
                "payload": {"role": "attacker", "script": "apt-get install -y htop",
                            "ssh_user": "root"}})
            self.assertEqual(detail["exit_code"], 0)
            self.assertEqual(captured["ip"], "10.250.0.10")
            self.assertEqual(captured["user"], "root")
            self.assertIn("apt-get", captured["script"])

    def test_build_capture_writes_template_and_reports_sha(self):
        import hashlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            (executor.images_dir / "attacker").mkdir(parents=True)
            (executor.images_dir / "attacker" / "domain.xml").write_text(
                "<domain><name>@NAME@</name></domain>")
            executor.existing = {"ctf-build-attacker"}
            executor.domain_state = lambda name: "shut off"
            executor._compact_disk = lambda src, dst: dst.write_bytes(b"customized-disk")
            (executor.disks_dir / "ctf-build-attacker.qcow2").write_bytes(b"dirty")
            detail = workhorse_agent.execute(executor, {
                "op_id": "c1", "operation": "build-capture",
                "payload": {"role": "attacker", "version": "kali-v9", "base_version": ""}})
            self.assertEqual(detail["captured"], "attacker/kali-v9")
            self.assertEqual(detail["sha256"], hashlib.sha256(b"customized-disk").hexdigest())
            # Placeholders survive the capture: the template stays generic (§10).
            xml = (executor.images_dir / "attacker" / "kali-v9" / "domain.xml").read_text()
            self.assertIn("@NAME@", xml)
            self.assertEqual((executor.images_dir / "attacker" / "kali-v9" / "disk-template").read_bytes(),
                             b"customized-disk")
            # Capturing an already-existing version is refused.
            executor.existing = {"ctf-build-attacker"}
            (executor.disks_dir / "ctf-build-attacker.qcow2").write_bytes(b"dirty-again")
            with self.assertRaises(workhorse_agent.AgentError) as error:
                workhorse_agent.execute(executor, {
                    "op_id": "c2", "operation": "build-capture",
                    "payload": {"role": "attacker", "version": "kali-v9", "base_version": ""}})
            self.assertIn("already exists", str(error.exception))

    def test_vocabulary_remains_restricted_and_closed(self):
        with self.assertRaises(workhorse_agent.AgentError):
            workhorse_agent.execute(None, {"operation": "shell", "payload": {"cmd": "rm -rf /"}})
        with self.assertRaises(workhorse_agent.AgentError):
            workhorse_agent.execute(None, {"operation": ""})

    def test_allocate_requires_pinned_templates(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            command = {"op_id": "abc123", "operation": "allocate", "payload": {"lab": 1}}
            with self.assertRaises(workhorse_agent.AgentError) as error:
                workhorse_agent.execute(executor, command)
            self.assertIn("missing pinned template", str(error.exception))

    def test_allocate_defines_and_starts_pair_from_templates(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            for role in ("attacker", "target"):
                directory = executor.images_dir / role
                directory.mkdir(parents=True)
                (directory / "domain.xml").write_text("<domain><name>@NAME@</name><disk>@DISK@</disk></domain>")
            command = {"op_id": "abc123def456", "operation": "allocate", "payload": {"lab": 1}, "lab": 1}
            detail = workhorse_agent.execute(executor, command)
            self.assertFalse(detail["resumed"])
            self.assertEqual(detail["domains"], ["ctf-lab1-atk", "ctf-lab1-tgt"])
            defines = [call for call in executor.calls if call[0] == "define"]
            starts = [call for call in executor.calls if call[0] == "start"]
            self.assertEqual(len(defines), 2)
            self.assertEqual(len(starts), 2)
            xml = (executor.disks_dir / "ctf-lab1-atk.xml").read_text()
            self.assertIn("ctf-lab1-atk", xml)
            self.assertIn("disk", xml)

    def test_save_saves_then_undefines(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            executor.existing = {"ctf-abc123def456-atk", "ctf-abc123def456-tgt"}
            command = {"op_id": "abc123def456", "operation": "save", "payload": {}}
            detail = workhorse_agent.execute(executor, command)
            saves = [call for call in executor.calls if call[0] == "save"]
            self.assertEqual(len(saves), 2)
            self.assertIn("saved_dir", detail)

    def test_reset_scopes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            workhorse_agent.execute(executor, {"op_id": "abc123def456", "operation": "reset-target", "payload": {}, "lab": 1})
            self.assertTrue(all("-tgt" in call[1] for call in executor.calls if call[0] == "destroy"))
            executor.calls.clear()
            workhorse_agent.execute(executor, {"op_id": "abc123def456", "operation": "reset-full", "payload": {}, "lab": 1})
            destroyed = [call[1] for call in executor.calls if call[0] == "destroy"]
            self.assertTrue(any("atk" in name for name in destroyed))
            self.assertTrue(any("tgt" in name for name in destroyed))

    def test_allocate_uses_pinned_versions(self):
        """Versions in the allocate payload select images_dir/<role>/<version>."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            for role, version in (("attacker", "kali-v9"), ("target", "target-v2")):
                directory = executor.images_dir / role / version
                directory.mkdir(parents=True)
                (directory / "domain.xml").write_text("<domain><name>@NAME@</name><disk>@DISK@</disk><vnc>@VNC_PORT@</vnc><net>@LAB_NET@</net></domain>")
            command = {"op_id": "abc123def456", "operation": "allocate",
                       "payload": {"lab": 3, "attacker_version": "kali-v9", "target_version": "target-v2"}, "lab": 3}
            detail = workhorse_agent.execute(executor, command)
            self.assertEqual(detail["domains"], ["ctf-lab3-atk", "ctf-lab3-tgt"])
            defines = [call for call in executor.calls if call[0] == "define"]
            self.assertEqual(len(defines), 2)
            xml = (executor.disks_dir / "ctf-lab3-atk.xml").read_text()
            self.assertIn("ctf-lab3-net", xml)  # @LAB_NET@ substituted
            # Missing pinned version fails explicitly (§13: no silent substitution).
            command["payload"]["target_version"] = "absent-version"
            with self.assertRaises(workhorse_agent.AgentError) as error:
                workhorse_agent.execute(executor, command)
            self.assertIn("missing pinned template", str(error.exception))

    def test_template_put_chunks_assemble_and_verify(self):
        import base64
        import hashlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            content = b"x" * 5000 + b"y" * 3000
            sha = hashlib.sha256(content).hexdigest()
            chunks = [content[i:i + 4096] for i in range(0, len(content), 4096)]
            for seq, chunk in enumerate(chunks, start=1):
                detail = workhorse_agent.execute(executor, {
                    "op_id": f"tpl-{seq}", "operation": "template-put",
                    "payload": {"role": "target", "version": "v1", "file": "disk-template",
                                "seq": seq, "total": len(chunks),
                                "data_b64": base64.b64encode(chunk).decode("ascii"), "sha256": sha},
                })
                self.assertFalse(detail["complete"]) if seq < len(chunks) else self.assertTrue(detail["complete"])
            disk = executor.images_dir / "target" / "v1" / "disk-template"
            self.assertEqual(disk.read_bytes(), content)

            # Corrupt reassembly is refused; bad names are refused for path safety.
            with self.assertRaises(workhorse_agent.AgentError):
                workhorse_agent.execute(executor, {
                    "op_id": "tpl-verify", "operation": "template-verify",
                    "payload": {"role": "target", "version": "v1",
                                "sha256": "0" * 64, "size": len(content)},
                })
            with self.assertRaises(workhorse_agent.AgentError):
                workhorse_agent.execute(executor, {
                    "op_id": "tpl-evil", "operation": "template-put",
                    "payload": {"role": "target", "version": "../evil", "file": "domain.xml",
                                "seq": 1, "total": 1, "data_b64": "aGVsbG8=", "sha256": sha},
                })
            with self.assertRaises(workhorse_agent.AgentError):
                workhorse_agent.execute(executor, {
                    "op_id": "tpl-file", "operation": "template-put",
                    "payload": {"role": "target", "version": "v1", "file": "authorized_keys",
                                "seq": 1, "total": 1, "data_b64": "aGVsbG8=", "sha256": sha},
                })

    def test_template_verify_checks_both_files(self):
        """The verify step proves BOTH staged files against their own hashes;
        a missing or tampered domain.xml refuses, not just the disk."""
        import base64
        import hashlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            disk_content, xml_content = b"disk-bytes", b"<domain>@NAME@</domain>"
            disk_sha = hashlib.sha256(disk_content).hexdigest()
            xml_sha = hashlib.sha256(xml_content).hexdigest()
            for file_name, content, sha in (
                ("disk-template", disk_content, disk_sha),
                ("domain.xml", xml_content, xml_sha),
            ):
                workhorse_agent.execute(executor, {
                    "op_id": f"tv-{file_name}", "operation": "template-put",
                    "payload": {"role": "target", "version": "v9", "file": file_name,
                                "seq": 1, "total": 1,
                                "data_b64": base64.b64encode(content).decode("ascii"),
                                "sha256": sha},
                })
            payload = {"role": "target", "version": "v9", "sha256": disk_sha,
                       "size": len(disk_content), "xml_sha256": xml_sha}
            detail = workhorse_agent.execute(executor, {
                "op_id": "tv-ok", "operation": "template-verify", "payload": payload})
            self.assertEqual(detail["verified"], "target/v9")
            # Tampered xml refused.
            bad = dict(payload, xml_sha256="0" * 64)
            with self.assertRaises(workhorse_agent.AgentError):
                workhorse_agent.execute(executor, {"op_id": "tv-bad", "operation": "template-verify", "payload": bad})
            # Missing xml refused.
            (executor.images_dir / "target" / "v9" / "domain.xml").unlink()
            with self.assertRaises(workhorse_agent.AgentError):
                workhorse_agent.execute(executor, {"op_id": "tv-missing", "operation": "template-verify", "payload": payload})

    def test_template_list_and_remove(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            version_dir = executor.images_dir / "attacker" / "kali-v1"
            version_dir.mkdir(parents=True)
            (version_dir / "domain.xml").write_text("<domain>")
            (version_dir / "disk-template").write_bytes(b"disk")
            inventory = workhorse_agent.execute(executor, {
                "op_id": "tpl-list", "operation": "template-list", "payload": {},
            })
            self.assertEqual(inventory["attacker"]["kali-v1"], {"domain.xml": 8, "disk-template": 4})
            detail = workhorse_agent.execute(executor, {
                "op_id": "tpl-rm", "operation": "template-remove",
                "payload": {"role": "attacker", "version": "kali-v1"},
            })
            self.assertEqual(detail["removed"], "attacker/kali-v1")
            self.assertFalse(version_dir.exists())

    def test_save_is_idempotent_under_the_dual_expiry_race(self):
        """The controller's durable save and the agent's own host-local
        deadline enforcement can both fire at lease expiry (§11 + §18).
        Whichever runs second finds the domains already saved and undefined:
        that is a successful no-op, never a recovery."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            command = {"op_id": "save-2", "operation": "save", "payload": {}, "lab": 1}
            # Second runner: domains gone, but the first runner's artifacts exist.
            saved_dir = executor.disks_dir / "saved-lab1"
            saved_dir.mkdir(parents=True)
            (saved_dir / "attacker.save").write_bytes(b"a")
            (saved_dir / "target.save").write_bytes(b"t")
            detail = workhorse_agent.execute(executor, command)
            self.assertEqual(detail["already_saved"], ["ctf-lab1-atk", "ctf-lab1-tgt"])
            self.assertFalse([call for call in executor.calls if call[0] == "save"])
            # A missing domain with NO prior save is still a hard error.
            (saved_dir / "attacker.save").unlink()
            with self.assertRaises(workhorse_agent.AgentError) as error:
                workhorse_agent.execute(executor, command)
            self.assertIn("no prior save exists", str(error.exception))

    def test_same_lab_domain_identity_across_operations(self):
        """Allocate, save, and reset must address the SAME domain names (§12)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            for role in ("attacker", "target"):
                directory = executor.images_dir / role
                directory.mkdir(parents=True)
                (directory / "domain.xml").write_text("<domain><name>@NAME@</name><disk>@DISK@</disk></domain>")
            lab_command = {"op_id": "op-allocate", "operation": "allocate", "payload": {"lab": 7}, "lab": 7}
            detail = workhorse_agent.execute(executor, lab_command)
            self.assertEqual(detail["domains"], ["ctf-lab7-atk", "ctf-lab7-tgt"])
            # After a completed allocate the guests exist on the host.
            executor.existing = {"ctf-lab7-atk", "ctf-lab7-tgt"}
            save_command = {"op_id": "op-save-different-id", "operation": "save", "payload": {}, "lab": 7}
            workhorse_agent.execute(executor, save_command)
            saves = [call[1] for call in executor.calls if call[0] == "save"]
            self.assertEqual(sorted(saves), ["ctf-lab7-atk", "ctf-lab7-tgt"])

    def test_allocate_reuses_preserved_attacker_and_rebuilds_target(self):
        """After a target-scoped recovery, a running attacker VM is preserved
        (§13) and only the target is rebuilt (no direct target access)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            executor.existing = set()
            executor.domain_state = lambda name: "running" if name in executor.existing else "unknown"
            for role in ("attacker", "target"):
                directory = executor.images_dir / role
                directory.mkdir(parents=True)
                (directory / "domain.xml").write_text("<domain><name>@NAME@</name><disk>@DISK@</disk></domain>")
            executor.existing = {"ctf-lab9-atk"}
            executor.vnc_port_of = lambda name: "5900" if name in executor.existing else "unknown"
            # dumpxml must report the attacker as already attached to lab 9's
            # per-lab network, so the §13 preserve branch applies.
            executor.probes["dumpxml"] = (
                0, '<domain>\n  <source network="ctf-lab9-net"/>\n</domain>',
            )
            command = {"op_id": "op-x", "operation": "allocate", "payload": {"lab": 9}, "lab": 9}
            detail = workhorse_agent.execute(executor, command)
            self.assertFalse(detail["resumed"])
            defines = [call[1] for call in executor.calls if call[0] == "define"]
            self.assertFalse(any("ctf-lab9-atk" in path for path in defines))  # attacker preserved, not redefined
            self.assertTrue(any("ctf-lab9-tgt" in path for path in defines))   # target rebuilt
            started = [call[1] for call in executor.calls if call[0] == "start"]
            self.assertNotIn("ctf-lab9-atk", started)  # already running
    def test_build_guest_ip_parses_virsh_lease_layout(self):
        """Regression: virsh net-dhcp-leases puts Expiry in the second
        column — the address appears later. Matched by VALUE now."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            lease_output = (
                " Expiry time   MAC address        Protocol  IP address   Hostname  Client ID/Hostname\n"
                "-------------------------------------------------------------------------------------\n"
                " 2026-11-19 10:00:00  52:54:00:ab:cd:ef  ipv4   10.250.0.17/24  build-guest  -\n"
            )
            executor.probes["net-dhcp-leases"] = (0, lease_output)
            self.assertEqual(executor._build_guest_ip(), "10.250.0.17")

    def test_build_exec_maps_ssh_transport_failure(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            executor.domain_state = lambda name: "running"
            executor._build_guest_ip = lambda: "10.250.0.10"
            executor._run_ssh = lambda *a, **k: (255, "", "Connection refused")
            with self.assertRaises(workhorse_agent.AgentError) as error:
                workhorse_agent.execute(executor, {
                    "op_id": "e9", "operation": "build-exec",
                    "payload": {"role": "attacker", "script": "ls", "ssh_user": "root"}})
            self.assertIn("SSH connection failed", str(error.exception))
            # A real script result passes through with script_ok.
            executor._run_ssh = lambda *a, **k: (3, "partial", "")
            detail = workhorse_agent.execute(executor, {
                "op_id": "e10", "operation": "build-exec",
                "payload": {"role": "attacker", "script": "exit 3", "ssh_user": "root"}})
            self.assertEqual(detail["exit_code"], 3)
            self.assertFalse(detail["script_ok"])

    def test_build_start_injects_build_console_for_targets(self):
        """Target templates have no VNC; the BUILD guest gets one injected so
        the operator can watch provisioning. Captured XML keeps the base."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            (executor.images_dir / "target").mkdir(parents=True)
            (executor.images_dir / "target" / "domain.xml").write_text(
                "<domain><name>@NAME@</name><disk>@DISK@</disk><net>@LAB_NET@</net>"
                "<devices></devices></domain>")
            (executor.images_dir / "target" / "disk-template").write_bytes(b"base")
            workhorse_agent.execute(executor, {
                "op_id": "bt", "operation": "build-start",
                "payload": {"role": "target", "version": "tgt-v1", "base_version": ""}})
            xml = (executor.disks_dir / "ctf-build-target.xml").read_text()
            self.assertIn("<graphics type='vnc' port='6911'", xml)  # injected

    def test_capture_compresses_the_template(self):
        """Owner feedback: save space on VM templates — capture converts with
        compressed qcow2 clusters (qemu decompresses transparently)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            src_path = executor.disks_dir / "compress-src.qcow2"
            dst_path = executor.disks_dir / "compress-dst.qcow2"
            src_path.write_bytes(b"QFI\xfb\x00\x03src")
            real = workhorse_agent.LibvirtExecutor._compact_disk
            with mock.patch.object(workhorse_agent.subprocess, "run") as run_mock:
                run_mock.return_value = SimpleNamespace(returncode=0)
                real(executor, src_path, dst_path)
            args = run_mock.call_args[0][0]
            self.assertIn("-c", args)  # compression flag (owner feedback)
            self.assertEqual(args[args.index("-O") + 1], "qcow2")

    def test_vocabulary_remains_restricted_and_closed(self):
        with self.assertRaises(workhorse_agent.AgentError):
            workhorse_agent.execute(None, {"operation": "shell", "payload": {"cmd": "rm -rf /"}})
        with self.assertRaises(workhorse_agent.AgentError):
            workhorse_agent.execute(None, {"operation": ""})

    def test_allocate_requires_pinned_templates(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            command = {"op_id": "abc123", "operation": "allocate", "payload": {"lab": 1}}
            with self.assertRaises(workhorse_agent.AgentError) as error:
                workhorse_agent.execute(executor, command)
            self.assertIn("missing pinned template", str(error.exception))

    def test_allocate_defines_and_starts_pair_from_templates(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            for role in ("attacker", "target"):
                directory = executor.images_dir / role
                directory.mkdir(parents=True)
                (directory / "domain.xml").write_text("<domain><name>@NAME@</name><disk>@DISK@</disk></domain>")
            command = {"op_id": "abc123def456", "operation": "allocate", "payload": {"lab": 1}, "lab": 1}
            detail = workhorse_agent.execute(executor, command)
            self.assertFalse(detail["resumed"])
            self.assertEqual(detail["domains"], ["ctf-lab1-atk", "ctf-lab1-tgt"])
            defines = [call for call in executor.calls if call[0] == "define"]
            starts = [call for call in executor.calls if call[0] == "start"]
            self.assertEqual(len(defines), 2)
            self.assertEqual(len(starts), 2)
            xml = (executor.disks_dir / "ctf-lab1-atk.xml").read_text()
            self.assertIn("ctf-lab1-atk", xml)
            self.assertIn("disk", xml)

    def test_save_saves_then_undefines(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            executor.existing = {"ctf-abc123def456-atk", "ctf-abc123def456-tgt"}
            command = {"op_id": "abc123def456", "operation": "save", "payload": {}}
            detail = workhorse_agent.execute(executor, command)
            saves = [call for call in executor.calls if call[0] == "save"]
            self.assertEqual(len(saves), 2)
            self.assertIn("saved_dir", detail)

    def test_reset_scopes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            workhorse_agent.execute(executor, {"op_id": "abc123def456", "operation": "reset-target", "payload": {}, "lab": 1})
            self.assertTrue(all("-tgt" in call[1] for call in executor.calls if call[0] == "destroy"))
            executor.calls.clear()
            workhorse_agent.execute(executor, {"op_id": "abc123def456", "operation": "reset-full", "payload": {}, "lab": 1})
            destroyed = [call[1] for call in executor.calls if call[0] == "destroy"]
            self.assertTrue(any("atk" in name for name in destroyed))
            self.assertTrue(any("tgt" in name for name in destroyed))

    def test_allocate_uses_pinned_versions(self):
        """Versions in the allocate payload select images_dir/<role>/<version>."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            for role, version in (("attacker", "kali-v9"), ("target", "target-v2")):
                directory = executor.images_dir / role / version
                directory.mkdir(parents=True)
                (directory / "domain.xml").write_text("<domain><name>@NAME@</name><disk>@DISK@</disk><vnc>@VNC_PORT@</vnc><net>@LAB_NET@</net></domain>")
            command = {"op_id": "abc123def456", "operation": "allocate",
                       "payload": {"lab": 3, "attacker_version": "kali-v9", "target_version": "target-v2"}, "lab": 3}
            detail = workhorse_agent.execute(executor, command)
            self.assertEqual(detail["domains"], ["ctf-lab3-atk", "ctf-lab3-tgt"])
            defines = [call for call in executor.calls if call[0] == "define"]
            self.assertEqual(len(defines), 2)
            xml = (executor.disks_dir / "ctf-lab3-atk.xml").read_text()
            self.assertIn("ctf-lab3-net", xml)  # @LAB_NET@ substituted
            # Missing pinned version fails explicitly (§13: no silent substitution).
            command["payload"]["target_version"] = "absent-version"
            with self.assertRaises(workhorse_agent.AgentError) as error:
                workhorse_agent.execute(executor, command)
            self.assertIn("missing pinned template", str(error.exception))

    def test_template_put_chunks_assemble_and_verify(self):
        import base64
        import hashlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            content = b"x" * 5000 + b"y" * 3000
            sha = hashlib.sha256(content).hexdigest()
            chunks = [content[i:i + 4096] for i in range(0, len(content), 4096)]
            for seq, chunk in enumerate(chunks, start=1):
                detail = workhorse_agent.execute(executor, {
                    "op_id": f"tpl-{seq}", "operation": "template-put",
                    "payload": {"role": "target", "version": "v1", "file": "disk-template",
                                "seq": seq, "total": len(chunks),
                                "data_b64": base64.b64encode(chunk).decode("ascii"), "sha256": sha},
                })
                self.assertFalse(detail["complete"]) if seq < len(chunks) else self.assertTrue(detail["complete"])
            disk = executor.images_dir / "target" / "v1" / "disk-template"
            self.assertEqual(disk.read_bytes(), content)

            # Corrupt reassembly is refused; bad names are refused for path safety.
            with self.assertRaises(workhorse_agent.AgentError):
                workhorse_agent.execute(executor, {
                    "op_id": "tpl-verify", "operation": "template-verify",
                    "payload": {"role": "target", "version": "v1",
                                "sha256": "0" * 64, "size": len(content)},
                })
            with self.assertRaises(workhorse_agent.AgentError):
                workhorse_agent.execute(executor, {
                    "op_id": "tpl-evil", "operation": "template-put",
                    "payload": {"role": "target", "version": "../evil", "file": "domain.xml",
                                "seq": 1, "total": 1, "data_b64": "aGVsbG8=", "sha256": sha},
                })
            with self.assertRaises(workhorse_agent.AgentError):
                workhorse_agent.execute(executor, {
                    "op_id": "tpl-file", "operation": "template-put",
                    "payload": {"role": "target", "version": "v1", "file": "authorized_keys",
                                "seq": 1, "total": 1, "data_b64": "aGVsbG8=", "sha256": sha},
                })

    def test_template_verify_checks_both_files(self):
        """The verify step proves BOTH staged files against their own hashes;
        a missing or tampered domain.xml refuses, not just the disk."""
        import base64
        import hashlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            disk_content, xml_content = b"disk-bytes", b"<domain>@NAME@</domain>"
            disk_sha = hashlib.sha256(disk_content).hexdigest()
            xml_sha = hashlib.sha256(xml_content).hexdigest()
            for file_name, content, sha in (
                ("disk-template", disk_content, disk_sha),
                ("domain.xml", xml_content, xml_sha),
            ):
                workhorse_agent.execute(executor, {
                    "op_id": f"tv-{file_name}", "operation": "template-put",
                    "payload": {"role": "target", "version": "v9", "file": file_name,
                                "seq": 1, "total": 1,
                                "data_b64": base64.b64encode(content).decode("ascii"),
                                "sha256": sha},
                })
            payload = {"role": "target", "version": "v9", "sha256": disk_sha,
                       "size": len(disk_content), "xml_sha256": xml_sha}
            detail = workhorse_agent.execute(executor, {
                "op_id": "tv-ok", "operation": "template-verify", "payload": payload})
            self.assertEqual(detail["verified"], "target/v9")
            # Tampered xml refused.
            bad = dict(payload, xml_sha256="0" * 64)
            with self.assertRaises(workhorse_agent.AgentError):
                workhorse_agent.execute(executor, {"op_id": "tv-bad", "operation": "template-verify", "payload": bad})
            # Missing xml refused.
            (executor.images_dir / "target" / "v9" / "domain.xml").unlink()
            with self.assertRaises(workhorse_agent.AgentError):
                workhorse_agent.execute(executor, {"op_id": "tv-missing", "operation": "template-verify", "payload": payload})

    def test_template_list_and_remove(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            version_dir = executor.images_dir / "attacker" / "kali-v1"
            version_dir.mkdir(parents=True)
            (version_dir / "domain.xml").write_text("<domain>")
            (version_dir / "disk-template").write_bytes(b"disk")
            inventory = workhorse_agent.execute(executor, {
                "op_id": "tpl-list", "operation": "template-list", "payload": {},
            })
            self.assertEqual(inventory["attacker"]["kali-v1"], {"domain.xml": 8, "disk-template": 4})
            detail = workhorse_agent.execute(executor, {
                "op_id": "tpl-rm", "operation": "template-remove",
                "payload": {"role": "attacker", "version": "kali-v1"},
            })
            self.assertEqual(detail["removed"], "attacker/kali-v1")
            self.assertFalse(version_dir.exists())

    def test_save_is_idempotent_under_the_dual_expiry_race(self):
        """The controller's durable save and the agent's own host-local
        deadline enforcement can both fire at lease expiry (§11 + §18).
        Whichever runs second finds the domains already saved and undefined:
        that is a successful no-op, never a recovery."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            command = {"op_id": "save-2", "operation": "save", "payload": {}, "lab": 1}
            # Second runner: domains gone, but the first runner's artifacts exist.
            saved_dir = executor.disks_dir / "saved-lab1"
            saved_dir.mkdir(parents=True)
            (saved_dir / "attacker.save").write_bytes(b"a")
            (saved_dir / "target.save").write_bytes(b"t")
            detail = workhorse_agent.execute(executor, command)
            self.assertEqual(detail["already_saved"], ["ctf-lab1-atk", "ctf-lab1-tgt"])
            self.assertFalse([call for call in executor.calls if call[0] == "save"])
            # A missing domain with NO prior save is still a hard error.
            (saved_dir / "attacker.save").unlink()
            with self.assertRaises(workhorse_agent.AgentError) as error:
                workhorse_agent.execute(executor, command)
            self.assertIn("no prior save exists", str(error.exception))

    def test_same_lab_domain_identity_across_operations(self):
        """Allocate, save, and reset must address the SAME domain names (§12)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            for role in ("attacker", "target"):
                directory = executor.images_dir / role
                directory.mkdir(parents=True)
                (directory / "domain.xml").write_text("<domain><name>@NAME@</name><disk>@DISK@</disk></domain>")
            lab_command = {"op_id": "op-allocate", "operation": "allocate", "payload": {"lab": 7}, "lab": 7}
            detail = workhorse_agent.execute(executor, lab_command)
            self.assertEqual(detail["domains"], ["ctf-lab7-atk", "ctf-lab7-tgt"])
            # After a completed allocate the guests exist on the host.
            executor.existing = {"ctf-lab7-atk", "ctf-lab7-tgt"}
            save_command = {"op_id": "op-save-different-id", "operation": "save", "payload": {}, "lab": 7}
            workhorse_agent.execute(executor, save_command)
            saves = [call[1] for call in executor.calls if call[0] == "save"]
            self.assertEqual(sorted(saves), ["ctf-lab7-atk", "ctf-lab7-tgt"])

    def test_allocate_reuses_preserved_attacker_and_rebuilds_target(self):
        """After a target-scoped recovery, a running attacker VM is preserved
        (§13) and only the target is rebuilt (no direct target access)."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            executor.existing = set()
            executor.domain_state = lambda name: "running" if name in executor.existing else "unknown"
            for role in ("attacker", "target"):
                directory = executor.images_dir / role
                directory.mkdir(parents=True)
                (directory / "domain.xml").write_text("<domain><name>@NAME@</name><disk>@DISK@</disk></domain>")
            executor.existing = {"ctf-lab9-atk"}
            executor.vnc_port_of = lambda name: "5900" if name in executor.existing else "unknown"
            # dumpxml must report the attacker as already attached to lab 9's
            # per-lab network, so the §13 preserve branch applies.
            executor.probes["dumpxml"] = (
                0, '<domain>\n  <source network="ctf-lab9-net"/>\n</domain>',
            )
            command = {"op_id": "op-x", "operation": "allocate", "payload": {"lab": 9}, "lab": 9}
            detail = workhorse_agent.execute(executor, command)
            self.assertFalse(detail["resumed"])
            defines = [call[1] for call in executor.calls if call[0] == "define"]
            self.assertFalse(any("ctf-lab9-atk" in path for path in defines))  # attacker preserved, not redefined
            self.assertTrue(any("ctf-lab9-tgt" in path for path in defines))   # target rebuilt
            started = [call[1] for call in executor.calls if call[0] == "start"]
            self.assertNotIn("ctf-lab9-atk", started)  # already running
    def test_build_guest_ip_parses_virsh_lease_layout(self):
        """Regression: virsh net-dhcp-leases puts Expiry in the second
        column — the address appears later. Matched by VALUE now."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            lease_output = (
                " Expiry time   MAC address        Protocol  IP address   Hostname  Client ID/Hostname\n"
                "-------------------------------------------------------------------------------------\n"
                " 2026-11-19 10:00:00  52:54:00:ab:cd:ef  ipv4   10.250.0.17/24  build-guest  -\n"
            )
            executor.probes["net-dhcp-leases"] = (0, lease_output)
            self.assertEqual(executor._build_guest_ip(), "10.250.0.17")

    def test_build_exec_maps_ssh_transport_failure(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            executor.domain_state = lambda name: "running"
            executor._build_guest_ip = lambda: "10.250.0.10"
            executor._run_ssh = lambda *a, **k: (255, "", "Connection refused")
            with self.assertRaises(workhorse_agent.AgentError) as error:
                workhorse_agent.execute(executor, {
                    "op_id": "e9", "operation": "build-exec",
                    "payload": {"role": "attacker", "script": "ls", "ssh_user": "root"}})
            self.assertIn("SSH connection failed", str(error.exception))
            # A real script result passes through with script_ok.
            executor._run_ssh = lambda *a, **k: (3, "partial", "")
            detail = workhorse_agent.execute(executor, {
                "op_id": "e10", "operation": "build-exec",
                "payload": {"role": "attacker", "script": "exit 3", "ssh_user": "root"}})
            self.assertEqual(detail["exit_code"], 3)
            self.assertFalse(detail["script_ok"])

    def test_build_start_injects_build_console_for_targets(self):
        """Target templates have no VNC; the BUILD guest gets one injected so
        the operator can watch provisioning. Captured XML keeps the base."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            executor = self._executor(tmp)
            (executor.images_dir / "target").mkdir(parents=True)
            (executor.images_dir / "target" / "domain.xml").write_text(
                "<domain><name>@NAME@</name><disk>@DISK@</disk><net>@LAB_NET@</net>"
                "<devices></devices></domain>")
            (executor.images_dir / "target" / "disk-template").write_bytes(b"base")
            workhorse_agent.execute(executor, {
                "op_id": "bt", "operation": "build-start",
                "payload": {"role": "target", "version": "tgt-v1", "base_version": ""}})
            xml = (executor.disks_dir / "ctf-build-target.xml").read_text()
            self.assertIn("<graphics type='vnc' port='6911'", xml)  # injected

