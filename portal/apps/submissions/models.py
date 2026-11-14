"""Submissions, solves, redemptions, scoring inputs, audit (handoff §5–§9, §20).

Database-level invariants carried over from the verified prototype:
- One solve per user/question, referencing the first eligible correct submission.
- A repeat of an answer already in the user's active history cannot create an
  attempt (partial unique index on trimmed answer within the current
  eligibility set).
- Rejected/duplicate/rate-limited requests are recorded as request
  fingerprints for idempotent replay, never as counted attempts.
- Excluded submissions are never deleted; they keep an explicit exclusion
  reason (replacement / incorrect-clear / correction-pruned / generation).
- Hint redemptions are unique per user/hint; deductions always derive from the
  hint's current price.
"""
from django.conf import settings
from django.db import models


class Submission(models.Model):
    """Every counted attempt; audit-only rows keep their exclusion reason."""

    class Exclusion(models.TextChoices):
        ACTIVE = "", "Active (counted)"
        REPLACEMENT = "replacement", "Excluded by question replacement"
        INCORRECT_CLEAR = "incorrect-clear", "Cleared as incorrect attempt"
        CORRECTION_PRUNED = "correction-pruned", "Pruned after an earlier corrected solve"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="submissions")
    question = models.ForeignKey("content.Question", on_delete=models.PROTECT, related_name="submissions")
    raw = models.CharField(max_length=4096)  # Original submitted text, kept privately for audit.
    answer = models.CharField(max_length=4096)  # Trimmed comparison value.
    submitted_at = models.DateTimeField(auto_now_add=True)
    correct = models.BooleanField(default=False)  # Evaluated against the generation's answer set at insert time.
    eligible = models.BooleanField(default=True)
    exclusion = models.CharField(max_length=20, choices=Exclusion.choices, blank=True, default="")
    generation = models.PositiveBigIntegerField()

    class Meta:
        db_table = "submissions_submission"
        indexes = [
            models.Index(fields=["user", "question"]),
            models.Index(fields=["eligible", "correct"]),
        ]
        constraints = [
            # A repeat of an answer already active for this user/question cannot
            # create another attempt (case-sensitive, trimmed comparison).
            models.UniqueConstraint(
                fields=("user", "question", "answer"),
                condition=models.Q(eligible=True),
                name="uniq_active_answer",
            ),
            # At most one correct active submission per user/question.
            models.UniqueConstraint(
                fields=("user", "question"),
                condition=models.Q(eligible=True, correct=True),
                name="uniq_active_solve",
            ),
        ]


class Solve(models.Model):
    """First eligible correct submission per user/question, with its original time."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="solves")
    question = models.ForeignKey("content.Question", on_delete=models.PROTECT, related_name="solves")
    submission = models.OneToOneField(Submission, on_delete=models.PROTECT, related_name="solve")
    solved_at = models.DateTimeField()  # Original submission time, never the correction time.

    class Meta:
        db_table = "submissions_solve"
        constraints = [models.UniqueConstraint(fields=("user", "question"), name="uniq_solve_user_question")]


class HintRedemption(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="redemptions")
    hint = models.ForeignKey("content.Hint", on_delete=models.PROTECT, related_name="redemptions")
    redeemed_at = models.DateTimeField(auto_now_add=True)
    # Deduction is always the hint's CURRENT price (retroactive pricing, §9);
    # historical prices are reconstructible from the audit log.

    class Meta:
        db_table = "submissions_hint_redemption"
        constraints = [models.UniqueConstraint(fields=("user", "hint"), name="uniq_redemption_user_hint")]


class RequestFingerprint(models.Model):
    """Idempotency: replay of a processed request returns its original outcome."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    request_id = models.CharField(max_length=128)
    fingerprint = models.CharField(max_length=64)  # Hash of question + trimmed value.
    outcome = models.CharField(max_length=20)  # processed / duplicate-answer / rate-limited / already-solved
    response = models.CharField(max_length=2000, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "submissions_request_fingerprint"
        constraints = [models.UniqueConstraint(fields=("user", "request_id"), name="uniq_request_id")]


class AuditEvent(models.Model):
    """Actor, operation, reason, and payload for administrative changes."""

    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="audit_events")
    operation = models.CharField(max_length=50)
    reason = models.CharField(max_length=500, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "submissions_audit_event"
        indexes = [models.Index(fields=["operation", "created_at"])]


class Notification(models.Model):
    recipient = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    message = models.CharField(max_length=500)
    read_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "submissions_notification"
        ordering = ["-created_at"]
