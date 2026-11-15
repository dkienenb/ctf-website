"""Agent runtime workflows (handoff §11, §18): the heartbeat/poll/result
loop, controller-loss behavior, host-local lease enforcement, resource
reporting, and config loading — the parts the owner verified by hand."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase

from agent import agent as workhorse_agent
from portal.apps.testutils import make_host

from .test_agent import AgentExecutorTests


def _recording_client(poll_response=None, heartbeat_response=None, failing=False):
    """A ControllerClient stand-in that records every call."""
    import urllib.error

    calls = {"heartbeat": [], "poll": 0, "result": []}
    client = SimpleNamespace(
        base="http://controller", token="t", host_name="wh",
    )

    def heartbeat(resources):
        calls["heartbeat"].append(resources)
        if failing:
            raise urllib.error.URLError("controller down")
        return heartbeat_response or {"lease_deadlines": {}}

    def poll():
        calls["poll"] += 1
        if failing:
            raise urllib.error.URLError("controller down")
        return poll_response or {"commands": []}

    def result(op_id, status, detail):
        calls["result"].append({"op_id": op_id, "status": status, "detail": detail})
        if failing:
            raise urllib.error.URLError("controller down")
        return {"status": "completed"}

    client.heartbeat, client.poll, client.result = heartbeat, poll, result
    return client, calls


class AgentRuntimeTests(TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.executor = AgentExecutorTests._executor(AgentExecutorTests(), str(self.tmp))
        self.config = {"images_dir": str(self.tmp / "images"), "disks_dir": str(self.tmp / "disks"),
                       "controller_url": "http://controller", "token": "t", "host_name": "wh"}

    def _command(self, operation, **payload):
        command = {"op_id": "op-1234567890", "operation": operation, "payload": payload}
        command.update({"lab": payload.get("lab", 1)})
        return command

    def test_run_once_processes_commands_and_reports_results(self):
        client, calls = _recording_client(
            poll_response={"commands": [self._command("save", lab=2)]},
            heartbeat_response={"lease_deadlines": {"2": "3000-01-01T00:00:00"}},
        )
        handled = workhorse_agent.run_once(client, self.executor, self.config, deadlines={})
        self.assertEqual(handled, 1)
        # Heartbeat carried resources; the deadline from the response was stored.
        self.assertEqual(len(calls["heartbeat"]), 1)
        self.assertIn("free_disk_gb", calls["heartbeat"][0])
        self.assertIn("inventory", calls["heartbeat"][0])
        # The command executed through the vocabulary and reported OK.
        self.assertEqual(calls["result"][0]["status"], "ok")
        self.assertIn("saved_dir", calls["result"][0]["detail"])

    def test_run_once_reports_failures_without_retry(self):
        client, calls = _recording_client(
            poll_response={"commands": [self._command("template-list")]},
        )
        with mock.patch.object(workhorse_agent, "execute", side_effect=workhorse_agent.AgentError("boom")):
            handled = workhorse_agent.run_once(client, self.executor, self.config, deadlines={})
        self.assertEqual(handled, 1)
        self.assertEqual(calls["result"][0]["status"], "failed")
        self.assertIn("boom", calls["result"][0]["detail"]["error"])

    def test_run_once_survives_controller_outage_and_still_enforces_deadlines(self):
        """§18: with the controller unreachable, the agent still heartbeats
        locally, executes polled-nothing, and saves expired labs locally."""
        client, calls = _recording_client(failing=True)
        expired = "2020-01-01T00:00:00"
        deadlines = {"5": expired}
        with mock.patch.object(workhorse_agent.sys, "stderr"):
            handled = workhorse_agent.run_once(client, self.executor, self.config, deadlines)
        # The heartbeat attempt was made but its response failed (outage)…
        self.assertEqual(len(calls["heartbeat"]), 1)
        self.assertEqual(calls["result"], [])
        # …and no poll results were posted…
        self.assertEqual(calls["poll"], 1)  # poll attempted, response failed
        # …while the expired lab was still saved host-side.
        self.assertEqual(handled, 0)
        self.assertNotIn("5", deadlines)  # consumed by the local enforcement
        saves = [call for call in self.executor.calls if call[0] == "save"]
        self.assertEqual(len(saves), 2)  # attacker + target

    def test_enforce_deadlines_only_touches_expired_labs(self):
        client, calls = _recording_client()
        deadlines = {
            "1": "3000-01-01T00:00:00",   # future: untouched
            "2": "2020-01-01T00:00:00",   # expired: saved
            "3": "not-a-date",            # malformed: skipped, never crashes
        }
        workhorse_agent.enforce_deadlines(client, self.executor, deadlines)
        self.assertIn("1", deadlines)      # future deadline retained
        self.assertNotIn("2", deadlines)   # expired deadline consumed
        self.assertIn("3", deadlines)      # malformed deadline never popped by crash
        saves = [call for call in self.executor.calls if call[0] == "save"]
        self.assertEqual(len(saves), 2)    # one lab's attacker+target pair

    def test_enforce_deadlines_survives_a_failed_local_save(self):
        """A failed deadline save is reported, never crashes the loop, and
        the deadline is consumed so it is not retried destructively."""
        client, calls = _recording_client()
        deadlines = {"9": "2020-01-01T00:00:00"}
        with mock.patch.object(self.executor, "deadline_expired",
                               side_effect=workhorse_agent.AgentError("virsh failed")):
            with mock.patch.object(workhorse_agent.sys, "stderr"):
                workhorse_agent.enforce_deadlines(client, self.executor, deadlines)
        self.assertNotIn("9", deadlines)
        self.assertEqual(calls["result"], [])  # nothing reported to the controller

    def test_deadline_expired_is_a_coordinated_save(self):
        executor = self.executor
        detail = executor.deadline_expired({"lab": 4, "op_id": "local-4"})
        self.assertIn("saved_dir", detail)
        saves = [call for call in executor.calls if call[0] == "save"]
        self.assertEqual(len(saves), 2)

    def test_domain_names_are_keyed_to_the_lab(self):
        """allocate/save/reset on the same lab address the SAME guests (§12)."""
        self.assertEqual(workhorse_agent._domain_base({"lab": 7}), "lab7")
        self.assertEqual(workhorse_agent._domain_base({"lab": "7"}), "lab7")
        # No lab in the command: the op id keys the guests instead.
        self.assertEqual(workhorse_agent._domain_base({"op_id": "abcdef123456"}), "abcdef123456")
        attacker, target, _ = self.executor._names({"lab": 7, "op_id": "x"})
        self.assertEqual((attacker, target), ("ctf-lab7-atk", "ctf-lab7-tgt"))

    def test_recover_maps_to_target_scope_reset(self):
        """The 'recover' operation rebuilds only the target (no direct target
        access rules; attacker workspace preserved)."""
        executor = self.executor
        detail = workhorse_agent.execute(executor, {
            "op_id": "rec-1", "operation": "recover", "payload": {}, "lab": 3,
        })
        self.assertEqual(detail["reset"], "target")
        destroyed = [call[1] for call in executor.calls if call[0] == "destroy"]
        self.assertTrue(destroyed and all("-tgt" in name for name in destroyed))

    def test_refresh_lease_acknowledges_the_deadline(self):
        detail = workhorse_agent.execute(self.executor, {
            "op_id": "rl-1", "operation": "refresh-lease",
            "payload": {}, "acknowledged_deadline": "3000-01-01T00:00:00",
        })
        self.assertEqual(detail["acknowledged"], "3000-01-01T00:00:00")

    def test_collect_resources_reports_inventory_and_identity(self):
        for role in ("attacker", "target"):
            directory = self.tmp / "images" / role / "v1"
            directory.mkdir(parents=True)
            (directory / "domain.xml").write_text("<domain/>")
        config = dict(self.config, management_ip="192.0.2.7")
        resources = workhorse_agent.collect_resources(config, running_labs=2)
        self.assertEqual(resources["agent_version"], "1")
        self.assertEqual(resources["running_labs"], 2)
        self.assertEqual(resources["management_ip"], "192.0.2.7")
        self.assertGreater(resources["total_disk_gb"], 0)
        self.assertEqual(resources["inventory"]["attacker"]["v1"], {"domain.xml": 9})
        # Heartbeat-shaped payloads stay JSON-serializable (the agent posts them).
        json.dumps(resources)

    def test_load_config_requires_identity_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.toml"
            path.write_text('token = "t"\nhost_name = "wh"\n')  # controller_url missing
            with self.assertRaises(workhorse_agent.AgentError) as error:
                workhorse_agent.load_config(str(path))
            self.assertIn("controller_url", str(error.exception))
            path.write_text('controller_url = "http://c"\ntoken = "t"\nhost_name = "wh"\n')
            config = workhorse_agent.load_config(str(path))
            self.assertEqual(config["images_dir"], workhorse_agent.DEFAULT_IMAGES_DIR)

    def test_vocabulary_remains_restricted_and_closed(self):
        """No shell, no file ops outside templates, no invented operations."""
        for bad in ("shell", "exec", "eval", "rm", "define-file", "template-put-2", ""):
            with self.assertRaises(workhorse_agent.AgentError, msg=bad):
                workhorse_agent.execute(self.executor, {"op_id": "x", "operation": bad, "payload": {}})
        allowed = set(workhorse_agent.VOCABULARY)
        self.assertEqual(allowed, {
            "allocate", "save", "reset-target", "reset-full", "recover",
            "refresh-lease", "template-put", "template-verify", "template-remove", "template-list",
        })
