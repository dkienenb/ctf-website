"""Operator service and page-flow tests (approvals, hints, content, manage pages)."""
import io
import uuid

from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from portal.apps.accounts.models import ApprovalDecision, User
from portal.apps.content.models import AcceptedAnswer, Challenge, Hint, Pool, Question
from portal.apps.submissions import adminops, services
from portal.apps.labs.models import LabInstance, QueueEntry
from portal.apps.submissions.models import (
    AuditEvent, DataRevision, HintRedemption, Notification, Solve, Submission,
)
from portal.apps.submissions.services import ServiceError

from portal.apps.testutils import make_question, make_user


@override_settings(SUBMISSION_COOLDOWN_SECONDS=0)
class OperatorServiceTests(TestCase):
    def test_preapprove_page_flow_upgrades_pending_accounts(self):
        """The dashboard's 'Pre-approve ID' form: ledger first, no account
        required; a pending account with that ID is upgraded immediately."""
        pending = make_user("480000000000000001", "Early", status=User.Status.PENDING)
        self.client.force_login(self.admin)
        response = self.client.post(reverse("manage-preapprove"), {"discord_id": pending.discord_id})
        self.assertEqual(response.status_code, 302)
        pending.refresh_from_db()
        self.assertEqual(pending.status, User.Status.APPROVED)
        self.assertTrue(ApprovalDecision.objects.filter(
            discord_id=pending.discord_id, decision="preapprove").exists())
        # Pre-approving an ID with no account yet leaves only the ledger.
        response = self.client.post(reverse("manage-preapprove"), {"discord_id": "481111111111111111"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(User.objects.filter(discord_id="481111111111111111").exists())

    def test_preapprove_page_refuses_bad_ids(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("manage-preapprove"), {"discord_id": "abc"})
        self.assertEqual(response.status_code, 302)  # error toast, nothing created
        self.assertFalse(ApprovalDecision.objects.filter(decision="preapprove").exists())

    def test_dashboard_shows_preapproved_ids_with_state(self):
        """The operator sees existing pre-approvals: the ID, when it was
        pre-approved, and whether an account has used it yet."""
        self.client.force_login(self.admin)
        adminops.preapprove(self.admin, "470000000000000001")  # no account yet
        pending = make_user("470000000000000002", "Waiter", status=User.Status.PENDING)
        adminops.preapprove(self.admin, pending.discord_id)   # upgrades the pending account
        used = make_user("470000000000000003", "Already In")   # approved before the record
        adminops.preapprove(self.admin, used.discord_id)

        response = self.client.get(reverse("manage"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "470000000000000001")
        self.assertContains(response, "470000000000000002")
        self.assertContains(response, "awaiting first sign-in")
        # Pre-approving an existing account upgrades it immediately, so the
        # badge reads approved (there is no lingering "pending" state).
        self.assertContains(response, "account: approved")
        self.assertContains(response, "by Admin")  # the operator who pre-approved

    def test_dashboard_empty_preapproval_list(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("manage")), "No pre-approved IDs yet.")

    def test_challenge_create_page_supports_multiple_pools_and_author(self):
        self.client.force_login(self.admin)
        pool_a = Pool.objects.create(name="Pool A", slug="pool-a")
        pool_b = Pool.objects.create(name="Pool B", slug="pool-b")
        response = self.client.post(reverse("manage-create-challenge"), {
            "title": "Dual pool", "author": "Dana",
            "pools": [pool_a.id, pool_b.id],
            "questions-TOTAL_FORMS": "1", "questions-INITIAL_FORMS": "0",
            "questions-MIN_NUM_FORMS": "0", "questions-MAX_NUM_FORMS": "1000",
            "questions-0-prompt": "q?", "questions-0-answers": "a", "questions-0-points": "10",
        })
        self.assertEqual(response.status_code, 302)
        challenge = Challenge.objects.get(title="Dual pool")
        self.assertEqual(challenge.author, "Dana")
        self.assertEqual(
            set(challenge.memberships.values_list("pool__slug", flat=True)),
            {"pool-a", "pool-b"},
        )
        # And the player sees the credit on the challenge page (published first).
        adminops.set_lifecycle(self.admin, challenge.id, Challenge.Lifecycle.PUBLISHED)
        detail = self.client.get(reverse("challenge_detail", args=[challenge.slug]))
        self.assertContains(detail, "by Dana")

    def test_challenge_create_page_requires_a_pool(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("manage-create-challenge"), {
            "title": "No pools", "pools": [],
            "questions-TOTAL_FORMS": "1", "questions-INITIAL_FORMS": "0",
            "questions-MIN_NUM_FORMS": "0", "questions-MAX_NUM_FORMS": "1000",
            "questions-0-prompt": "q?", "questions-0-answers": "a", "questions-0-points": "10",
        })
        self.assertEqual(response.status_code, 200)  # form error re-rendered
        self.assertFalse(Challenge.objects.exists())

    def test_content_edit_updates_author(self):
        self.client.force_login(self.admin)
        challenge = Challenge.objects.create(title="Authored", slug="authored-1",
                                             lifecycle=Challenge.Lifecycle.PUBLISHED)
        pool = Pool.objects.create(name="AP", slug="ap")
        response = self.client.post(reverse("manage-content-edit", args=[challenge.id]), {
            "title": "Authored", "author": "New Credit", "summary": "", "description": "",
            "pools": [pool.id], "lifecycle": "published",
            "revision": DataRevision.current(),
        })
        self.assertEqual(response.status_code, 302)
        challenge.refresh_from_db()
        self.assertEqual(challenge.author, "New Credit")

    def test_inline_question_edit_applies_through_the_page(self):
        """The challenge edit page hosts a compact editor per question; it
        applies through the corrections engine (audit, revision guard)."""
        self.client.force_login(self.admin)
        challenge = Challenge.objects.create(title="Inline", slug="inline-1",
                                             lifecycle=Challenge.Lifecycle.PUBLISHED)
        question = Question.objects.create(challenge=challenge, prompt="Old?", points=10,
                                           display_order=0)
        AcceptedAnswer.objects.create(question=question, value="old")
        response = self.client.post(reverse("manage-question-inline", args=[question.id]), {
            "prompt": "New?", "answers": "new1\nnew2", "points": "25",
            "display_order": "0", "input_format": "",
            "mode": "none", "reason": "fix typo", "revision": DataRevision.current(),
        })
        self.assertEqual(response.status_code, 302)
        question.refresh_from_db()
        self.assertEqual(question.prompt, "New?")
        self.assertEqual(question.points, 25)
        # Mode "none" (corrected) ADDS alternates and keeps the old ones —
        # the corrections engine's documented semantics.
        self.assertEqual(list(question.accepted_answers.values_list("value", flat=True)),
                         ["old", "new1", "new2"])
        self.assertTrue(AuditEvent.objects.filter(operation="question-edit").exists())

    def test_inline_question_edit_requires_reason_and_current_revision(self):
        self.client.force_login(self.admin)
        challenge = Challenge.objects.create(title="Inline2", slug="inline-2",
                                             lifecycle=Challenge.Lifecycle.PUBLISHED)
        question = Question.objects.create(challenge=challenge, prompt="Old?", points=10)
        base = {"prompt": "New?", "answers": "a", "points": "10", "display_order": "0",
                "input_format": "", "mode": "none"}
        # Empty audit reason refused.
        response = self.client.post(reverse("manage-question-inline", args=[question.id]),
                                    {**base, "reason": "", "revision": DataRevision.current()})
        self.assertEqual(response.status_code, 302)
        question.refresh_from_db()
        self.assertEqual(question.prompt, "Old?")  # nothing applied
        # Stale revision refused.
        response = self.client.post(reverse("manage-question-inline", args=[question.id]),
                                    {**base, "reason": "late", "revision": DataRevision.current() - 1})
        self.assertEqual(response.status_code, 302)
        question.refresh_from_db()
        self.assertEqual(question.prompt, "Old?")

    def test_content_edit_page_renders_inline_question_forms(self):
        self.client.force_login(self.admin)
        challenge = Challenge.objects.create(title="Inline3", slug="inline-3",
                                             lifecycle=Challenge.Lifecycle.PUBLISHED)
        Question.objects.create(challenge=challenge, prompt="Q?", points=10)
        response = self.client.get(reverse("manage-content-edit", args=[challenge.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Apply question edit")
        self.assertContains(response, "/inline")

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

    def test_challenge_create_page_and_flow(self):
        """Creation lives on its own labeled page; the dashboard links it;
        the question formset supports several questions and alternate answers."""
        self.client.force_login(self.admin)
        response = self.client.get(reverse("manage"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("manage-create-challenge"))

        response = self.client.get(reverse("manage-create-challenge"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<label")
        self.assertContains(response, "Accepted answers")
        self.assertContains(response, "invisible to players until you publish")
        # The dynamic-question affordances are present: empty_form template,
        # add button, and management form.
        self.assertContains(response, "question-template")
        self.assertContains(response, "add-question")
        self.assertContains(response, 'name="questions-TOTAL_FORMS"')

        pool = Pool.objects.create(name="Labeled pool", slug="labeled-pool")
        response = self.client.post(reverse("manage-create-challenge"), {
            "title": "Labeled challenge", "pools": [pool.id],
            # Two questions; the third row is blank and must be skipped.
            "questions-TOTAL_FORMS": "3", "questions-INITIAL_FORMS": "0",
            "questions-MIN_NUM_FORMS": "0", "questions-MAX_NUM_FORMS": "1000",
            "questions-0-prompt": "one plus one?",
            "questions-0-answers": "2\n  two ",   # whitespace lines are dropped
            "questions-0-points": "50",
            "questions-1-prompt": "capital of France?",
            "questions-1-answers": "paris\nParis",
            "questions-1-points": "10",
            "questions-2-prompt": "",
            "questions-2-answers": "",
        })
        self.assertEqual(response.status_code, 302)
        challenge = Challenge.objects.get(title="Labeled challenge")
        self.assertEqual(challenge.lifecycle, Challenge.Lifecycle.DRAFT)
        questions = list(challenge.questions.order_by("display_order"))
        self.assertEqual(len(questions), 2)
        self.assertEqual([q.points for q in questions], [50, 10])
        self.assertEqual(list(questions[0].accepted_answers.values_list("value", flat=True)), ["2", "two"])
        self.assertEqual(list(questions[1].accepted_answers.values_list("value", flat=True)), ["paris", "Paris"])

    def test_challenge_create_page_all_blank_questions_is_refused(self):
        self.client.force_login(self.admin)
        pool = Pool.objects.create(name="P", slug="p-x")
        response = self.client.post(reverse("manage-create-challenge"), {
            "title": "Empty", "pools": [pool.id],
            "questions-TOTAL_FORMS": "1", "questions-INITIAL_FORMS": "0",
            "questions-MIN_NUM_FORMS": "0", "questions-MAX_NUM_FORMS": "1000",
            "questions-0-prompt": "", "questions-0-answers": "",
        })
        self.assertEqual(response.status_code, 200)  # re-rendered with the error
        self.assertFalse(Challenge.objects.exists())
        self.assertContains(response, "question")

    def test_challenge_create_partial_row_is_an_error(self):
        """A row with content but a missing field is an error, not a silent skip."""
        self.client.force_login(self.admin)
        pool = Pool.objects.create(name="P2", slug="p-2")
        response = self.client.post(reverse("manage-create-challenge"), {
            "title": "Partial", "pools": [pool.id],
            "questions-TOTAL_FORMS": "2", "questions-INITIAL_FORMS": "0",
            "questions-MIN_NUM_FORMS": "0", "questions-MAX_NUM_FORMS": "1000",
            "questions-0-prompt": "q?", "questions-0-answers": "a", "questions-0-points": "10",
            "questions-1-prompt": "", "questions-1-answers": "orphan answer",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Prompt is required.")
        self.assertFalse(Challenge.objects.exists())

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
        adminops.apply_hint_edit(self.admin, hint, cost=80, description="d", text="t",
                                 reason="repricing", expected_revision=preview["revision"])
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
            self.admin, title="Doomed", pool_ids=[self._pool().id],
            questions=[{"prompt": "Q?", "answers": ["a"], "points": 10}],
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
