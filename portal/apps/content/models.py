"""Pools, challenges, questions, and hints (handoff §4, §5, §9, §20).

Rules encoded here:
- Pools are the only grouping concept; overlapping memberships never sum.
- Questions are independently scored; all prompts of a visible challenge are
  immediately visible (no prerequisites).
- Accepted answers are private and versioned; matching stays server-side.
- Hint prices change retroactively: the current cost of a hint applies to all
  existing redemptions, so redemptions store only ownership and time.
- Published content enforces go-live timing; archived content keeps scoring.
"""
from django.conf import settings
from django.db import models


class Pool(models.Model):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.CharField(max_length=500, blank=True)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        db_table = "content_pool"
        ordering = ["order", "id"]

    def __str__(self):
        return self.name


class Challenge(models.Model):
    class Lifecycle(models.TextChoices):
        DRAFT = "draft", "Draft"
        PUBLISHED = "published", "Published"
        ARCHIVED = "archived", "Archived"

    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    summary = models.CharField(max_length=300, blank=True)
    description = models.TextField(blank=True)
    lifecycle = models.CharField(max_length=10, choices=Lifecycle.choices, default=Lifecycle.DRAFT)
    go_live = models.DateTimeField(null=True, blank=True)  # Publication timing, server-enforced.
    lab_template = models.ForeignKey(
        "labs.LabTemplate", null=True, blank=True, on_delete=models.SET_NULL, related_name="challenges"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "content_challenge"

    def __str__(self):
        return self.title

    def visible_at(self, when):
        """Published challenges honor go-live; archived ones stay invisible to browsing
        but keep contributing to scoring."""
        if self.lifecycle == self.Lifecycle.PUBLISHED:
            return self.go_live is None or self.go_live <= when
        return False


class PoolChallenge(models.Model):
    pool = models.ForeignKey(Pool, on_delete=models.CASCADE, related_name="memberships")
    challenge = models.ForeignKey(Challenge, on_delete=models.CASCADE, related_name="memberships")

    class Meta:
        db_table = "content_pool_challenge"
        constraints = [models.UniqueConstraint(fields=("pool", "challenge"), name="uniq_pool_challenge")]


class Attachment(models.Model):
    """Private file served only after server-side approval + visibility checks."""

    challenge = models.ForeignKey(Challenge, on_delete=models.CASCADE, related_name="attachments")
    filename = models.CharField(max_length=255)
    storage_ref = models.CharField(max_length=500)  # Private storage reference; enforce access at view layer.
    size = models.PositiveBigIntegerField(default=0)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "content_attachment"


class Question(models.Model):
    """Independently scored component of a challenge (handoff §5)."""

    challenge = models.ForeignKey(Challenge, on_delete=models.CASCADE, related_name="questions")
    display_order = models.PositiveIntegerField(default=0)
    prompt = models.CharField(max_length=1000)
    input_format = models.CharField(max_length=200, blank=True)  # Displayed format; separate from matching.
    points = models.PositiveBigIntegerField()  # Owner decision: unbounded nonnegative integers.
    generation = models.PositiveBigIntegerField(default=0)  # Replacement history boundary (§7).
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "content_question"
        ordering = ["display_order", "id"]

    def __str__(self):
        return f"{self.challenge_id}:{self.prompt[:40]}"


class AcceptedAnswer(models.Model):
    """Private accepted-answer set with change history (never sent to players)."""

    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="accepted_answers")
    value = models.CharField(max_length=4096)  # Canonical (trimmed) form; matching is case-sensitive.
    active = models.BooleanField(default=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    changed_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "content_accepted_answer"
        constraints = [models.UniqueConstraint(fields=("question", "value"), name="uniq_question_answer")]

    def __str__(self):
        return f"{self.question_id}:{self.value[:30]}"


class Hint(models.Model):
    challenge = models.ForeignKey(Challenge, on_delete=models.CASCADE, related_name="hints")
    description = models.CharField(max_length=500)  # Pre-redemption description, price only.
    text = models.CharField(max_length=4096)  # Protected text, revealed only after redemption.
    cost = models.PositiveBigIntegerField()  # Owner decision: nonnegative integer, unbounded.
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "content_hint"
