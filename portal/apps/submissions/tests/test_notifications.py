"""Notification delivery: inbox page, the toast polling endpoint, mark-read,
and the anonymous/unapproved gates around all of them."""
from django.test import TestCase
from django.urls import reverse

from portal.apps.accounts.models import User
from portal.apps.submissions.models import Notification
from portal.apps.testutils import make_user


def _notify(user, message):
    return Notification.objects.create(recipient=user, message=message)


class NotificationPageTests(TestCase):
    def setUp(self):
        self.user = make_user("800000000000000001", "Alex")
        self.client.force_login(self.user)

    def test_inbox_lists_messages_newest_first(self):
        first = _notify(self.user, "older message")
        second = _notify(self.user, "newer message")
        response = self.client.get(reverse("notifications"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "older message")
        self.assertContains(response, "newer message")
        content = response.content.decode()
        self.assertLess(content.index("newer message"), content.index("older message"))

    def test_inbox_shows_no_other_users_messages(self):
        other = make_user("800000000000000002", "Sam")
        _notify(other, "private to Sam")
        response = self.client.get(reverse("notifications"))
        self.assertNotContains(response, "private to Sam")


class NotificationPollTests(TestCase):
    def setUp(self):
        self.user = make_user("810000000000000001", "Alex")
        self.client.force_login(self.user)
        self.url = reverse("notifications-poll")

    def test_poll_reports_unread_and_unseen_messages(self):
        _notify(self.user, "toast one")
        _notify(self.user, "toast two")
        data = self.client.get(self.url).json()
        self.assertEqual(data["unread"], 2)
        self.assertEqual([m["message"] for m in data["messages"]], ["toast two", "toast one"])

    def test_poll_caps_the_message_payload_at_five(self):
        for index in range(8):
            _notify(self.user, f"message {index}")
        data = self.client.get(self.url).json()
        self.assertEqual(data["unread"], 8)
        self.assertEqual(len(data["messages"]), 5)  # payload is capped; badge shows the rest

    def test_read_messages_disappear_from_poll(self):
        note = _notify(self.user, "already seen")
        note.read_at = note.created_at
        note.save()
        data = self.client.get(self.url).json()
        self.assertEqual(data["unread"], 0)
        self.assertEqual(data["messages"], [])

    def test_mark_read_empties_the_poll_and_is_repeatable(self):
        _notify(self.user, "to be marked")
        response = self.client.post(reverse("notifications-read"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(self.url).json()["unread"], 0)
        # Marking read again is harmless.
        self.client.post(reverse("notifications-read"))
        self.assertEqual(self.client.get(self.url).json()["unread"], 0)

    def test_get_to_mark_read_is_ignored(self):
        _notify(self.user, "still unread")
        self.client.get(reverse("notifications-read"))
        self.assertEqual(self.client.get(self.url).json()["unread"], 1)

    def test_anonymous_users_cannot_poll(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 401)  # clean 401 JSON, not a 500

    def test_locked_accounts_cannot_poll(self):
        self.user.status = User.Status.SUSPENDED
        self.user.save()
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertIn(response.status_code, (302, 403))
