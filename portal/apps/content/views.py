"""Server-rendered pages: challenge browser, pools, challenge detail, file download."""
import mimetypes

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from portal.apps.accounts.decorators import approved_page
from portal.apps.submissions.models import Solve

from .models import Attachment, Challenge, Pool


def _visible_challenges(when=None):
    now = when or timezone.now()
    return (
        Challenge.objects.filter(lifecycle=Challenge.Lifecycle.PUBLISHED)
        .filter(go_live__isnull=True) | Challenge.objects.filter(lifecycle=Challenge.Lifecycle.PUBLISHED, go_live__lte=now)
    )


def _solved_question_ids(user):
    return set(Solve.objects.filter(user=user).values_list("question_id", flat=True))


@login_required
@approved_page
def home(request, pool_slug=None):
    pool = None
    if pool_slug:
        pool = get_object_or_404(Pool, slug=pool_slug)
    solved_ids = _solved_question_ids(request.user)
    query = request.GET.get("q", "").strip()
    status = request.GET.get("status", "all")

    challenges = (
        Challenge.objects.filter(lifecycle=Challenge.Lifecycle.PUBLISHED)
        .prefetch_related("questions", "memberships__pool", "hints")
    )
    rows = []
    for challenge in challenges:
        if challenge.go_live and challenge.go_live > timezone.now():
            continue
        if pool_slug and not challenge.memberships.filter(pool__slug=pool_slug).exists():
            continue
        if query and query.lower() not in (challenge.title + " " + challenge.description).lower():
            continue
        solved_count = sum(1 for question in challenge.questions.all() if question.id in solved_ids)
        total = challenge.questions.count()
        solved = total > 0 and solved_count == total
        if status == "solved" and not solved:
            continue
        if status == "unsolved" and solved:
            continue
        rows.append({
            "challenge": challenge,
            "solved": solved,
            "solved_count": solved_count,
            "total": total,
            "points": sum(q.points for q in challenge.questions.all()),
            "pools": [membership.pool for membership in challenge.memberships.all()],
        })
    pools = Pool.objects.all()
    return render(request, "home.html", {
        "rows": rows, "pools": pools, "pool": pool,
        "status": status, "query": query,
    })


@login_required
@approved_page
def challenge_detail(request, slug):
    challenge = get_object_or_404(
        Challenge.objects.prefetch_related("questions", "hints", "attachments", "memberships__pool"),
        slug=slug,
    )
    if not challenge.visible_at(timezone.now()):
        messages.info(request, "This challenge is not available.")
        return redirect("home")
    solved_ids = _solved_question_ids(request.user)
    questions = []
    for question in challenge.questions.all():
        solved_at = Solve.objects.filter(user=request.user, question=question).values_list("solved_at", flat=True).first()
        questions.append({"question": question, "solved": question.id in solved_ids, "solved_at": solved_at})
    owned_hints = set(
        request.user.redemptions.filter(hint__challenge=challenge).values_list("hint_id", flat=True)
    )
    pools = [membership.pool for membership in challenge.memberships.all()]
    return render(request, "challenge_detail.html", {
        "challenge": challenge,
        "questions": questions,
        "pools": pools,
        "owned_hints": owned_hints,
        "total_points": sum(q.points for q in challenge.questions.all()),
    })


@login_required
@approved_page
def pools_page(request):
    from portal.apps.submissions.models import Solve

    solved_ids = _solved_question_ids(request.user)
    rows = []
    for pool in Pool.objects.prefetch_related("memberships__challenge"):
        challenges = [m.challenge for m in pool.memberships.all()]
        total = 0
        solved = 0
        for challenge in challenges:
            if not challenge.visible_at(timezone.now()):
                continue
            total += 1
            if challenge.questions.exists() and all(
                q.id in solved_ids for q in challenge.questions.all()
            ):
                solved += 1
        rows.append({"pool": pool, "total": total, "solved": solved})
    return render(request, "pools.html", {"rows": rows})


@login_required
@approved_page
def download(request, slug, attachment_id):
    challenge = get_object_or_404(Challenge, slug=slug)
    if not challenge.visible_at(timezone.now()):
        raise Http404("Challenge is not available.")
    attachment = get_object_or_404(Attachment, pk=attachment_id, challenge=challenge)
    if not attachment.file:
        raise Http404("Attachment missing.")
    content_type = mimetypes.guess_type(attachment.filename)[0] or "application/octet-stream"
    return FileResponse(attachment.file.open("rb"), as_attachment=True, filename=attachment.filename, content_type=content_type)
