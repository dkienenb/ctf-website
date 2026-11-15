"""Operator service and page-flow tests (approvals, hints, content, manage pages)."""
import io
import uuid

from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from portal.apps.accounts.models import ApprovalDecision, User
from portal.apps.content.models import Challenge, Hint, Question
from portal.apps.submissions import adminops, services
from portal.apps.labs.models import LabInstance, QueueEntry
from portal.apps.submissions.models import (
    AuditEvent, DataRevision, HintRedemption, Notification, Solve, Submission,
)
from portal.apps.submissions.services import ServiceError

from .tests import make_question, make_user


@override_settings(SUBMISSION_COOLDOWN_SECONDS=0)
class OperatorServiceTests(TestCase):
    def test_dashboard_renders_with_hint_redemptions(self):
        """Regression: the dashboard crashed with AttributeError on the first
        HintRedemption (recipient_id vs the model's user field)."""
        challenge = self._challenge()
        hint = Hint.objects.create(challenge_id=challenge.id, description="d", text="t", cost=10)
        services.redeem(self.user, hint.id)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("manage"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Refund first owner")

    def test_dashboard_renders_with_machine_images(self):
        """The dashboard links the machine-image workbench."""
        self.client.force_login(self.admin)
        response = self.client.get(reverse("manage"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Machine images")

    def setUp(self):
        self.admin = make_user("400000000000000001", "Admin", is_admin=True)
        self.user = make_user("400000000000000002", "Alex")
        DataRevision.current()

    def test_approval_lifecycle_and_notifications(self):
        adminops.approve_user(self.admin, self.user.id)
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.APPROVED)
        self.assertTrue(ApprovalDecision.objects.filter(discord_id=self.user.discord_id, decision="approve").exists())
        self.assertTrue(Notification.objects.filter(recipient=self.user, message__icontains="approved").exists())

        adminops.suspend_user(self.admin, self.user.id)
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.SUSPENDED)
        self.assertTrue(Notification.objects.filter(recipient=self.user, message__icontains="suspended").exists())

        adminops.reinstate_user(self.admin, self.user.id)
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.APPROVED)
        self.assertTrue(ApprovalDecision.objects.filter(discord_id=self.user.discord_id, decision="reinstate").exists())
        self.assertTrue(Notification.objects.filter(recipient=self.user, message__icontains="reinstated").exists())

        # Rejection requires a pending account (owner decision: reject + reapply).
        User.objects.filter(pk=self.user.id).update(status=User.Status.PENDING)
        adminops.reject_user(self.admin, self.user.id)
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.REJECTED)

    def test_reject_requires_pending(self):
        with self.assertRaises(ServiceError) as error:
            adminops.reject_user(self.admin, self.user.id)  # approved, not pending
        self.assertEqual(error.exception.status, 409)

    def test_suspension_cancels_queue_and_saves_lab(self):
        challenge = Challenge.objects.create(title="Lab challenge", slug="lab-challenge")
        lab = LabInstance.objects.create(user=self.user, challenge=challenge, state="running")
        QueueEntry.objects.create(user=self.user, challenge=challenge)
        adminops.suspend_user(self.admin, self.user.id)
        lab.refresh_from_db()
        self.assertEqual(lab.state, "saved")
        self.assertFalse(QueueEntry.objects.filter(user=self.user, active=True).exists())
        with self.assertRaises(ServiceError):
            adminops.suspend_user(self.admin, self.admin.id)  # cannot self-suspend

    def test_reinstate_requires_suspended_and_keeps_saved_lab(self):
        with self.assertRaises(ServiceError) as error:
            adminops.reinstate_user(self.admin, self.user.id)  # approved, not suspended
        self.assertEqual(error.exception.status, 409)
        challenge = Challenge.objects.create(title="Lab challenge", slug="lab-challenge-2")
        lab = LabInstance.objects.create(user=self.user, challenge=challenge, state="saved")
        adminops.suspend_user(self.admin, self.user.id)
        adminops.reinstate_user(self.admin, self.user.id)
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.APPROVED)
        lab.refresh_from_db()
        self.assertEqual(lab.state, "saved")  # saved lab untouched; the user resumes it normally

    def test_preapprove_upgrades_pending_without_account(self):
        adminops.preapprove(self.admin, "1234567890123")
        # No account existed; the ledger alone is enough for the login adapter.
        self.assertTrue(ApprovalDecision.objects.filter(discord_id="1234567890123", decision="preapprove").exists())
        with self.assertRaises(ServiceError):
            adminops.preapprove(self.admin, "abc")  # non-numeric
        with self.assertRaises(ServiceError):
            adminops.preapprove(self.admin, "123")  # too short

    def test_grant_admin_recovery_command(self):
        from django.core.management import call_command

        # Existing account: admin flag restored, approval untouched.
        call_command("grant_admin", self.user.discord_id)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_admin)

        # Lost identity: the replacement Discord ID gets a pre-approved
        # account, ledger records the recovery, output reports it.
        out = io.StringIO()
        call_command("grant_admin", "499999999999999001", stdout=out)
        recovered = User.objects.get(discord_id="499999999999999001")
        self.assertTrue(recovered.is_admin)
        self.assertEqual(recovered.status, User.Status.APPROVED)
        self.assertTrue(ApprovalDecision.objects.filter(
            discord_id=recovered.discord_id, note__icontains="recovery").exists())

        # A suspended admin can be restored through the same command.
        adminops.suspend_user(self.admin, self.user.id)
        call_command("grant_admin", self.user.discord_id)
        self.user.refresh_from_db()
        self.assertEqual(self.user.status, User.Status.APPROVED)
        self.assertTrue(self.user.is_admin)

        with self.assertRaises(CommandError):
            call_command("grant_admin", "abc")

    def test_notify_admins_creates_inbox_alerts(self):
        from django.core.management import call_command

        call_command("notify_admins", "Drill: backup failure path")
        self.assertTrue(Notification.objects.filter(
            recipient=self.admin, message__icontains="Drill: backup failure path").exists())
        self.assertFalse(Notification.objects.filter(recipient=self.user).exists())  # non-admins untouched
        Notification.objects.all().delete()
        User.objects.filter(pk=self.admin.id).update(is_admin=False)
        with self.assertRaises(CommandError):
            call_command("notify_admins", "x")  # no admins left to notify

    def test_hint_price_preview_and_retroactive_apply(self):
        hint = Hint.objects.create(challenge_id=self._challenge().id, description="d", text="t", cost=20)
        services.redeem(self.user, hint.id)
        base = services.summary(self.user)["points"]
        preview = adminops.preview_hint_edit(hint, cost=80, description="d", text="t")
        self.assertEqual(preview["redeemed_count"], 1)
        self.assertEqual(preview["effects"][0]["score_delta"], -60)
        self.assertEqual(services.summary(self.user)["points"], base)  # preview mutated nothing

        stale = preview["revision"]
        DataRevision.bump()
        with self.assertRaises(ServiceError) as error:
            adminops.apply_hint_edit(self.admin, hint, cost=80, description="d", text="t", expected_revision=stale)
        self.assertEqual(error.exception.status, 409)

        preview = adminops.preview_hint_edit(hint, cost=80, description="d", text="t")
        adminops.apply_hint_edit(self.admin, hint, cost=80, description="d", text="t", expected_revision=preview["revision"])
        self.assertEqual(services.summary(self.user)["points"], base - 60)
        self.assertTrue(Notification.objects.filter(recipient=self.user, message__icontains="price changed").exists())
        self.assertTrue(AuditEvent.objects.filter(operation="hint-edit").exists())

    def test_refund_restores_score_and_allows_redeem(self):
        hint = Hint.objects.create(challenge_id=self._challenge().id, description="d", text="t", cost=20)
        services.redeem(self.user, hint.id)
        self.assertEqual(services.summary(self.user)["points"], -20)
        adminops.refund_hint(self.admin, self.user.id, hint.id)
        self.assertEqual(services.summary(self.user)["points"], 0)
        self.assertEqual(HintRedemption.objects.filter(hint=hint).count(), 0)
        services.redeem(self.user, hint.id)  # may redeem again
        self.assertEqual(services.summary(self.user)["points"], -20)

    def test_delete_hint_rules(self):
        free = Hint.objects.create(challenge_id=self._challenge().id, description="d", text="t", cost=10)
        owned = Hint.objects.create(challenge_id=self._challenge().id, description="d2", text="t2", cost=10)
        services.redeem(self.user, owned.id)
        with self.assertRaises(ServiceError) as error:
            adminops.delete_hint(self.admin, owned.id)
        self.assertEqual(error.exception.status, 409)
        adminops.delete_hint(self.admin, free.id)
        self.assertFalse(Hint.objects.filter(pk=free.id).exists())

    def test_content_lifecycle_and_draft_deletion(self):
        challenge = adminops.create_challenge(
            self.admin, title="Doomed", pool_id=self._pool().id, prompt="Q?", answer="a", points=10
        )
        self.assertEqual(challenge.lifecycle, Challenge.Lifecycle.DRAFT)
        adminops.delete_draft(self.admin, challenge.id)
        self.assertFalse(Challenge.objects.filter(pk=challenge.id).exists())
        # Published challenges archive, never hard-delete.
        published = make_question(["x"])
        with self.assertRaises(ServiceError) as error:
            adminops.delete_draft(self.admin, published.challenge.id)
        self.assertEqual(error.exception.status, 409)
        adminops.set_lifecycle(self.admin, published.challenge.id, Challenge.Lifecycle.ARCHIVED)
        published.challenge.refresh_from_db()
        self.assertEqual(published.challenge.lifecycle, Challenge.Lifecycle.ARCHIVED)
        with self.assertRaises(ServiceError):
            adminops.set_lifecycle(self.admin, 999999, Challenge.Lifecycle.PUBLISHED)
        with self.assertRaises(ServiceError):
            adminops.set_lifecycle(self.admin, published.challenge.id, "bogus")

    def test_add_question_and_revision(self):
        challenge = self._challenge()
        question = adminops.add_question(
            self.admin, challenge.id, prompt="New?", answers=["new"], points=25, display_order=0
        )
        self.assertEqual(Question.objects.filter(challenge=challenge).count(), 1)
        self.assertTrue(AuditEvent.objects.filter(operation="question-add").exists())

    def _pool(self):
        from portal.apps.content.models import Pool

        pool, _ = Pool.objects.get_or_create(name="Ops", slug="ops")
        return pool

    def _challenge(self):
        return Challenge.objects.create(
            title=f"H{uuid.uuid4().hex[:6]}", slug=f"h-{uuid.uuid4().hex[:10]}",
            lifecycle=Challenge.Lifecycle.PUBLISHED,
        )
