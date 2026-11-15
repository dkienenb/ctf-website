"""Submission integrity rules (handoff §5): the cooldown gate, request
fingerprint idempotency (retries/replays never double-count), and the
input-validation negatives around submit()."""
from django.test import TestCase, override_settings
from django.utils import timezone
from datetime import timedelta

from portal.apps.accounts.models import User
from portal.apps.submissions import services
from portal.apps.submissions.models import Solve, Submission
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
