"""Ports of the verified prototype rule tests against the production services.

Mirrors ctf-prototype/tests/test_prototype.py semantics where the rules are
the same, on the real PostgreSQL test database.
"""
import threading
from fractions import Fraction

from django.test import TransactionTestCase, TestCase, override_settings
from django.utils import timezone

from portal.apps.accounts.models import ApprovalDecision, User
from portal.apps.content.models import (
    AcceptedAnswer, Challenge, Hint, Pool, PoolChallenge, Question,
)
from portal.apps.submissions import services
from portal.apps.submissions.models import HintRedemption, Solve, Submission
from portal.apps.submissions.services import ServiceError


def make_user(discord_id, name, status=User.Status.APPROVED, is_admin=False):
    user = User.objects.create_user(discord_id, name)
    user.status = status
    user.is_admin = is_admin
    user.save()
    return user


def make_question(answers, points=100, pools=(), user=None):
    challenge = Challenge.objects.create(
        title=f"Challenge {Question.objects.count()}",
        slug=f"challenge-{Question.objects.count() or 0}-{timezone.now().timestamp()}",
        lifecycle=Challenge.Lifecycle.PUBLISHED,
    )
    for pool_name in pools:
        pool, _ = Pool.objects.get_or_create(name=pool_name, defaults={"slug": pool_name})
        PoolChallenge.objects.create(pool=pool, challenge=challenge)
    question = Question.objects.create(challenge=challenge, prompt="Prompt", points=points)
    for answer in answers:
        AcceptedAnswer.objects.create(question=question, value=answer)
    return question


@override_settings(SUBMISSION_COOLDOWN_SECONDS=0)
class SubmissionRuleTests(TestCase):
    def setUp(self):
        self.user = make_user("100000000000000001", "Alex")

    def submit(self, question, value, request_id=None):
        return services.submit(self.user, question.id, value, request_id or f"request-{timezone.now().timestamp()}")

    def test_case_trim_alternatives_and_solved_rejection(self):
        question = make_question(["Ada", "Ada Lovelace"])
        self.assertFalse(self.submit(question, "ada")["correct"])
        self.assertTrue(self.submit(question, " Ada Lovelace ")["correct"])
        with self.assertRaises(ServiceError):
            self.submit(question, "Ada")
        self.assertEqual(
            Submission.objects.filter(user=self.user, question=question, eligible=True).count(), 2
        )

    def test_already_solved_and_duplicate_answer_create_no_attempts(self):
        question = make_question(["CTF{x}"])
        self.assertTrue(self.submit(question, " CTF{x} ")["correct"])
        before = Submission.objects.filter(user=self.user, question=question).count()
        with self.assertRaises(ServiceError):
            self.submit(question, "CTF{y}")
        with self.assertRaises(ServiceError):
            self.submit(question, "CTF{x}")
        self.assertEqual(Submission.objects.filter(user=self.user, question=question).count(), before)
        self.assertEqual(Solve.objects.filter(user=self.user, question=question).count(), 1)

    def test_request_replay_and_reuse_conflict(self):
        question = make_question(["CTF{x}"])
        first = self.submit(question, "wrong", request_id="request-0001")
        replay = self.submit(question, " wrong ", request_id="request-0001")
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["message"], first["message"])
        with self.assertRaises(ServiceError) as error:
            self.submit(question, "different", request_id="request-0001")
        self.assertEqual(error.exception.status, 409)

    def test_cooldown_blocks_counted_attempt(self):
        question = make_question(["CTF{x}"])
        with override_settings(SUBMISSION_COOLDOWN_SECONDS=60):
            self.submit(question, "wrong")
            with self.assertRaises(ServiceError) as error:
                self.submit(question, "CTF{x}")
            self.assertEqual(error.exception.status, 429)
        self.assertEqual(
            Submission.objects.filter(user=self.user, question=question, eligible=True).count(), 1
        )

    def test_approval_gating_and_visibility(self):
        pending = make_user("100000000000000002", "Taylor", status=User.Status.PENDING)
        question = make_question(["x"])
        with self.assertRaises(ServiceError) as error:
            services.submit(pending, question.id, "x", "request-xxxx0001")
        self.assertEqual(error.exception.status, 403)
        draft = make_question(["x"])
        draft.challenge.lifecycle = Challenge.Lifecycle.DRAFT
        draft.challenge.save()
        with self.assertRaises(ServiceError) as error:
            services.submit(self.user, draft.id, "x", "request-xxxx0002")
        self.assertEqual(error.exception.status, 404)
        scheduled = make_question(["x"])
        scheduled.challenge.go_live = timezone.now() + timezone.timedelta(hours=1)
        scheduled.challenge.save()
        with self.assertRaises(ServiceError):
            services.submit(self.user, scheduled.id, "x", "request-xxxx0003")

    def test_hint_redemption_negative_and_retroactive_price(self):
        question = make_question(["x"])
        hint = Hint.objects.create(challenge=question.challenge, description="d", text="t", cost=20)
        services.redeem(self.user, hint.id)
        self.assertEqual(services.summary(self.user)["points"], -20)
        hint.cost = 80
        hint.save()
        self.assertEqual(services.summary(self.user)["points"], -80)
        self.assertEqual(HintRedemption.objects.filter(user=self.user, hint=hint).count(), 1)
        # Redemption unlocks protected text.
        result = services.redeem(self.user, hint.id)
        self.assertEqual(result["text"], "t")

    def test_overlapping_pools_do_not_double_count(self):
        question = make_question(["CTF{x}"], points=100, pools=("Alpha", "Beta"))
        self.assertTrue(self.submit(question, "CTF{x}")["correct"])
        hint = Hint.objects.create(challenge=question.challenge, description="d", text="t", cost=20)
        services.redeem(self.user, hint.id)
        overall = services.summary(self.user)
        self.assertEqual(overall["points"], 80)
        alpha = services.summary(self.user, Pool.objects.get(name="Alpha"))
        self.assertEqual(alpha["points"], 80)
        beta = services.summary(self.user, Pool.objects.get(name="Beta"))
        self.assertEqual(beta["points"], 80)
        self.assertEqual(overall["correct"], 1)

    def test_ranking_rules_and_visibility(self):
        admin = make_user("100000000000000003", "Admin", is_admin=True)
        fast = make_user("100000000000000004", "Sam")
        question = make_question(["CTF{x}"], points=100)
        self.assertTrue(self.submit(question, "CTF{x}")["correct"])  # Alex: 1/1, earliest
        services.submit(fast, question.id, "CTF{x}", "request-xxxx0004")  # Sam: 1/1, later
        hint = Hint.objects.create(challenge=question.challenge, description="d", text="t", cost=10)
        services.redeem(fast, hint.id)  # Sam: 90 points, 1/1 accuracy
        board = services.leaderboard()
        ids = [row["id"] for row in board]
        self.assertEqual(ids[0], self.user.pk)  # Higher points first
        self.assertEqual(board[0]["rank"], 1)
        self.assertIn(admin.pk, ids)  # Admins remain visible
        suspended = make_user("100000000000000005", "Suspended", status=User.Status.SUSPENDED)
        suspended.status = User.Status.SUSPENDED
        suspended.save()
        board = services.leaderboard()
        self.assertNotIn(suspended.pk, [row["id"] for row in board])
        # Zero attempts show accuracy None and rank after solvers.
        idle = make_user("100000000000000006", "Idle")
        board = services.leaderboard()
        idle_row = next(row for row in board if row["id"] == idle.pk)
        self.assertIsNone(idle_row["accuracy"])
        self.assertGreater(idle_row["rank"], 1)

    def test_shared_rank_on_full_tie(self):
        # Full ties share rank. Different last-solve times are NOT a full tie:
        # the third criterion ranks the earlier most-recent solve first (§6).
        a = make_user("100000000000000007", "A")
        b = make_user("100000000000000008", "B")
        question = make_question(["CTF{x}"], points=50)
        services.submit(a, question.id, "CTF{x}", "request-xxxx0007")
        services.submit(b, question.id, "CTF{x}", "request-xxxx0008")
        board = services.leaderboard()
        a_row = next(row for row in board if row["id"] == a.pk)
        b_row = next(row for row in board if row["id"] == b.pk)
        self.assertEqual((a_row["rank"], b_row["rank"]), (1, 2))
        self.assertEqual(a_row["last_solve"], board[0]["last_solve"])
        # Zero-attempt users with no timestamps are a genuine full tie.
        idle_a = make_user("100000000000000014", "IdleA")
        idle_b = make_user("100000000000000015", "IdleB")
        board = services.leaderboard()
        idle_ranks = [row["rank"] for row in board if row["id"] in (idle_a.pk, idle_b.pk)]
        self.assertEqual(len(set(idle_ranks)), 1)  # Zero-attempt users share one rank
        self.assertGreater(idle_ranks[0], 1)  # Behind anyone with a most-recent solve

    def test_exact_accuracy_comparison(self):
        # Equal points: 1/2 accuracy must rank above 1/4 despite any display rounding.
        higher = make_user("100000000000000009", "Higher")
        lower = make_user("100000000000000010", "Lower")
        q1 = make_question(["x1"], points=100)
        q2 = make_question(["x2"], points=0)
        services.submit(higher, q1.id, "x1", "request-xxxx0009")
        services.submit(higher, q2.id, "wrong", "request-xxxx0010")  # 100 pts, 1/2
        wrongs = [make_question(["w1"], points=0), make_question(["w2"], points=0), make_question(["w3"], points=0)]
        services.submit(lower, q1.id, "x1", "request-xxxx0011")  # 100 pts
        for i, question in enumerate(wrongs):
            services.submit(lower, question.id, "wrong", f"request-xxxx0012{i}")  # 1/4
        board = services.leaderboard()
        rows = {row["id"]: row for row in board}
        self.assertEqual(rows[lower.pk]["accuracy"], 25.0)
        self.assertEqual(rows[higher.pk]["points"], rows[lower.pk]["points"])
        self.assertLess(rows[higher.pk]["rank"], rows[lower.pk]["rank"])


class SubmissionConcurrencyTests(TransactionTestCase):
    """Concurrent correct requests cannot award twice (handoff §22.10)."""

    @override_settings(SUBMISSION_COOLDOWN_SECONDS=0)
    def test_concurrent_correct_submissions_award_once(self):
        user = make_user("100000000000000011", "Racer")
        question = make_question(["CTF{x}"])

        results, errors = [], []

        def attempt(index):
            try:
                results.append(services.submit(user, question.id, "CTF{x}", f"race-request-{index}"))
            except ServiceError as error:
                errors.append(error)
            finally:
                from django.db import connections

                connections.close_all()

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results), 1, f"unexpected extra successes: {results}")
        self.assertEqual(len(errors), 4, f"unexpected rejection reasons: {[e.message for e in errors]}")
        self.assertEqual(Solve.objects.filter(user=user, question=question).count(), 1)
        self.assertEqual(
            Submission.objects.filter(user=user, question=question, eligible=True, correct=True).count(), 1
        )
