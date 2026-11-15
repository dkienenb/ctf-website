"""Operator services: approvals, hint management, content authoring (handoff §3, §4, §9).

Carries the recorded owner decisions (2026-11-14):
- Reject + reapply: only pending accounts can be rejected.
- Hint refunds remove ownership and return the current price; redeemed hints
  are never deletable; unredeemed hints may be deleted.
- Published/scheduled content is archived, never hard-deleted; drafts without
  submissions may be deleted.
- Content edits use the same DataRevision guard as corrections.
- Suspension revokes desktop access, cancels queued requests, saves active
  labs, and retains progress.
"""
from django.db import transaction
from django.utils import timezone

from portal.apps.accounts.models import ApprovalDecision, User
from portal.apps.content.models import AcceptedAnswer, Challenge, Hint, Pool, PoolChallenge, Question

from portal.apps.labs.models import LabInstance, QueueEntry
from .models import AuditEvent, DataRevision, HintRedemption, Notification, Submission
from .services import ServiceError


def _assert_admin(actor):
    if actor is None or not getattr(actor, "is_admin", False):
        raise ServiceError("Administrator access is required.", 403)


def _audit(actor, operation, reason, payload):
    AuditEvent.objects.create(actor=actor, operation=operation, reason=reason, payload=payload)


def approve_user(actor, user_id):
    _assert_admin(actor)
    try:
        target = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        raise ServiceError("Account not found.", 404)
    target.status = User.Status.APPROVED
    target.save(update_fields=("status", "updated_at"))
    ApprovalDecision.objects.create(discord_id=target.discord_id, decision="approve", actor=actor)
    Notification.objects.create(
        recipient=target,
        message="Your account was approved. Challenges, files, hints, rankings, and labs are now available.",
    )
    _audit(actor, "approve", "", {"user": target.id})
    DataRevision.bump()


def reject_user(actor, user_id):
    _assert_admin(actor)
    try:
        target = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        raise ServiceError("Account not found.", 404)
    if target.status != User.Status.PENDING:
        raise ServiceError("Only pending accounts can be rejected.", 409)
    target.status = User.Status.REJECTED
    target.save(update_fields=("status", "updated_at"))
    ApprovalDecision.objects.create(discord_id=target.discord_id, decision="reject", actor=actor)
    Notification.objects.create(
        recipient=target,
        message="Your application was rejected. Sign in again to submit a new application. Any progress is retained.",
    )
    _audit(actor, "reject", "", {"user": target.id})
    DataRevision.bump()


def suspend_user(actor, user_id):
    _assert_admin(actor)
    try:
        target = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        raise ServiceError("Account not found.", 404)
    if target.id == actor.id:
        raise ServiceError("Administrators cannot suspend their own account.", 409)
    target.status = User.Status.SUSPENDED
    target.save(update_fields=("status", "updated_at"))
    QueueEntry.objects.filter(user=target, active=True).update(active=False)
    for lab in LabInstance.objects.filter(user=target).exclude(state="saved"):
        lab.state = "saved"
        lab.save(update_fields=("state", "updated_at"))
        lab.leases.filter(active=True).update(active=False)
    ApprovalDecision.objects.create(discord_id=target.discord_id, decision="suspend", actor=actor)
    Notification.objects.create(
        recipient=target,
        message="Your account was suspended. Desktop access was revoked; your progress and saved lab state are retained.",
    )
    _audit(actor, "suspend", "", {"user": target.id})
    DataRevision.bump()


def reinstate_user(actor, user_id):
    """Unsuspend: restore approval; progress, ownership, and the saved lab are
    retained (the user resumes it through the normal flow)."""
    _assert_admin(actor)
    try:
        target = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        raise ServiceError("Account not found.", 404)
    if target.status != User.Status.SUSPENDED:
        raise ServiceError("Only suspended accounts can be reinstated.", 409)
    target.status = User.Status.APPROVED
    target.save(update_fields=("status", "updated_at"))
    ApprovalDecision.objects.create(discord_id=target.discord_id, decision="reinstate", actor=actor)
    Notification.objects.create(
        recipient=target,
        message="Your account was reinstated. Desktop access is available again; your saved lab and progress are intact.",
    )
    _audit(actor, "reinstate", "", {"user": target.id})
    DataRevision.bump()


def preapprove(actor, discord_id):
    _assert_admin(actor)
    if not isinstance(discord_id, str) or not discord_id.isdigit() or not 5 <= len(discord_id) <= 25:
        raise ServiceError("Enter a 5–25 digit Discord ID.")
    ApprovalDecision.objects.create(discord_id=discord_id, decision="preapprove", actor=actor)
    User.objects.filter(discord_id=discord_id, status=User.Status.PENDING).update(status=User.Status.APPROVED)
    _audit(actor, "preapprove", "", {"discord_id": discord_id})
    DataRevision.bump()


def _validate_hint_edit(cost, description, text):
    if isinstance(cost, bool) or not isinstance(cost, int) or cost < 0:
        raise ServiceError("Use a nonnegative integer hint price.")
    if not isinstance(description, str) or not description.strip():
        raise ServiceError("Hint description is required.")
    if not isinstance(text, str) or not text.strip():
        raise ServiceError("Protected hint text is required.")


def _validate_reason(reason):
    if not isinstance(reason, str) or not reason.strip():
        raise ServiceError("An audit reason is required.")


def preview_hint_edit(hint, *, cost, description, text):
    _validate_hint_edit(cost, description, text)
    redemptions = list(HintRedemption.objects.filter(hint=hint))
    effects = [
        {
            "user_id": redemption.user_id,
            "before": -hint.cost,
            "after": -cost,
            "score_delta": hint.cost - cost,
        }
        for redemption in redemptions
    ]
    return {
        "hint_id": hint.id,
        "redeemed_count": len(redemptions),
        "effects": effects,
        "message": f"Impact preview — no changes applied. {len(redemptions)} existing redemption(s) use this price.",
        "revision": DataRevision.current(),
    }


def apply_hint_edit(actor, hint, *, cost, description, text, reason="", expected_revision):
    _assert_admin(actor)
    if DataRevision.current() != expected_revision:
        raise ServiceError("Data changed since the preview. Preview the change again.", 409)
    _validate_hint_edit(cost, description, text)
    _validate_reason(reason)
    with transaction_atomic():
        before = {"cost": hint.cost, "description": hint.description}
        hint.cost = cost
        hint.description = description.strip()
        hint.text = text.strip()
        hint.save(update_fields=("cost", "description", "text"))
        for redemption in HintRedemption.objects.filter(hint=hint).select_related("user"):
            Notification.objects.create(
                recipient=redemption.user,
                message="A redeemed hint price changed. Your score now uses the current price.",
            )
        _audit(actor, "hint-edit", reason, {"hint": hint.id, "before": before, "after": {"cost": cost}})
        DataRevision.bump()
    return {"message": "Change applied. Results recalculated.", "revision": DataRevision.current()}


def refund_hint(actor, user_id, hint_id):
    _assert_admin(actor)
    redemption = HintRedemption.objects.filter(user_id=user_id, hint_id=hint_id).first()
    if not redemption:
        raise ServiceError("No redemption of that hint exists for this account.", 404)
    hint = redemption.hint
    redemption.delete()
    Notification.objects.create(
        recipient_id=user_id,
        message="A hint redemption was refunded. The current hint price was returned to your score; you may redeem the hint again.",
    )
    _audit(actor, "hint-refund", "", {"user": user_id, "hint": hint_id})
    DataRevision.bump()
    return hint


def delete_hint(actor, hint_id):
    _assert_admin(actor)
    try:
        hint = Hint.objects.get(pk=hint_id)
    except Hint.DoesNotExist:
        raise ServiceError("Hint not found.", 404)
    if HintRedemption.objects.filter(hint=hint).exists():
        raise ServiceError("Redeemed hints cannot be deleted. Refund every redemption first, or archive the challenge instead.", 409)
    hint_id_value = hint.id
    hint.delete()
    _audit(actor, "hint-delete", "", {"hint": hint_id_value})
    DataRevision.bump()


def _clean_questions(questions):
    """Validate a create-challenge question list before anything is created."""
    if not isinstance(questions, list) or not questions:
        raise ServiceError("At least one question is required.")
    cleaned = []
    for index, question in enumerate(questions, start=1):
        prompt = question.get("prompt") if isinstance(question, dict) else None
        answers = question.get("answers")
        points = question.get("points", 100)
        if not isinstance(prompt, str) or not prompt.strip():
            raise ServiceError(f"Question {index}: the prompt is required.")
        if not isinstance(answers, list) or not answers:
            raise ServiceError(f"Question {index}: at least one accepted answer is required.")
        values = []
        for answer in answers:
            if not isinstance(answer, str) or not answer.strip():
                raise ServiceError(f"Question {index}: accepted answers must be nonempty text.")
            value = answer.strip()
            if value not in values:
                values.append(value)
        if isinstance(points, bool) or not isinstance(points, int) or points < 0:
            raise ServiceError(f"Question {index}: points must be a nonnegative integer.")
        cleaned.append({"prompt": prompt.strip(), "answers": values, "points": points})
    return cleaned


def create_challenge(actor, *, title, description="", pool_ids=(), author="", questions):
    _assert_admin(actor)
    if not title.strip():
        raise ServiceError("Title is required.")
    if not isinstance(author, str) or len(author) > 100:
        raise ServiceError("Author is a display credit of at most 100 characters.")
    try:
        pools = [Pool.objects.get(pk=pool_id) for pool_id in pool_ids]
    except (Pool.DoesNotExist, ValueError, TypeError):
        raise ServiceError("Select valid pools.")
    cleaned = _clean_questions(questions)
    challenge = Challenge.objects.create(
        title=title.strip(), slug=_unique_slug(title), summary="Draft challenge",
        description=description, lifecycle=Challenge.Lifecycle.DRAFT,
        author=author.strip(),
    )
    try:
        for pool in pools:
            PoolChallenge.objects.create(pool=pool, challenge=challenge)
        for order, question in enumerate(cleaned):
            row = Question.objects.create(
                challenge=challenge, prompt=question["prompt"], points=question["points"],
                display_order=order,
            )
            for value in question["answers"]:
                AcceptedAnswer.objects.create(question=row, value=value)
    except Exception:
        # Validation happened up front; this is a last-resort rollback so a
        # failed creation leaves no half-built challenge behind.
        Question.objects.filter(challenge=challenge).delete()
        PoolChallenge.objects.filter(challenge=challenge).delete()
        challenge.delete()
        raise
    _audit(actor, "challenge-create", "", {"challenge": challenge.id, "questions": len(cleaned)})
    DataRevision.bump()
    return challenge


def revoke_preapproval(actor, discord_id):
    """Revoke a pre-approval: the ID no longer auto-approves at first sign-in.

    Append-only ledger: a 'revoke-preapprove' row after the pre-approval.
    An account that already used the pre-approval is untouched — suspending
    that account is the separate lever (owner decision: revoke is about
    future auto-approvals, never about stripping existing access)."""
    _assert_admin(actor)
    discord_id = str(discord_id)
    if not discord_id.isdigit() or not 5 <= len(discord_id) <= 25:
        raise ServiceError("Enter a 5–25 digit Discord ID.")
    latest = ApprovalDecision.objects.filter(
        discord_id=discord_id,
        decision__in=("preapprove", "approve", "revoke-preapprove"),
    ).order_by("-id").first()
    if latest is None:
        raise ServiceError("No pre-approval exists for that ID.", 404)
    if latest.decision == "revoke-preapprove":
        raise ServiceError("That pre-approval is already revoked.", 409)
    if latest.decision == "approve":
        raise ServiceError("That ID was approved directly, not pre-approved; suspend the account instead.", 409)
    ApprovalDecision.objects.create(
        discord_id=discord_id, decision="revoke-preapprove", actor=actor,
    )
    _audit(actor, "preapproval-revoke", "", {"discord_id": discord_id})
    DataRevision.bump()


def _unique_slug(title):
    from django.utils.text import slugify
    import uuid as uuid_lib

    base = slugify(title)[:180] or "pool"
    slug = f"{base}-{uuid_lib.uuid4().hex[:8]}"
    return slug


def _validate_pool_fields(name, description, order):
    if not isinstance(name, str) or not name.strip():
        raise ServiceError("Pool name is required.")
    if len(name.strip()) > 100:
        raise ServiceError("Pool name is too long (100 characters max).")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise ServiceError("Description must be text.")
    if isinstance(order, bool) or not isinstance(order, int) or order < 0:
        raise ServiceError("Order must be a nonnegative integer.")
    return name.strip(), description.strip(), order


def create_pool(actor, *, name, description="", order=0):
    _assert_admin(actor)
    name, description, order = _validate_pool_fields(name, description, order)
    if Pool.objects.filter(name__iexact=name).exists():
        raise ServiceError("A pool with that name already exists.", 409)
    pool = Pool.objects.create(name=name, slug=_unique_slug(name), description=description, order=order)
    _audit(actor, "pool-create", "", {"pool": pool.id, "name": name})
    DataRevision.bump()
    return pool


def edit_pool(actor, pool_id, *, name, description="", order=0):
    _assert_admin(actor)
    try:
        pool = Pool.objects.get(pk=pool_id)
    except Pool.DoesNotExist:
        raise ServiceError("Pool not found.", 404)
    name, description, order = _validate_pool_fields(name, description, order)
    if Pool.objects.filter(name__iexact=name).exclude(pk=pool.id).exists():
        raise ServiceError("A pool with that name already exists.", 409)
    pool.name = name
    pool.description = description
    pool.order = order
    pool.save(update_fields=("name", "description", "order"))
    _audit(actor, "pool-edit", "", {"pool": pool.id, "name": name})
    DataRevision.bump()
    return pool


def delete_pool(actor, pool_id):
    _assert_admin(actor)
    try:
        pool = Pool.objects.get(pk=pool_id)
    except Pool.DoesNotExist:
        raise ServiceError("Pool not found.", 404)
    name, pool_id_value = pool.name, pool.id
    pool.delete()  # cascades memberships only; challenges and scores are untouched
    _audit(actor, "pool-delete", "", {"pool": pool_id_value, "name": name})
    DataRevision.bump()


def edit_content(actor, challenge, *, title, summary, description, pool_ids, expected_revision,
                 lifecycle=None, go_live=None, author=""):
    _assert_admin(actor)
    if DataRevision.current() != expected_revision:
        raise ServiceError("Data changed since the page loaded. Reload and try again.", 409)
    if not isinstance(title, str) or not title.strip():
        raise ServiceError("Title is required.")
    if not isinstance(author, str) or len(author) > 100:
        raise ServiceError("Author is a display credit of at most 100 characters.")
    pools = []
    for pool_id in pool_ids:
        try:
            pools.append(Pool.objects.get(pk=pool_id))
        except (Pool.DoesNotExist, ValueError, TypeError):
            raise ServiceError("Select valid pools.")
    if not pools:
        raise ServiceError("Select at least one pool.")
    with transaction_atomic():
        challenge.title = title.strip()
        challenge.summary = str(summary or "")
        challenge.description = str(description or "")
        challenge.author = author.strip()
        challenge.save(update_fields=("title", "summary", "description", "author", "updated_at"))
        PoolChallenge.objects.filter(challenge=challenge).delete()
        PoolChallenge.objects.bulk_create([PoolChallenge(pool=pool, challenge=challenge) for pool in pools])
        if lifecycle is not None:
            if lifecycle not in Challenge.Lifecycle.values:
                raise ServiceError("Invalid lifecycle.")
            challenge.lifecycle = lifecycle
            if go_live is not None:
                challenge.go_live = go_live
            challenge.save(update_fields=("lifecycle", "go_live", "updated_at"))
        _audit(actor, "content-edit", "", {
            "challenge": challenge.id, "pools": [pool.id for pool in pools],
            "lifecycle": challenge.lifecycle,
        })
        DataRevision.bump()
    return challenge


def set_lifecycle(actor, challenge_id, lifecycle, go_live=None):
    _assert_admin(actor)
    if lifecycle not in Challenge.Lifecycle.values:
        raise ServiceError("Invalid lifecycle.")
    try:
        challenge = Challenge.objects.get(pk=challenge_id)
    except Challenge.DoesNotExist:
        raise ServiceError("Challenge not found.", 404)
    if go_live is not None and go_live < 0:
        raise ServiceError("Go-live time must be a nonnegative timestamp.")
    challenge.lifecycle = lifecycle
    if go_live is not None:
        challenge.go_live = go_live
    challenge.save(update_fields=("lifecycle", "go_live", "updated_at"))
    _audit(actor, "lifecycle", "", {"challenge": challenge.id, "lifecycle": lifecycle})
    DataRevision.bump()
    return challenge


def delete_draft(actor, challenge_id):
    _assert_admin(actor)
    try:
        challenge = Challenge.objects.get(pk=challenge_id)
    except Challenge.DoesNotExist:
        raise ServiceError("Challenge not found.", 404)
    if challenge.lifecycle != Challenge.Lifecycle.DRAFT:
        raise ServiceError("Only drafts can be deleted. Archive published challenges instead.", 409)
    if Submission.objects.filter(question__challenge=challenge).exists():
        raise ServiceError("This draft has submission history and cannot be deleted.", 409)
    # Redeemed hints are never deletable (owner decision §9): refusal, not a
    # database ProtectedError, is the operator-facing behavior.
    if HintRedemption.objects.filter(hint__challenge=challenge).exists():
        raise ServiceError("A hint of this draft has redemptions; refund them before deleting.", 409)
    Hint.objects.filter(challenge=challenge).delete()
    Question.objects.filter(challenge=challenge).delete()
    PoolChallenge.objects.filter(challenge=challenge).delete()
    LabInstance.objects.filter(challenge=challenge).delete()
    challenge_id_value = challenge.id
    challenge.delete()
    _audit(actor, "draft-delete", "", {"challenge": challenge_id_value})
    DataRevision.bump()


def add_question(actor, challenge_id, *, prompt, answers, points, display_order, input_format=""):
    _assert_admin(actor)
    try:
        challenge = Challenge.objects.get(pk=challenge_id)
    except Challenge.DoesNotExist:
        raise ServiceError("Challenge not found.", 404)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ServiceError("Question prompt is required.")
    cleaned = []
    if not isinstance(answers, list) or not answers:
        raise ServiceError("Accepted answers are required.")
    for answer in answers:
        if not isinstance(answer, str) or not answer.strip():
            raise ServiceError("Accepted answers must be nonempty text.")
        value = answer.strip()
        if value not in cleaned:
            cleaned.append(value)
    if isinstance(points, bool) or not isinstance(points, int) or points < 0:
        raise ServiceError("Points must be a nonnegative integer.")
    if isinstance(display_order, bool) or not isinstance(display_order, int) or display_order < 0:
        raise ServiceError("Display order must be a nonnegative integer.")
    question = Question.objects.create(
        challenge=challenge, prompt=prompt.strip(), points=points,
        display_order=display_order, input_format=str(input_format or ""),
    )
    for value in cleaned:
        AcceptedAnswer.objects.create(question=question, value=value)
    _audit(actor, "question-add", "", {"question": question.id, "challenge": challenge.id})
    DataRevision.bump()
    return question


def transaction_atomic():
    return transaction.atomic()
