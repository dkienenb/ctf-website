"""Operator page-flow tests: /manage/ access control and the two-step
correction preview → apply workflow through the rendered forms."""
import uuid

from django.test import TestCase, override_settings
from django.urls import reverse

from portal.apps.accounts.models import User
from portal.apps.content.models import Hint
from portal.apps.submissions import services
from portal.apps.submissions.models import DataRevision, Solve

from portal.apps.testutils import make_question, make_user


@override_settings(SUBMISSION_COOLDOWN_SECONDS=0)
class ManagePageTests(TestCase):
    def setUp(self):
        self.admin = make_user("500000000000000001", "Admin", is_admin=True)
        self.member = make_user("500000000000000002", "Alex")

    def test_access_control(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse("manage"))
        self.assertRedirects(response, reverse("home"))  # approval alone is not admin
        self.client.force_login(self.admin)
        response = self.client.get(reverse("manage"))
        self.assertContains(response, "Accounts")

    def test_correction_workflow_through_forms(self):
        self.client.force_login(self.admin)
        question = make_question(["CTF{x}"], points=100)
        self.assertTrue(services.submit(self.member, question.id, "CTF{x}", "request-" + uuid.uuid4().hex)["correct"])
        self.assertEqual(Solve.objects.get(user=self.member, question=question).submission.answer, "CTF{x}")

        # Step 1: preview through the rendered form.
        preview_response = self.client.post(reverse("manage-question-edit", args=[question.id]), {
            "prompt": question.prompt,
            "input_format": question.input_format,
            "points": "20",
            "display_order": "0",
            "answers": "CTF{x}",
            "mode": "none",
            "reason": "typo fix",
            "preview": "1",
        })
        self.assertContains(preview_response, "Impact preview")
        self.assertContains(preview_response, "(-80)")  # effect rows render
        revision = preview_response.context["preview"]["revision"]

        # Step 2: apply the same values with the previewed revision.
        self.client.post(reverse("manage-question-edit", args=[question.id]), {
            "prompt": question.prompt,
            "input_format": question.input_format,
            "points": "20",
            "display_order": "0",
            "answers": "CTF{x}",
            "mode": "none",
            "reason": "typo fix",
            "expected_revision": str(revision),
            "apply": "1",
        })
        self.member.refresh_from_db()
        self.assertEqual(services.summary(self.member)["points"], 20)
        self.assertTrue(Solve.objects.filter(user=self.member, question=question).exists())

    def test_stale_apply_is_rejected(self):
        self.client.force_login(self.admin)
        question = make_question(["CTF{x}"], points=100)
        services.submit(self.member, question.id, "CTF{x}", "request-" + uuid.uuid4().hex)
        DataRevision.bump()  # intervening change
        response = self.client.post(reverse("manage-question-edit", args=[question.id]), {
            "prompt": question.prompt,
            "input_format": question.input_format,
            "points": "20",
            "display_order": "0",
            "answers": "CTF{x}",
            "mode": "none",
            "reason": "typo fix",
            "expected_revision": str(DataRevision.current() - 1),
            "apply": "1",
        }, follow=True)
        messages = [str(message) for message in response.context["messages"]]
        self.assertTrue(any("Preview the change again" in message for message in messages))
        self.assertEqual(services.summary(self.member)["points"], 100)

    def test_hint_price_workflow_and_confirm_flow(self):
        self.client.force_login(self.admin)
        question = make_question(["x"])
        hint = Hint.objects.create(challenge=question.challenge, description="d", text="t", cost=20)
        services.redeem(self.member, hint.id)

        preview_response = self.client.post(reverse("manage-hint-edit", args=[hint.id]), {
            "cost": "80", "description": "d", "text": "t", "reason": "price bump", "preview": "1",
        })
        self.assertContains(preview_response, "Impact preview")
        revision = preview_response.context["preview"]["revision"]
        self.client.post(reverse("manage-hint-edit", args=[hint.id]), {
            "cost": "80", "description": "d", "text": "t", "reason": "price bump",
            "expected_revision": str(revision), "apply": "1",
        })
        hint.refresh_from_db()
        self.assertEqual(hint.cost, 80)
        self.assertEqual(services.summary(self.member)["points"], -80)

        # Confirm step renders before destructive actions.
        response = self.client.post(reverse("manage-action"), {
            "action": "refund", "target": f"{self.member.id}:{hint.id}",
        })
        self.assertContains(response, "Refund this hint redemption?")
        self.assertEqual(services.summary(self.member)["points"], -80)  # not yet applied
        self.client.post(reverse("manage-action"), {
            "action": "refund", "target": f"{self.member.id}:{hint.id}", "confirm": "1",
        }, follow=True)
        self.assertEqual(services.summary(self.member)["points"], 0)

    def test_lab_recovery_action_issues_command(self):
        """The Recover button issues the durable recover command (regression:
        this was a 500 — the view referenced an unimported service module)."""
        from portal.apps.labs.models import Host, HostCommand, LabInstance

        self.client.force_login(self.admin)
        host = Host.objects.create(name="recovery-test-host")
        challenge = make_question(["x"]).challenge
        lab = LabInstance.objects.create(user=self.member, challenge=challenge, host=host, state="recovery")
        response = self.client.post(
            reverse("manage-action"),
            {"action": "recover", "target": f"{self.member.id}:{challenge.id}", "confirm": "1"},
        )
        self.assertRedirects(response, reverse("manage"))
        command = HostCommand.objects.filter(lab=lab, operation="recover").first()
        self.assertIsNotNone(command)
        self.assertEqual(command.status, HostCommand.Status.PENDING)

    def test_preapprove_and_lifecycle_buttons(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("manage-preapprove"), {"discord_id": "1234567890123"}, follow=True)
        self.assertContains(response, "pre-approved")
        challenge = make_question(["x"]).challenge
        self.client.post(reverse("manage-action"), {"action": "archive", "target": str(challenge.id), "confirm": "1"}, follow=True)
        challenge.refresh_from_db()
        self.assertEqual(challenge.lifecycle, "archived")
