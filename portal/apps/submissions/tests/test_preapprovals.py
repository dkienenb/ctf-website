"""Pre-approval revocation: the ledger semantics, the login adapter's
behavior after a revoke, and the operator page flow."""
from django.test import TestCase, override_settings
from django.urls import reverse

from portal.apps.accounts import adapter as discord_adapter
from portal.apps.accounts.models import ApprovalDecision, User
from portal.apps.submissions import adminops
from portal.apps.submissions.services import ServiceError
from portal.apps.testutils import make_user


class RevokePreapprovalServiceTests(TestCase):
    def setUp(self):
        self.admin = make_user("950000000000000001", "Admin", is_admin=True)
        self.pleb = make_user("950000000000000002", "Pleb")

    def test_revoke_appends_to_the_ledger(self):
        adminops.preapprove(self.admin, "960000000000000001")
        adminops.revoke_preapproval(self.admin, "960000000000000001")
        self.assertTrue(ApprovalDecision.objects.filter(
            discord_id="960000000000000001", decision="revoke-preapprove").exists())

    def test_revoke_negatives(self):
        with self.assertRaises(ServiceError) as error:
            adminops.revoke_preapproval(self.admin, "961111111111111111")  # never pre-approved
        self.assertEqual(error.exception.status, 404)
        adminops.preapprove(self.admin, "960000000000000002")
        adminops.revoke_preapproval(self.admin, "960000000000000002")
        with self.assertRaises(ServiceError) as error:
            adminops.revoke_preapproval(self.admin, "960000000000000002")  # already revoked
        self.assertEqual(error.exception.status, 409)
        for bad in ("abc", "12"):
            with self.assertRaises(ServiceError, msg=bad):
                adminops.revoke_preapproval(self.admin, bad)

    def test_revoke_does_not_touch_direct_approvals(self):
        """A directly approved account has no pre-approval to revoke; the
        lever for those is suspension."""
        user = make_user("960000000000000003", "Approved")
        adminops.approve_user(self.admin, user.id)
        with self.assertRaises(ServiceError) as error:
            adminops.revoke_preapproval(self.admin, user.discord_id)
        self.assertIn("approved directly", error.exception.message)
        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.APPROVED)  # access untouched

    def test_revocation_keeps_existing_accounts_access(self):
        """Revoke is about FUTURE auto-approvals; an account that already
        used the pre-approval keeps its access (suspension is the other lever)."""
        user = make_user("960000000000000004", "Kept")
        adminops.preapprove(self.admin, user.discord_id)  # upgraded the account
        adminops.revoke_preapproval(self.admin, user.discord_id)
        user.refresh_from_db()
        self.assertEqual(user.status, User.Status.APPROVED)
        self.assertTrue(user.is_approved)

    def test_non_admin_cannot_revoke(self):
        adminops.preapprove(self.admin, "960000000000000005")
        with self.assertRaises(ServiceError) as error:
            adminops.revoke_preapproval(self.pleb, "960000000000000005")
        self.assertEqual(error.exception.status, 403)


class LoginAfterRevocationTests(TestCase):
    """The adapter honors revocations: the ID no longer auto-approves."""

    def setUp(self):
        self.admin = make_user("970000000000000001", "Admin", is_admin=True)
        self.sociallogin = _FakeLogin()

    def _upgraded_at_login(self):
        user = self.sociallogin.user
        discord_adapter.DiscordAccountAdapter().pre_social_login(None, self.sociallogin)
        user.refresh_from_db()
        return user.status

    def _pending_user(self, discord_id):
        self.sociallogin.user = make_user(discord_id, "Waiter", status=User.Status.PENDING)
        return self.sociallogin.user

    def test_revoked_preapproval_no_longer_upgrades_pending(self):
        user = self._pending_user("970000000000000002")
        adminops.preapprove(self.admin, user.discord_id)
        self.assertEqual(self._upgraded_at_login(), User.Status.APPROVED)  # before revoke
        user.status = User.Status.PENDING
        user.save()
        adminops.revoke_preapproval(self.admin, user.discord_id)
        self.assertEqual(self._upgraded_at_login(), User.Status.PENDING)  # no longer upgrades
        self.assertFalse(discord_adapter._is_preapproved(user.discord_id))

    def test_new_signup_after_revocation_stays_pending(self):
        adminops.preapprove(self.admin, "970000000000000003")
        adminops.revoke_preapproval(self.admin, "970000000000000003")
        # save_user consults _is_preapproved for a brand-new account:
        self.assertFalse(discord_adapter._is_preapproved("970000000000000003"))

    def test_preapproval_after_a_revocation_re_grants(self):
        """The ledger is append-only: a NEW pre-approval after a revoke is
        again effective (re-approving someone is a normal operator action)."""
        adminops.preapprove(self.admin, "970000000000000004")
        adminops.revoke_preapproval(self.admin, "970000000000000004")
        self.assertFalse(discord_adapter._is_preapproved("970000000000000004"))
        adminops.preapprove(self.admin, "970000000000000004")
        self.assertTrue(discord_adapter._is_preapproved("970000000000000004"))


class RevokePageFlowTests(TestCase):
    def setUp(self):
        self.admin = make_user("980000000000000001", "Admin", is_admin=True)
        self.client.force_login(self.admin)

    def test_dashboard_offers_revoke_only_on_awaiting_ids(self):
        adminops.preapprove(self.admin, "980000000000000002")   # awaiting
        used = make_user("980000000000000003", "Used")
        adminops.preapprove(self.admin, used.discord_id)        # already used
        response = self.client.get(reverse("manage"))
        self.assertContains(response, "revoke-preapproval")
        # The used one has no revoke button (its account row shows the badge instead).
        awaiting_row = response.content.decode().split("980000000000000002", 1)[1]
        self.assertIn('value="revoke-preapproval"', awaiting_row)
        self.assertIn(">Revoke</button>", awaiting_row)

    def test_revoke_through_the_confirm_flow(self):
        adminops.preapprove(self.admin, "980000000000000004")
        response = self.client.post(reverse("manage-action"),
                                    {"action": "revoke-preapproval", "target": "980000000000000004"})
        self.assertEqual(response.status_code, 200)  # server-rendered confirm
        self.assertContains(response, "Revoke this pre-approval?")
        response = self.client.post(reverse("manage-action"),
                                    {"action": "revoke-preapproval", "target": "980000000000000004",
                                     "confirm": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ApprovalDecision.objects.filter(
            discord_id="980000000000000004", decision="revoke-preapprove").exists())


class _FakeLogin:
    """Minimal SocialLogin stand-in for the adapter's login hooks."""

    def __init__(self):
        self.is_existing = True
        self.user = None
