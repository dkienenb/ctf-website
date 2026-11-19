"""Retroactive accepted-answer corrections (handoff §7–§8).

Replay boundary (documented in ctf-prototype/docs/DECISIONS.md and carried
here as the production interpretation):

1. `all` (Remove all submissions): every active submission in the question's
   current generation is excluded as `replacement` history, its solve is
   cleared, and the generation increments. No historical credit carries into
   the replacement. Message: "Question replaced".
2. `incorrect` / `none`: active records AND records previously pruned by a
   correction are replayed within the current generation only. Deliberate
   clears (`incorrect-clear`) and `replacement` exclusions are never
   resurrected.
3. Matching uses the new accepted-answer set. The earliest now-correct record
   keeps its original timestamp and becomes the solve; later records are
   marked `correction-pruned`.
4. `incorrect` then excludes remaining incorrect active records. Message:
   "Question updated". `none` retains them. Message: "Question corrected".
5. Records excluded by `replacement` cannot replay, and old generations never
   reappear.

Preview and apply share the same pure planner, and both verify the coarse
DataRevision so a stale apply is rejected with 409. Every apply writes an
audit event (actor, reason, before/after) and notifies affected accounts with
the exact selected message.
"""
from django.db import transaction

from portal.apps.content.models import AcceptedAnswer, Question

from .models import AuditEvent, DataRevision, Notification, Solve, Submission
from .services import ServiceError

MODE_MESSAGES = {
    "all": "Question replaced",
    "incorrect": "Question updated",
    "none": "Question corrected",
}
MODES = tuple(MODE_MESSAGES)


def _clean_answers(answers):
    if not isinstance(answers, list) or not answers:
        raise ServiceError("Provide at least one accepted answer.")
    cleaned = []
    for answer in answers:
        if not isinstance(answer, str) or not answer.strip() or len(answer) > 4096:
            raise ServiceError("Accepted answers must be nonempty text of at most 4096 characters.")
        value = answer.strip()
        if value not in cleaned:
            cleaned.append(value)
    return cleaned


def _validate_edit_body(*, answers, points, mode, prompt, input_format, display_order):
    cleaned = _clean_answers(answers)
    if mode not in MODES:
        raise ServiceError("Select an edit mode.")
    if isinstance(points, bool) or not isinstance(points, int) or points < 0:
        raise ServiceError("Points must be a nonnegative integer.")
    if prompt is not None and (not isinstance(prompt, str) or not prompt.strip()):
        raise ServiceError("Prompt is required.")
    if input_format is not None and not isinstance(input_format, str):
        raise ServiceError("Displayed input format must be text.")
    if display_order is not None and (isinstance(display_order, bool) or not isinstance(display_order, int) or display_order < 0):
        raise ServiceError("Question order must be a nonnegative integer.")
    return cleaned


def _plan(rows, answers, mode, generation):
    """Pure planner: returns (changes, solve_for, users).

    changes: {submission_id: (eligible, exclusion, correct)} for rows whose
    state would change; rows absent from the mapping keep their current state.
    solve_for: {user_id: submission_id} after the edit.
    users: every account with a submission in the current generation.
    """
    changes = {}
    solve_for = {}
    users = set()
    by_user = {}
    for row in rows:
        by_user.setdefault(row.user_id, []).append(row)
    for user_id, user_rows in by_user.items():
        gen_rows = [row for row in user_rows if row.generation == generation]
        if not gen_rows:
            continue
        users.add(user_id)
        if mode == "all":
            for row in gen_rows:
                if row.eligible:
                    changes[row.id] = (False, Submission.Exclusion.REPLACEMENT, row.correct)
            continue
        replayable = [
            row for row in gen_rows
            if row.eligible or row.exclusion == Submission.Exclusion.CORRECTION_PRUNED
        ]
        found = False
        seen_active_incorrect = set()
        for row in replayable:
            is_correct = row.answer in answers
            if is_correct and not found:
                changes[row.id] = (True, "", True)
                solve_for[user_id] = row.id
                found = True
            elif found:
                changes[row.id] = (False, Submission.Exclusion.CORRECTION_PRUNED, is_correct)
            else:
                # Not yet correct: the earliest record of an answer stays a
                # counted incorrect attempt; later duplicates of the same
                # answer would violate uniq_active_answer, so they are pruned
                # as `duplicate` audit-only records.
                if row.answer in seen_active_incorrect:
                    changes[row.id] = (False, Submission.Exclusion.DUPLICATE, False)
                    continue
                seen_active_incorrect.add(row.answer)
                changes[row.id] = (True, "", False)
        if mode == "incorrect":
            for row in gen_rows:
                eligible, exclusion, correct = changes.get(row.id, (row.eligible, row.exclusion, row.correct))
                if eligible and not correct:
                    changes[row.id] = (False, Submission.Exclusion.INCORRECT_CLEAR, False)
    return changes, solve_for, users


def _effects(rows, changes, solves_before, solve_for, current_points, new_points):
    """Per-user before/after on this question; global score delta equals the
    earned delta because hint deductions are untouched by question edits."""
    state_before = {}
    state_after = {}
    for row in rows:
        before = state_before.setdefault(row.user_id, {"attempts": 0, "correct": 0})
        if row.eligible:
            before["attempts"] += 1
            before["correct"] += 1 if row.correct else 0
        after = state_after.setdefault(row.user_id, {"attempts": 0, "correct": 0})
        eligible, _, correct = changes.get(row.id, (row.eligible, row.exclusion, row.correct))
        if eligible:
            after["attempts"] += 1
            after["correct"] += 1 if correct else 0
    effects = []
    for user_id in sorted(set(state_before) | set(state_after) | set(solves_before) | set(solve_for)):
        before_earned = current_points if user_id in solves_before else 0
        after_earned = new_points if user_id in solve_for else 0
        before = state_before.get(user_id, {"attempts": 0, "correct": 0})
        after = state_after.get(user_id, {"attempts": 0, "correct": 0})
        if (before, before_earned) == (after, after_earned):
            continue
        effects.append({
            "user_id": user_id,
            "attempts_before": before["attempts"],
            "attempts_after": after["attempts"],
            "correct_before": before["correct"],
            "correct_after": after["correct"],
            "earned_before": before_earned,
            "earned_after": after_earned,
            "score_delta": after_earned - before_earned,
        })
    return effects


def _load(question):
    rows = list(
        Submission.objects.filter(question=question).order_by("submitted_at", "id")
    )
    solves_before = {solve.user_id: solve for solve in Solve.objects.filter(question=question)}
    return rows, solves_before


def preview_question_edit(question, *, answers, points, mode, prompt=None, input_format=None, display_order=None):
    cleaned = _validate_edit_body(
        answers=answers, points=points, mode=mode,
        prompt=prompt, input_format=input_format, display_order=display_order,
    )
    rows, solves_before = _load(question)
    changes, solve_for, _users = _plan(rows, cleaned, mode, question.generation)
    active_after = 0
    for row in rows:
        eligible, _, _ = changes.get(row.id, (row.eligible, row.exclusion, row.correct))
        active_after += 1 if eligible else 0
    return {
        "question_id": question.id,
        "mode": mode,
        "message": f"Impact preview — no changes applied. Message on apply: {MODE_MESSAGES[mode]}",
        "effects": _effects(rows, changes, solves_before, solve_for, question.points, points),
        "active_attempts_after": active_after,
        "solve_count_after": len(solve_for),
        "generation_after": question.generation + 1 if mode == "all" else question.generation,
        "revision": DataRevision.current(),
    }


def apply_question_edit(actor, question, *, answers, points, mode, prompt=None,
                        input_format=None, display_order=None, reason="", expected_revision):
    cleaned = _validate_edit_body(
        answers=answers, points=points, mode=mode,
        prompt=prompt, input_format=input_format, display_order=display_order,
    )
    if DataRevision.current() != expected_revision:
        raise ServiceError("Data changed since the preview. Preview the change again.", 409)
    if not isinstance(reason, str):
        raise ServiceError("An audit reason is required.")

    with transaction.atomic():
        question = Question.objects.select_for_update().select_related("challenge").get(pk=question.pk)
        rows, solves_before = _load(question)
        rows = list(Submission.objects.select_for_update().filter(pk__in=[row.id for row in rows]).order_by("submitted_at", "id"))
        changes, solve_for, users = _plan(rows, cleaned, mode, question.generation)
        effects = _effects(rows, changes, solves_before, solve_for, question.points, points)

        before_snapshot = {
            "points": question.points,
            "generation": question.generation,
            "prompt": question.prompt,
            "active_answers": list(
                AcceptedAnswer.objects.filter(question=question, active=True).values_list("value", flat=True)
            ),
        }

        # Clear all reclassified rows first in one statement so intermediate
        # states cannot transiently violate uniq_active_answer / uniq_active_solve.
        changed_ids = [row.id for row in rows if row.id in changes]
        Submission.objects.filter(pk__in=changed_ids).update(eligible=False)
        fresh = {row.id: row for row in Submission.objects.filter(pk__in=changed_ids)}
        for row_id, (eligible, exclusion, correct) in changes.items():
            row = fresh[row_id]
            if (row.eligible, row.exclusion, row.correct) != (eligible, exclusion, correct):
                row.eligible = eligible
                row.exclusion = exclusion
                row.correct = correct
                row.save(update_fields=("eligible", "exclusion", "correct"))

        current_values = set(
            AcceptedAnswer.objects.filter(question=question, active=True).values_list("value", flat=True)
        )
        for value in sorted(current_values - set(cleaned)):
            AcceptedAnswer.objects.filter(question=question, value=value).update(active=False, changed_by=actor)
        for value in cleaned:
            if value not in current_values:
                existing = AcceptedAnswer.objects.filter(question=question, value=value).first()
                if existing:
                    existing.active = True
                    existing.changed_by = actor
                    existing.save(update_fields=("active", "changed_by", "changed_at"))
                else:
                    AcceptedAnswer.objects.create(question=question, value=value, active=True, changed_by=actor)

        question.points = points
        if prompt is not None:
            question.prompt = prompt.strip()
        if input_format is not None:
            question.input_format = input_format
        if display_order is not None:
            question.display_order = display_order
        if mode == "all":
            question.generation += 1
        question.save()

        row_by_id = {row.id: row for row in rows}
        for user_id in users:
            existing = solves_before.get(user_id)
            new_submission_id = solve_for.get(user_id)
            if new_submission_id:
                if existing and existing.submission_id == new_submission_id:
                    continue
                if existing:
                    existing.submission_id = new_submission_id
                    existing.solved_at = row_by_id[new_submission_id].submitted_at
                    existing.save(update_fields=("submission", "solved_at"))
                else:
                    Solve.objects.create(
                        user_id=user_id, question=question,
                        submission_id=new_submission_id,
                        solved_at=row_by_id[new_submission_id].submitted_at,
                    )
            elif existing:
                existing.delete()

        for user_id in users:
            Notification.objects.create(
                recipient_id=user_id,
                message=(
                    f"{MODE_MESSAGES[mode]}: '{question.prompt[:120]}' "
                    f"in challenge '{question.challenge.title[:120]}'. "
                    "Your progress and scores were recalculated."
                ),
            )

        AuditEvent.objects.create(
            actor=actor,
            operation="question-edit",
            reason=reason,
            payload={
                "question": question.id,
                "mode": mode,
                "before": before_snapshot,
                "after": {
                    "points": points,
                    "generation": question.generation,
                    "prompt": question.prompt,
                    "active_answers": list(cleaned),
                },
            },
        )
        DataRevision.bump()

    return {
        "message": f"{MODE_MESSAGES[mode]}. Results recalculated.",
        "effects": effects,
        "revision": DataRevision.current(),
    }
