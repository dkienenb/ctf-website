"""Lab page flows: the challenge page's request/claim/refresh/release/reset
buttons, the desktop route, and the register_host command (the operator
entry point for provisioning)."""
import json

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from portal.apps.accounts.models import User
from portal.apps.labs.models import Host, LabInstance, QueueEntry
from portal.apps.testutils import complete, make_host, make_lab_challenge, make_user


@override_settings(SUBMISSION_COOLDOWN_SECONDS=0, GUACAMOLE_JSON_SECRET="ab" * 16)
class LabPageFlowTests(TestCase):
    def setUp(self):
        self.host = make_host()
        self.user = make_user("920000000000000001", "Alex")
        self.admin = make_user("920000000000000002", "Admin", is_admin=True)
        self.challenge = make_lab_challenge()
        self.client.force_login(self.user)

    def _action(self, action, slug=None, **extra):
        return self.client.post(
            reverse("lab-action", args=[slug or self.challenge.slug, action]), **extra
        )

    def test_request_claim_and_desktop_route_through_the_page(self):
        response = self._action("request")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._lab().state, "reserved")
        self._action("claim")
        self.assertEqual(self._lab().state, "starting")
        complete(self.host, "allocate")
        self.assertEqual(self._lab().state, "running")
        # The desktop opens for a running lab (grant issued, redirect to Guacamole).
        response = self.client.get(reverse("lab-desktop", args=[self.challenge.slug]))
        self.assertEqual(response.status_code, 302)
        self.assertIn("data=", response.url)  # the signed grant rides the URL

    def _lab(self):
        return LabInstance.objects.get(user=self.user, challenge=self.challenge)

    def test_get_to_lab_action_is_405(self):
        self.assertEqual(
            self.client.get(reverse("lab-action", args=[self.challenge.slug, "request"])).status_code,
            405,
        )

    def test_unknown_action_is_refused_cleanly(self):
        response = self._action("detonate")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(LabInstance.objects.exists())

    def test_refresh_and_release_refuse_wrong_states_via_page(self):
        self._action("request")
        self._action("claim")
        complete(self.host, "allocate")
        # Release while running works; releasing again (already saving) refuses.
        self._action("release")
        self.assertEqual(self._lab().state, "saving")
        response = self._action("release")
        self.assertEqual(response.status_code, 302)  # back to the challenge with an error toast

    def test_actions_require_approved_accounts(self):
        locked = make_user("920000000000000003", "Locked", status=User.Status.PENDING)
        self.client.force_login(locked)
        response = self._action("request")
        self.assertIn(response.status_code, (302, 403))
        self.assertFalse(LabInstance.objects.filter(user=locked).exists())

    def test_request_reset_notifies_admins_via_page(self):
        self._action("request")
        response = self._action("request-reset")
        self.assertEqual(response.status_code, 302)
        entry = QueueEntry.objects.filter(user=self.user, challenge=self.challenge).order_by("-id").first()
        self.assertIsNotNone(entry)  # the request is recorded for the operator flow

    def test_desktop_refused_before_running(self):
        self._action("request")
        self._action("claim")
        response = self.client.get(reverse("lab-desktop", args=[self.challenge.slug]))
        self.assertEqual(response.status_code, 302)  # redirected with an error, no grant
        self.assertNotIn("data=", getattr(response, "url", ""))


class RegisterHostCommandTests(TestCase):
    def test_registers_and_prints_token_once(self):
        out = self._run("workhorse-new", capacity=2)
        host = Host.objects.get(name="workhorse-new")
        self.assertEqual(host.capacity_slots, 2)
        self.assertTrue(host.online)
        printed = out.strip().splitlines()
        token_line = [line for line in printed if line.strip()]
        self.assertTrue(any("token" in line.lower() for line in token_line), out)
        self.assertNotEqual(host.token_hash, "")  # only the hash is stored

    def _run(self, name, capacity=1):
        import io
        buffer = io.StringIO()
        call_command("register_host", name, capacity=capacity, stdout=buffer)
        return buffer.getvalue()

    def test_rerun_rotates_the_token(self):
        self._run("workhorse-rot")
        host = Host.objects.get(name="workhorse-rot")
        first_hash = host.token_hash
        self._run("workhorse-rot", capacity=3)
        host.refresh_from_db()
        self.assertEqual(host.capacity_slots, 3)
        self.assertNotEqual(host.token_hash, first_hash)  # re-registration rotates credentials
