"""Account pages: status gating, settings, explicit Discord profile refresh,
and sign-out — plus the identity rules around the refresh (§3)."""
from types import SimpleNamespace
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from portal.apps.accounts.models import User
from portal.apps.testutils import make_user


class AccountPageGatingTests(TestCase):
    def setUp(self):
        self.approved = make_user("900000000000000001", "Alex")
        self.pending = make_user("900000000000000002", "Taylor", status=User.Status.PENDING)
        self.suspended = make_user("900000000000000003", "Sibyl", status=User.Status.SUSPENDED)

    def test_anonymous_users_are_sent_to_discord_login(self):
        for url in (reverse("status"), reverse("settings")):
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_status_page_renders_for_pending_and_suspended(self):
        self.client.force_login(self.pending)
        self.assertEqual(self.client.get(reverse("status")).status_code, 200)
        self.client.force_login(self.suspended)
        self.assertEqual(self.client.get(reverse("status")).status_code, 200)

    def test_settings_page_renders_for_approved(self):
        self.client.force_login(self.approved)
        self.assertEqual(self.client.get(reverse("settings")).status_code, 200)

    def test_sign_out_via_allauth(self):
        """The UI's sign-out button posts to allauth's logout endpoint."""
        self.client.force_login(self.approved)
        response = self.client.post(reverse("account_logout"))
        self.assertIn(response.status_code, (200, 302))
        response = self.client.get(reverse("settings"))
        self.assertEqual(response.status_code, 302)  # logged out


class ProfileRefreshTests(TestCase):
    def setUp(self):
        self.user = make_user("910000000000000001", "Old Name")
        self.client.force_login(self.user)
        self.url = reverse("refresh-profile")
        # The refresh flow looks up the Discord token via SocialToken; tests
        # stub the whole queryset chain so no allauth fixtures are needed.
        patcher = mock.patch("allauth.socialaccount.models.SocialToken.objects.filter")
        self.token_filter = patcher.start()
        self.token_filter.return_value.order_by.return_value.values_list.return_value.first.return_value = (
            "discord-oauth-token"
        )
        self.addCleanup(patcher.stop)

    def _api(self, payload):
        import io
        return mock.patch("portal.apps.accounts.views.urllib.request.urlopen",
                          return_value=io.BytesIO(__import__("json").dumps(payload).encode()))

    def test_refresh_updates_cached_display_only(self):
        with self._api({"id": self.user.discord_id, "global_name": "Fresh Display",
                        "avatar": "hash123"}):
            response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.cached_name, "Fresh Display")
        self.assertEqual(self.user.cached_avatar, "https://cdn.discordapp.com/avatars/%s/hash123.png" % self.user.discord_id)
        self.assertIsNotNone(self.user.profile_refreshed_at)
        # Identity, status, and admin flag untouched (§3).
        self.assertEqual(self.user.discord_id, "910000000000000001")
        self.assertEqual(self.user.status, User.Status.APPROVED)
        self.assertFalse(self.user.is_admin)

    def test_refresh_falls_back_to_username_then_cached_name(self):
        with self._api({"id": self.user.discord_id, "username": "handle", "avatar": None}):
            self.client.post(self.url)
        self.user.refresh_from_db()
        self.assertEqual(self.user.cached_name, "handle")
        with self._api({"id": self.user.discord_id, "avatar": None}):
            self.client.post(self.url)
        self.user.refresh_from_db()
        self.assertEqual(self.user.cached_name, "handle")  # neither field present → unchanged

    def test_refresh_rejects_a_different_discord_identity(self):
        with self._api({"id": "111222333444555666", "global_name": "Impostor"}):
            response = self.client.post(self.url)
        self.user.refresh_from_db()
        self.assertEqual(self.user.cached_name, "Old Name")  # cancelled
        self.assertIsNone(self.user.profile_refreshed_at)

    def test_refresh_survives_discord_being_down(self):
        with mock.patch("portal.apps.accounts.views.urllib.request.urlopen",
                        side_effect=OSError("discord unreachable")):
            response = self.client.post(self.url)
        self.user.refresh_from_db()
        self.assertEqual(self.user.cached_name, "Old Name")
        self.assertIsNone(self.user.profile_refreshed_at)

    def test_refresh_without_a_token_is_refused_cleanly(self):
        self.token_filter.return_value.order_by.return_value.values_list.return_value.first.return_value = None
        response = self.client.post(self.url)
        self.user.refresh_from_db()
        self.assertEqual(self.user.cached_name, "Old Name")
        self.assertIsNone(self.user.profile_refreshed_at)

    def test_refresh_requires_post(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)

    def test_refresh_requires_authentication(self):
        self.client.logout()
        self.assertEqual(self.client.post(self.url).status_code, 302)


class StatusCheckEndpointTests(TestCase):
    def test_pending_user_gets_not_approved(self):
        pending = make_user("991000000000000001", "Wait", status=User.Status.PENDING)
        self.client.force_login(pending)
        data = self.client.get(reverse("status-check")).json()
        self.assertEqual(data, {"authenticated": True, "approved": False})

    def test_approved_user_flips_the_flag(self):
        approved = make_user("991000000000000002", "Now In")
        self.client.force_login(approved)
        self.assertEqual(self.client.get(reverse("status-check")).json()["approved"], True)

    def test_approved_users_are_redirected_off_the_waiting_page(self):
        approved = make_user("991000000000000003", "Done")
        self.client.force_login(approved)
        response = self.client.get(reverse("status"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, "/")
