"""Accounts: user model/manager invariants, Discord login-state resolution,
and the admin-recovery command matrix (handoff §3, owner decisions)."""
from types import SimpleNamespace
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError
from django.test import TestCase

from portal.apps.accounts import adapter as discord_adapter
from portal.apps.accounts.models import ApprovalDecision, User
from portal.apps.testutils import make_user


class UserModelTests(TestCase):
    def test_create_user_requires_numeric_discord_id(self):
        for bad in ("", None, "abc", "12.5", "-42"):
            with self.assertRaises(ValueError):
                User.objects.create_user(bad, "Nobody")

    def test_discord_id_is_the_single_identity_key(self):
        User.objects.create_user("111111111111111111", "First")
        with self.assertRaises(IntegrityError):
            User.objects.create_user("111111111111111111", "Second")

    def test_new_accounts_start_pending_and_unapproved(self):
        user = User.objects.create_user("122222222222222222", "Fresh")
        self.assertEqual(user.status, User.Status.PENDING)
        self.assertFalse(user.is_approved)
        self.assertFalse(user.is_admin)

    def test_status_does_not_imply_admin_and_vice_versa(self):
        """Approval never confers admin; admin is never conferral of approval."""
        admin = make_user("133333333333333333", "Admin", is_admin=True)
        self.assertTrue(admin.is_admin)
        self.assertTrue(admin.is_approved)  # make_user default
        pleb = make_user("144444444444444444", "Pleb")
        self.assertTrue(pleb.is_approved)
        self.assertFalse(pleb.is_admin)

    def test_create_superuser_sets_all_flags(self):
        user = User.objects.create_superuser("155555555555555555")
        self.assertTrue(user.is_admin and user.is_staff and user.is_superuser)

    def test_suspended_and_rejected_are_not_approved(self):
        self.assertFalse(make_user("166666666666666666", "S", status=User.Status.SUSPENDED).is_approved)
        self.assertFalse(make_user("177777777777777777", "R", status=User.Status.REJECTED).is_approved)


class _FakeSocialLogin(SimpleNamespace):
    """Shape-compatible stand-in for allauth's SocialLogin."""


class DiscordLoginResolutionTests(TestCase):
    """The login adapter's state matrix (adapter.py docstring)."""

    def setUp(self):
        self.sociallogin = _FakeSocialLogin(
            is_existing=True, user=None, account=SimpleNamespace(
                uid="188888888888888888",
                extra_data={"global_name": "Player One", "username": "playerone", "avatar": None},
            ),
        )
        self.sociallogin.user = User.objects.create_user("188888888888888888", "Old Name")

    def _profile_fallbacks(self):
        # Username fallback when no global_name; numeric fallback when neither.
        login = _FakeSocialLogin(account=SimpleNamespace(uid="999999999999999999", extra_data={}))
        uid, display, avatar = discord_adapter._discord_profile(login)
        self.assertEqual(uid, "999999999999999999")
        self.assertEqual(display, "Discord 9999")
        self.assertIn("embed/avatars/", avatar)
        login2 = _FakeSocialLogin(account=SimpleNamespace(uid="999999999999999998", extra_data={"username": "handle"}))
        self.assertEqual(discord_adapter._discord_profile(login2)[1], "handle")

    def test_rejected_goes_back_to_pending_for_reapplication(self):
        self.sociallogin.user.status = User.Status.REJECTED
        self.sociallogin.user.save()
        discord_adapter.DiscordAccountAdapter().pre_social_login(None, self.sociallogin)
        self.sociallogin.user.refresh_from_db()
        self.assertEqual(self.sociallogin.user.status, User.Status.PENDING)

    def test_pending_with_preapproval_is_approved(self):
        ApprovalDecision.objects.create(discord_id=self.sociallogin.user.discord_id, decision="preapprove")
        discord_adapter.DiscordAccountAdapter().pre_social_login(None, self.sociallogin)
        self.sociallogin.user.refresh_from_db()
        self.assertEqual(self.sociallogin.user.status, User.Status.APPROVED)

    def test_suspended_stays_suspended_at_login(self):
        """Suspension survives login: no path may silently restore access."""
        self.sociallogin.user.status = User.Status.SUSPENDED
        self.sociallogin.user.save()
        discord_adapter.DiscordAccountAdapter().pre_social_login(None, self.sociallogin)
        self.sociallogin.user.refresh_from_db()
        self.assertEqual(self.sociallogin.user.status, User.Status.SUSPENDED)

    def test_stale_preapproval_does_not_touch_approved_or_suspended(self):
        ApprovalDecision.objects.create(discord_id=self.sociallogin.user.discord_id, decision="preapprove")
        for status in (User.Status.APPROVED, User.Status.SUSPENDED, User.Status.REJECTED):
            self.sociallogin.user.status = status
            self.sociallogin.user.save()
            discord_adapter.DiscordAccountAdapter().pre_social_login(None, self.sociallogin)
            self.sociallogin.user.refresh_from_db()
            # rejected->pending (reapply) is the only non-approved transition
            expected = User.Status.PENDING if status == User.Status.REJECTED else status
            self.assertEqual(self.sociallogin.user.status, expected)

    def test_preapproval_ledger_lives_even_without_an_account(self):
        """Pre-approval is keyed by Discord ID and needs no User row (§3)."""
        ApprovalDecision.objects.create(discord_id="199999999999999999", decision="approve")
        self.assertTrue(discord_adapter._is_preapproved("199999999999999999"))
        self.assertFalse(discord_adapter._is_preapproved("211111111111111111"))

    def test_cached_name_never_overwritten_at_login(self):
        """populated_user refreshes display fields; identity/ownership do not move."""
        original = self.sociallogin.user
        login = _FakeSocialLogin(
            account=SimpleNamespace(uid=original.discord_id, extra_data={"global_name": "New Display", "avatar": None}),
            user=SimpleNamespace(discord_id=original.discord_id, cached_name="Old Name", cached_avatar=""),
        )
        refreshed = discord_adapter.DiscordAccountAdapter().populate_user(None, login, {})
        self.assertEqual(refreshed.discord_id, original.discord_id)
        self.assertEqual(refreshed.cached_name, "New Display")  # display refresh is allowed
        self.assertEqual(refreshed.discord_id, original.discord_id)  # identity is not


class AdminRecoveryTests(TestCase):
    def test_grant_admin_existing_account(self):
        user = make_user("222222222222222222", "Recoverable")
        call_command("grant_admin", user.discord_id)
        user.refresh_from_db()
        self.assertTrue(user.is_admin)
        self.assertEqual(user.status, User.Status.APPROVED)  # untouched

    def test_grant_admin_creates_account_when_identity_lost(self):
        call_command("grant_admin", "233333333333333333")
        user = User.objects.get(discord_id="233333333333333333")
        self.assertTrue(user.is_admin)
        self.assertEqual(user.status, User.Status.APPROVED)
        self.assertTrue(ApprovalDecision.objects.filter(
            discord_id=user.discord_id, note__icontains="recovery").exists())

    def test_grant_admin_reinstates_suspended_account(self):
        user = make_user("244444444444444444", "Suspended Admin", is_admin=True)
        user.status = User.Status.SUSPENDED
        user.save()
        call_command("grant_admin", user.discord_id)
        user.refresh_from_db()
        self.assertTrue(user.is_admin)
        self.assertEqual(user.status, User.Status.APPROVED)
        self.assertTrue(ApprovalDecision.objects.filter(
            discord_id=user.discord_id, decision="reinstate").exists())

    def test_grant_admin_rejects_bad_ids(self):
        for bad in ("", "abc", "12", "x" * 30):
            with self.assertRaises(CommandError):
                call_command("grant_admin", bad)

    def test_grant_admin_is_idempotent(self):
        user = make_user("255555555555555555", "Twice")
        call_command("grant_admin", user.discord_id)
        with mock.patch("portal.apps.accounts.management.commands.grant_admin.ApprovalDecision.objects.create") as spy:
            call_command("grant_admin", user.discord_id)
            self.assertFalse(spy.called)  # existing account: no duplicate ledger rows
        self.assertTrue(User.objects.get(pk=user.pk).is_admin)
