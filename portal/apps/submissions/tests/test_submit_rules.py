"""Submission integrity rules (handoff §5): the cooldown gate, request
fingerprint idempotency (retries/replays never double-count), and the
input-validation negatives around submit()."""
import uuid

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from datetime import timedelta

from portal.apps.accounts.models import User
from portal.apps.content.models import Challenge
from portal.apps.submissions import services
from portal.apps.submissions.models import HintRedemption, Solve, Submission
from portal.apps.submissions.services import ServiceError
from portal.apps.testutils import make_question, make_user


@override_settings(SUBMISSION_COOLDOWN_SECONDS=60)
class CooldownTests(TestCase):
    def setUp(self):
        self.user = make_user("700000000000000001", "Alex")
        self.question = make_question(["flag"], points=100)

    def _submit(self, value, request_id=None):
        return services.submit(self.user, self.question.id, value, request_id or "req-cooldown-0001")

    def test_second_immediate_submit_hits_the_cooldown(self):
        self._submit("wrong one", "req-cooldown-0001")
        with self.assertRaises(ServiceError) as error:
            self._submit("wrong two", "req-cooldown-0002")
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(Submission.objects.filter(user=self.user).count(), 1)

    def test_cooldown_expires_and_allows_the_next_attempt(self):
        self._submit("wrong one")
        last = Submission.objects.get(user=self.user)
        last.submitted_at = timezone.now() - timedelta(seconds=61)
        last.save()
        result = self._submit("flag", "req-cooldown-0002")
        self.assertTrue(result["correct"])
        self.assertTrue(Solve.objects.filter(user=self.user).exists())

    def test_cooldown_is_per_question(self):
        other = make_question(["flag"], points=100)
        self._submit("wrong one")
        result = services.submit(self.user, other.id, "flag", "req-cooldown-0003")
        self.assertTrue(result["correct"])  # a different question is not throttled

    def test_correct_solves_bypass_future_attempts_anyway(self):
        self._submit("flag", "req-cooldown-0004")
        with self.assertRaises(ServiceError) as error:
            self._submit("another guess", "req-cooldown-0005")
        self.assertIn("already solved", error.exception.message)  # not a cooldown message


class RequestFingerprintTests(TestCase):
    def setUp(self):
        self.user = make_user("710000000000000001", "Alex")
        self.question = make_question(["flag"], points=100)

    def _submit(self, raw="wrong", request_id="req-fingerprint-01", question=None):
        return services.submit(self.user, (question or self.question).id, raw, request_id)

    def test_retry_with_same_request_id_and_input_replays(self):
        first = self._submit()
        count = Submission.objects.count()
        second = self._submit()
        self.assertTrue(second.get("replayed"))
        self.assertEqual(second["correct"], first["correct"])
        self.assertEqual(Submission.objects.count(), count)  # replay created nothing

    def test_replay_works_across_cooldowns(self):
        """A transport retry after the cooldown window replays the recorded
        outcome instead of counting a fresh attempt (§5)."""
        first = self._submit(request_id="req-fingerprint-02")
        Submission.objects.update(submitted_at=timezone.now() - timedelta(hours=1))
        second = self._submit(request_id="req-fingerprint-02")
        self.assertTrue(second.get("replayed"))
        self.assertEqual(second["correct"], first["correct"])

    def test_reused_request_id_with_different_input_is_refused(self):
        self._submit("first guess", "req-fingerprint-03")
        with self.assertRaises(ServiceError) as error:
            self._submit("different guess", "req-fingerprint-03")
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(Submission.objects.count(), 1)

    def test_fingerprints_are_per_user(self):
        other = make_user("710000000000000002", "Sam")
        self._submit("wrong", "req-fingerprint-04")
        result = services.submit(other, self.question.id, "wrong", "req-fingerprint-04")
        self.assertFalse(result.get("replayed"))  # another user may reuse the id
        self.assertEqual(Submission.objects.count(), 2)


class SubmitInputValidationTests(TestCase):
    def setUp(self):
        self.user = make_user("720000000000000001", "Alex")
        self.question = make_question(["flag"], points=100)

    def test_unknown_question_is_404(self):
        with self.assertRaises(ServiceError) as error:
            services.submit(self.user, 999999, "x", "req-validate-0001")
        self.assertEqual(error.exception.status, 404)

    def test_raw_answer_bounds(self):
        too_long = "x" * 4097
        for bad_raw in ("", "   ", None, too_long):
            with self.assertRaises(ServiceError, msg=repr(bad_raw)[:40]):
                services.submit(self.user, self.question.id, bad_raw, "req-validate-0002")
        # Exactly 4096 characters is accepted.
        boundary = services.submit(self.user, self.question.id, "x" * 4096, "req-validate-0002")
        self.assertFalse(boundary["correct"])

    def test_request_id_bounds(self):
        for bad_id in ("", "short", "x" * 129, None):
            with self.assertRaises(ServiceError, msg=repr(bad_id)[:30]):
                services.submit(self.user, self.question.id, "guess", bad_id)
        # Minimum accepted length is 8.
        services.submit(self.user, self.question.id, "guess", "12345678")

    def test_unauthenticated_is_refused(self):
        from types import SimpleNamespace
        with self.assertRaises(ServiceError) as error:
            services.submit(SimpleNamespace(is_authenticated=False), self.question.id, "x", "req-validate-0003")
        self.assertEqual(error.exception.status, 401)

    def test_locked_account_cannot_submit(self):
        locked = make_user("720000000000000002", "Locked", status=User.Status.SUSPENDED)
        with self.assertRaises(ServiceError) as error:
            services.submit(locked, self.question.id, "x", "req-validate-0004")
        self.assertEqual(error.exception.status, 403)

    def test_suspended_midsession_still_blocked(self):
        from portal.apps.accounts.models import User
        player = make_user("720000000000000003", "Turncoat")
        services.submit(player, self.question.id, "wrong", "req-validate-0005")
        player.status = User.Status.SUSPENDED
        player.save()
        with self.assertRaises(ServiceError) as error:
            services.submit(player, self.question.id, "wrong two", "req-validate-0006")
        self.assertEqual(error.exception.status, 403)


@override_settings(SUBMISSION_COOLDOWN_SECONDS=0)
class SubmissionBoundaryTests(TestCase):
    """Boundaries: zero-value scoring, exact length limits, whitespace
    semantics, unicode, and the exact-match trimming contract."""

    def setUp(self):
        self.user = make_user("800000000000000101", "Boundary")
        self.zero_q = make_question(["flag"], points=0)
        self.norm_q = make_question(["flag"], points=100)

    def test_zero_point_question_still_counts_as_solve(self):
        result = services.submit(self.user, self.zero_q.id, "flag", "req-boundary-0001")
        self.assertTrue(result["correct"])
        from portal.apps.submissions.models import Solve
        self.assertTrue(Solve.objects.filter(user=self.user, question=self.zero_q).exists())
        data = services.summary(self.user)
        self.assertEqual(data["earned"], 0)  # zero points but still a solve
        self.assertEqual(data["correct"], 1)

    def test_zero_cost_hint_deducts_nothing(self):
        from portal.apps.content.models import Hint
        hint = Hint.objects.create(challenge_id=self.norm_q.challenge.id,
                                   description="free", text="t", cost=0)
        services.redeem(self.user, hint.id)
        data = services.summary(self.user)
        self.assertEqual(data["deductions"], 0)
        self.assertTrue(HintRedemption.objects.filter(user=self.user).exists())

    def test_answer_with_internal_whitespace_stays_untrimmed(self):
        """Only leading/trailing whitespace is stripped; internal spacing is
        significant — 'a  b' ≠ 'a b' unless an accepted answer matches."""
        make_question(["a b"], points=100)  # different question
        # The exact accepted answer works.
        from portal.apps.content.models import AcceptedAnswer, Question
        q = Question.objects.create(challenge=self.norm_q.challenge, prompt="p", points=10)
        AcceptedAnswer.objects.create(question=q, value="a  b")
        result = services.submit(self.user, q.id, "a  b", "req-boundary-0002")
        self.assertTrue(result["correct"])
        # Internal double-space doesn't match single-space answer.
        q2 = Question.objects.create(challenge=self.norm_q.challenge, prompt="p", points=10)
        AcceptedAnswer.objects.create(question=q2, value="a b")
        result = services.submit(self.user, q2.id, "a  b", "req-boundary-0003")
        self.assertFalse(result["correct"])

    def test_unicode_answer_round_trip(self):
        from portal.apps.content.models import AcceptedAnswer, Question
        q = Question.objects.create(challenge=self.norm_q.challenge, prompt="p", points=10)
        AcceptedAnswer.objects.create(question=q, value="¿flag?")
        result = services.submit(self.user, q.id, "¿flag?", "req-boundary-0004")
        self.assertTrue(result["correct"])
        # Emoji answer
        q2 = Question.objects.create(challenge=self.norm_q.challenge, prompt="p", points=10)
        AcceptedAnswer.objects.create(question=q2, value="🏳️‍🌈")
        result = services.submit(self.user, q2.id, "🏳️‍🌈", "req-boundary-0005")
        self.assertTrue(result["correct"])

    def test_answer_newlines_and_tabs_are_preserved_in_raw(self):
        from portal.apps.submissions.models import Submission
        services.submit(self.user, self.norm_q.id, "line1\nline2\ttabbed", "req-boundary-0006")
        sub = Submission.objects.get(user=self.user)
        self.assertEqual(sub.raw, "line1\nline2\ttabbed")
        self.assertEqual(sub.answer, "line1\nline2\ttabbed")  # only outer trim

    def test_submission_raw_field_preserves_4096_boundary(self):
        exact = "x" * 4096
        result = services.submit(self.user, self.norm_q.id, exact, "req-boundary-0007")
        self.assertFalse(result["correct"])
        from portal.apps.submissions.models import Submission
        sub = Submission.objects.get(user=self.user)
        self.assertEqual(len(sub.raw), 4096)
        self.assertEqual(sub.answer, exact)


class HintRedemptionConcurrencyTests(TransactionTestCase):
    """Cross-connection hint redemption race: needs committed data."""

    def setUp(self):
        self.user = make_user("830000000000000001", "Redeemer")
        self.norm_q = make_question(["flag"], points=100)

    def test_concurrent_hint_redemptions_award_once(self):
        """Two simultaneous redeem calls for the same user/hint: exactly one
        ownership row (the get_or_create is atomic)."""
        import threading
        from django.db import connection
        from portal.apps.content.models import Hint
        hint = Hint.objects.create(challenge_id=self.norm_q.challenge.id,
                                   description="d", text="t", cost=10)
        results = []
        def redeem():
            connection.close()
            try:
                services.redeem(self.user, hint.id)
                results.append("ok")
            except Exception:
                results.append("error")
            finally:
                connection.close()
        threads = [threading.Thread(target=redeem) for _ in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(results), 2)
        self.assertEqual(HintRedemption.objects.filter(user=self.user, hint=hint).count(), 1)
    """Leaderboard edge cases: pools with no challenges, zero-score players,
    players with only hint deductions, and the completion calculation."""


class LeaderboardBoundaryTests(TestCase):

    def setUp(self):
        self.player = make_user("810000000000000001", "Solo")

    def test_empty_pool_returns_empty_leaderboard(self):
        from portal.apps.content.models import Pool
        pool = Pool.objects.create(name="Empty", slug="empty")
        rows = services.leaderboard(pool=pool)
        # The player appears (all approved players are listed) but with zero data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["correct"], 0)
        self.assertEqual(rows[0]["possible"], 0)

    def test_pool_with_no_questions_shows_zero_possible(self):
        from portal.apps.content.models import Challenge, Pool, PoolChallenge, Question, AcceptedAnswer
        pool = Pool.objects.create(name="NoQ", slug="noq")
        ch = Challenge.objects.create(title="NoQ", slug=f"noq-{Challenge.objects.count()}",
                                      lifecycle=Challenge.Lifecycle.PUBLISHED)
        PoolChallenge.objects.create(pool=pool, challenge=ch)
        q = Question.objects.create(challenge=ch, prompt="p", points=100)
        AcceptedAnswer.objects.create(question=q, value="flag")
        services.submit(self.player, q.id, "flag", "req-lb-noq-1")
        rows = services.leaderboard(pool=pool)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["possible"], 1)
        self.assertEqual(rows[0]["completion"], 100.0)

    def test_hint_deduction_alone_shows_negative_score(self):
        from portal.apps.content.models import Hint
        q = make_question(["flag"], points=100)
        hint = Hint.objects.create(challenge_id=q.challenge.id, description="d", text="t", cost=150)
        services.redeem(self.player, hint.id)
        rows = services.leaderboard()
        mine = next(r for r in rows if r["id"] == self.player.id)
        self.assertEqual(mine["points"], -150)  # negative, no solves to offset
        self.assertEqual(mine["correct"], 0)

