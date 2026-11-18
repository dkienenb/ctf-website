"""Submission, hint, and scoring services.

Ports of the behavior verified in the local prototype (see
ctf-prototype/tests/test_prototype.py and the handoff §5–§9):

- Exact case-sensitive matching on trimmed values; alternates award once.
- Repeats, rate-limited requests, already-solved submissions, and transport
  retries of processed requests never create counted attempts.
- Idempotency via per-user request fingerprints with replay + reuse conflict.
- One solve per user/question at the original submission time.
- Scores derive from current question values and current hint prices;
  overlapping pools never double-count the overall score.
- Ranking: points desc, exact-fraction accuracy desc, earlier most-recent
  counted solve asc; missing timestamps sort last; full ties share rank.
- Owner decision 2026-11-14: suspended accounts are hidden from rankings and
  admins remain visible.
"""
import hashlib
import json
from fractions import Fraction

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max, Q, Sum
from django.utils import timezone

from portal.apps.accounts.models import User
from portal.apps.content.models import AcceptedAnswer, Challenge, Hint, Pool, PoolChallenge, Question

from .models import HintRedemption, RequestFingerprint, Solve, Submission


class ServiceError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _visible(question_or_hint_challenge, when=None):
    """Published challenges honor go-live; drafts/archived are unavailable (§4)."""
    challenge = question_or_hint_challenge
    if challenge.lifecycle != Challenge.Lifecycle.PUBLISHED:
        raise ServiceError("Challenge is not available.", 404)
    if challenge.go_live and challenge.go_live > (when or timezone.now()):
        raise ServiceError("Challenge is not available.", 404)


def _assert_approved(user):
    if not user.is_authenticated or not getattr(user, "pk", None):
        raise ServiceError("Sign in first.", 401)
    if user.status != User.Status.APPROVED:
        raise ServiceError("Approved access is required.", 403)


def submit(user, question_id, raw, request_id):
    """Evaluate one answer. Returns {correct, message, replayed?} or raises ServiceError."""
    _assert_approved(user)
    try:
        question = Question.objects.select_related("challenge").get(pk=question_id)
    except Question.DoesNotExist:
        raise ServiceError("Question not found.", 404)
    _visible(question.challenge)

    if not isinstance(raw, str) or not raw.strip() or len(raw) > 4096:
        raise ServiceError("Enter a nonempty answer of at most 4096 characters.")
    if not isinstance(request_id, str) or not 8 <= len(request_id) <= 128:
        raise ServiceError("A request identifier is required.")

    value = raw.strip()
    fingerprint = hashlib.sha256(f"{question.id}\0{value}".encode()).hexdigest()

    with transaction.atomic():
        # Serialize retries of the same request id (§5: retries never duplicate attempts).
        prior = RequestFingerprint.objects.select_for_update().filter(user=user, request_id=request_id).first()
        if prior:
            if prior.fingerprint != fingerprint:
                raise ServiceError("Request identifier reused for different input.", 409)
            return {**json.loads(prior.response), "replayed": True}

        if Submission.objects.filter(user=user, question=question, eligible=True, correct=True).exists():
            raise ServiceError("This question is already solved. No attempt was counted.", 409)
        if Submission.objects.filter(user=user, question=question, eligible=True, answer=value).exists():
            raise ServiceError("You already submitted that answer. No attempt was counted.", 409)

        last = (
            Submission.objects.filter(user=user, question=question, eligible=True)
            .aggregate(latest=Max("submitted_at"))["latest"]
        )
        cooldown = settings.SUBMISSION_COOLDOWN_SECONDS
        if last is not None and (timezone.now() - last).total_seconds() < cooldown:
            raise ServiceError("Submission cooldown active. No attempt was counted.", 429)

        answers = set(
            AcceptedAnswer.objects.filter(question=question, active=True).values_list("value", flat=True)
        )
        correct = value in answers
        response = {
            "correct": correct,
            "message": "Correct. Question solved." if correct else "Incorrect answer. This attempt was counted.",
        }

        try:
            # Savepoint: constraint races (concurrent solves/duplicates) map to
            # rejections, not 500s. One solve per user/question is enforced by
            # uniq_active_solve / uniq_solve_user_question.
            with transaction.atomic():
                submission = Submission.objects.create(
                    user=user, question=question, raw=raw, answer=value,
                    correct=correct, generation=question.generation,
                )
                if correct:
                    Solve.objects.create(user=user, question=question, submission=submission, solved_at=submission.submitted_at)
        except IntegrityError:
            raise ServiceError("You already submitted that answer. No attempt was counted.", 409)

        try:
            with transaction.atomic():
                RequestFingerprint.objects.create(
                    user=user, request_id=request_id, fingerprint=fingerprint,
                    outcome="processed", response=json.dumps(response),
                )
        except IntegrityError:
            prior = RequestFingerprint.objects.filter(user=user, request_id=request_id).first()
            if prior and prior.fingerprint == fingerprint:
                return {**json.loads(prior.response), "replayed": True}
            raise ServiceError("Request identifier reused for different input.", 409)

    return response


def redeem(user, hint_id):
    """Redeem a hint: one ownership per user/hint, immediate deduction, no balance check."""
    _assert_approved(user)
    try:
        hint = Hint.objects.select_related("challenge").get(pk=hint_id)
    except Hint.DoesNotExist:
        raise ServiceError("Hint not found.", 404)
    _visible(hint.challenge)
    HintRedemption.objects.get_or_create(user=user, hint=hint)
    return {
        "text": hint.text,
        "message": "Hint unlocked. Its current price is deducted from your score.",
    }


def _visible_challenges_queryset():
    """The challenge set players can currently see (published + go-live passed)."""
    now = timezone.now()
    return Challenge.objects.filter(lifecycle=Challenge.Lifecycle.PUBLISHED).filter(
        Q(go_live__isnull=True) | Q(go_live__lte=now)
    )


def summary(user, pool=None):
    """Score/accuracy inputs for one user over all challenges or one pool."""
    params = {"user": user}
    question_scope = None
    if pool is not None:
        question_scope = Question.objects.filter(
            challenge__in=PoolChallenge.objects.filter(pool=pool).values("challenge")
        )
    submissions = Submission.objects.filter(user=user, eligible=True)
    if question_scope is not None:
        submissions = submissions.filter(question__in=question_scope)
    attempts = submissions.count()
    correct = submissions.filter(correct=True).count()
    earned = (
        submissions.filter(correct=True)
        .aggregate(total=Sum("question__points"))["total"]
        or 0
    )
    redemptions = HintRedemption.objects.filter(user=user)
    if question_scope is not None:
        redemptions = redemptions.filter(hint__challenge__in=question_scope.values("challenge"))
    deductions = redemptions.aggregate(total=Sum("hint__cost"))["total"] or 0
    last = submissions.filter(correct=True).aggregate(latest=Max("submitted_at"))["latest"]
    # Total possible solves: every question of the currently visible
    # challenges in scope (§4: all questions of a visible challenge count).
    possible_scope = _visible_challenges_queryset()
    if pool is not None:
        possible_scope = possible_scope.filter(
            pk__in=PoolChallenge.objects.filter(pool=pool).values("challenge")
        )
    possible = Question.objects.filter(challenge__in=possible_scope).count()
    return {
        "attempts": attempts,
        "correct": correct,
        "possible": possible,
        "completion": round(correct / possible * 100, 1) if possible else None,
        "earned": earned,
        "deductions": deductions,
        "points": earned - deductions,
        # Display value only; ranking compares exact counts (§6).
        "accuracy": round(correct / attempts * 100, 1) if attempts else None,
        "last_solve": last,
    }


def leaderboard(pool=None):
    """Approved accounts only (owner decision 2026-11-14). Shared ranks on full ties."""
    if pool is not None and not Pool.objects.filter(pk=pool.pk).exists():
        raise ServiceError("Pool not found.", 404)
    users = User.objects.filter(status=User.Status.APPROVED)
    rows = []
    for user in users:
        data = summary(user, pool)
        rows.append({
            "id": user.pk,
            "name": user.cached_name or user.discord_id,
            "avatar": user.cached_avatar,
            **data,
        })

    def key(row):
        accuracy = Fraction(row["correct"], row["attempts"]) if row["attempts"] else Fraction(0)
        # Missing timestamps sort last; fully equal keys share rank.
        last = row["last_solve"].timestamp() if row["last_solve"] is not None else float("inf")
        return (-row["points"], -accuracy, last)

    rows.sort(key=key)
    previous, rank = None, 0
    for index, row in enumerate(rows):
        current = key(row)
        if current != previous:
            rank = index + 1
        row["rank"] = rank
        previous = current
    return rows
