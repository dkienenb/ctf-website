"""Ports of the prototype correction/regrade tests (handoff §7–§8)."""
from django.test import TestCase, override_settings
from django.utils import timezone

from portal.apps.content.models import AcceptedAnswer, Question
from portal.apps.submissions import corrections, services
from portal.apps.submissions.models import AuditEvent, DataRevision, Notification, Solve, Submission
from portal.apps.submissions.services import ServiceError

from .tests import make_question, make_user


def _edit(question, answers, mode="none", points=None, expected_revision=None, reason="test correction"):
    return corrections.apply_question_edit(
        None, question,
        answers=answers,
        points=question.points if points is None else points,
        mode=mode,
        reason=reason,
        expected_revision=expected_revision if expected_revision is not None else DataRevision.current(),
    )


def _active(question):
    return list(
        Submission.objects.filter(question=question, eligible=True).order_by("submitted_at")
    )


@override_settings(SUBMISSION_COOLDOWN_SECONDS=0)
class CorrectionRuleTests(TestCase):
    def setUp(self):
        self.actor = make_user("300000000000000001", "Admin", is_admin=True)
        self.user = make_user("300000000000000002", "Alex")

    def submit(self, question, value):
        return services.submit(self.user, question.id, value, f"request-{value}-{timezone.now().timestamp()}")

    def test_regrade_earliest_original_time_and_removal_replay(self):
        question = make_question(["Ada"], points=50)
        self.submit(question, "A")
        self.submit(question, "B")
        self.submit(question, "Ada")
        # Fix deterministic timestamps: A=120s ago (earliest), B=110s, Ada=100s.
        rows = list(Submission.objects.filter(question=question).order_by("submitted_at"))
        for row, seconds in zip(rows, (120, 110, 100)):
            row.submitted_at = timezone.now() - timezone.timedelta(seconds=seconds)
            row.save(update_fields=("submitted_at",))
        Solve.objects.get(user=self.user, question=question).delete()
        solve_row = rows[2]  # seeded solve referenced 'Ada'; re-point to it for realism
        Solve.objects.create(user=self.user, question=question, submission=solve_row, solved_at=solve_row.submitted_at)

        # Add 'A' as accepted: earliest now-correct submission keeps its original time.
        _edit(question, ["A", "Ada"])
        active = _active(question)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].answer, "A")
        self.assertAlmostEqual((timezone.now() - active[0].submitted_at).total_seconds(), 120, delta=2)
        self.assertEqual(Solve.objects.get(user=self.user, question=question).submission.answer, "A")

        # Remove 'A' again: correction-pruned records replay within this generation.
        _edit(question, ["Ada"])
        active = _active(question)
        self.assertEqual(len(active), 3)  # A, B active incorrect again; Ada is the solve
        solve = Solve.objects.get(user=self.user, question=question)
        self.assertEqual(solve.submission.answer, "Ada")
        self.assertAlmostEqual((timezone.now() - solve.solved_at).total_seconds(), 100, delta=2)

    def test_edit_modes_messages_and_replacement_boundary(self):
        question = make_question(["Ada"], points=50)
        self.submit(question, "wrong")
        self.submit(question, "Ada")

        _edit(question, ["Ada"], mode="incorrect")
        self.assertEqual(len(_active(question)), 1)
        self.assertTrue(Notification.objects.filter(recipient=self.user, message="Question updated").exists())

        # Deliberately cleared incorrect history is never resurrected.
        _edit(question, ["wrong", "Ada"])
        self.assertEqual(len(_active(question)), 1)
        self.assertTrue(Notification.objects.filter(recipient=self.user, message="Question corrected").exists())

        _edit(question, ["Ada"], mode="all")
        self.assertEqual(len(_active(question)), 0)
        question.refresh_from_db()
        self.assertEqual(question.generation, 1)
        self.assertFalse(Solve.objects.filter(user=self.user, question=question).exists())
        self.assertTrue(Notification.objects.filter(recipient=self.user, message="Question replaced").exists())

        # The old generation never reappears.
        _edit(question, ["Ada", "wrong"])
        self.assertEqual(len(_active(question)), 0)

        # The question is open again in the new generation.
        self.submit(question, "Ada")
        self.assertEqual(len(_active(question)), 1)
        self.assertTrue(Solve.objects.filter(user=self.user, question=question).exists())

    def test_replacement_discards_credit_and_recovers_openness(self):
        question = make_question(["CTF{x}"], points=100)
        self.assertTrue(self.submit(question, "CTF{x}")["correct"])
        before = services.summary(self.user)
        self.assertEqual(before["points"], 100)
        _edit(question, ["CTF{x}"], mode="all")
        after = services.summary(self.user)
        self.assertEqual(after["points"], 0)
        self.assertEqual(after["attempts"], 0)

    def test_preview_matches_apply_and_stale_apply_rejected(self):
        question = make_question(["CTF{x}"], points=100)
        self.assertTrue(self.submit(question, "CTF{x}")["correct"])
        before = services.summary(self.user)

        preview = corrections.preview_question_edit(question, answers=["CTF{x}"], points=20, mode="none")
        self.assertEqual(preview["effects"], [{
            "user_id": self.user.id,
            "attempts_before": 1, "attempts_after": 1,
            "correct_before": 1, "correct_after": 1,
            "earned_before": 100, "earned_after": 20,
            "score_delta": -80,
        }])
        self.assertEqual(services.summary(self.user), before)  # Preview mutated nothing
        self.assertEqual(preview["active_attempts_after"], 1)

        # An intervening data change invalidates the preview revision.
        stale_revision = preview["revision"]
        DataRevision.bump()
        with self.assertRaises(ServiceError) as error:
            _edit(question, ["CTF{x}"], points=20, expected_revision=stale_revision)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(services.summary(self.user), before)

        # Fresh preview applies cleanly.
        preview = corrections.preview_question_edit(question, answers=["CTF{x}"], points=20, mode="none")
        result = _edit(question, ["CTF{x}"], points=20, expected_revision=preview["revision"])
        self.assertEqual(services.summary(self.user)["points"], 20)
        self.assertIn("Question corrected", result["message"])
        self.assertTrue(AuditEvent.objects.filter(operation="question-edit").exists())

    def test_duplicate_answer_replay_prunes_duplicates(self):
        """Defensive planner behavior: replaying two same-answer rows keeps one
        active attempt and prunes the later duplicate. Unreachable through
        normal submissions (submit-time dedupe blocks it), but protects
        imported or legacy histories from uniq_active_answer violations."""
        user = self.user
        first = Submission(user=user, question=None, raw="X", answer="X", correct=False, generation=0, submitted_at=timezone.now() - timezone.timedelta(seconds=30))
        second = Submission(user=user, question=None, raw="X ", answer="X", correct=False, generation=0, submitted_at=timezone.now() - timezone.timedelta(seconds=20))
        first.id, second.id = 900001, 900002
        changes, solve_for, users = corrections._plan([first, second], ["Ada"], "none", 0)
        self.assertEqual(changes[first.id], (True, "", False))  # earliest duplicate stays a counted attempt
        self.assertEqual(changes[second.id], (False, Submission.Exclusion.DUPLICATE, False))
        self.assertEqual(solve_for, {})
        self.assertEqual(users, {user.id})

    def test_validation_rejects_bad_bodies(self):
        question = make_question(["Ada"])
        with self.assertRaises(ServiceError):
            _edit(question, [])
        with self.assertRaises(ServiceError):
            _edit(question, ["   "])
        with self.assertRaises(ServiceError):
            corrections.preview_question_edit(question, answers=["Ada"], points=-1, mode="none")
        with self.assertRaises(ServiceError):
            corrections.preview_question_edit(question, answers=["Ada"], points=1, mode="unknown")
        with self.assertRaises(ServiceError):
            corrections.preview_question_edit(question, answers=["Ada"], points=1, mode="all", display_order=-2)

    def test_accepted_answer_history_is_retained(self):
        question = make_question(["Ada"], points=50)
        _edit(question, ["Ada", "A"])
        _edit(question, ["Ada"])
        values = list(AcceptedAnswer.objects.filter(question=question).values_list("value", "active"))
        self.assertIn(("A", False), values)
        self.assertIn(("Ada", True), values)
