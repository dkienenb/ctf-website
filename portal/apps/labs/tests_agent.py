"""Agent API and restricted-executor tests (handoff §14, §18)."""
import json
import uuid
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from agent import agent as workhorse_agent  # repo-root agent package
from portal.apps.labs.models import Host, HostCommand, LabInstance, LabTemplate
from portal.apps.labs.services import ServiceError

from portal.apps.submissions.tests import make_user
from portal.apps.labs.tests import make_lab_challenge, make_host


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

            def virsh(self, *args):
                self.calls.append(args)

            def virsh_tolerant(self, *args):
                self.calls.append(args)

            def domain_exists(self, name):
                return False

            def _ensure_disk(self, source, template_disk, payload):
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(b"disk")

        executor = RecordingExecutor(None)
        executor.images_dir = Path(tmp) / "images"
        executor.disks_dir = Path(tmp) / "disks"
        executor.disks_dir.mkdir(parents=True)
        return executor

    def test_vocabulary_is_restricted(self):
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
            command = {"op_id": "op-x", "operation": "allocate", "payload": {"lab": 9}, "lab": 9}
            detail = workhorse_agent.execute(executor, command)
            self.assertFalse(detail["resumed"])
            defines = [call[1] for call in executor.calls if call[0] == "define"]
            self.assertNotIn("ctf-lab9-atk", defines)  # attacker preserved, not redefined
            self.assertIn("ctf-lab9-tgt", defines)    # target rebuilt
            started = [call[1] for call in executor.calls if call[0] == "start"]
            self.assertNotIn("ctf-lab9-atk", started)  # already running