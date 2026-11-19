"""Server-rendered pages: leaderboard, progress, notifications, answer + hint actions.

Replaces the interim JSON API (handoff §14: server-rendered templates). Forms
carry real CSRF tokens; feedback flows through the messages framework.
"""
import uuid
import time

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from portal.apps.accounts.decorators import approved_page, approved_required
from portal.apps.content.models import Challenge, Hint, Pool, Question

from . import services
from .models import Solve, Submission


def _client_ip(request):
    """The player's IP. Behind the Cloudflare proxy the edge-set
    CF-Connecting-IP header is the trustworthy value (the edge overwrites
    it; players cannot spoof it). Direct LAN access falls back to the
    socket address."""
    forwarded = request.META.get("HTTP_CF_CONNECTING_IP", "").strip()
    return forwarded or request.META.get("REMOTE_ADDR", "")


def _rate_limited(request, scope, limit, window_seconds):
    """Simple fixed-window counter for production-only flood control.
    Skipped entirely when DEBUG (single-operator dev mode).

    The window key is time-bucketed so it expires naturally: a sustained
    abuser is blocked for each window instead of being locked out forever
    (a sliding refresh would never let the counter age out)."""
    if getattr(settings, "DEBUG", False):
        return False
    ip = _client_ip(request)
    if not ip:
        return False
    window = int(time.time()) // window_seconds
    key = f"ratelimit:{scope}:{ip}:{window}"
    try:
        current = cache.get_or_set(key, 0, timeout=window_seconds) + 1
        cache.set(key, current, timeout=window_seconds + 5)
    except Exception:
        return False  # cache unavailable: fail open, never lock players out
    return current > limit


def _pool_from_request(request):
    pool_slug = request.GET.get("pool")
    if pool_slug and pool_slug != "all":
        return get_object_or_404(Pool, slug=pool_slug)
    return None


@approved_page
def leaderboard(request):
    pool = _pool_from_request(request)
    return render(request, "leaderboard.html", {
        "entries": services.leaderboard(pool),
        "pools": Pool.objects.all(),
        "pool": pool,
    })


@approved_page
def progress(request):
    user = request.user
    overall = services.summary(user)
    redemptions = user.redemptions.select_related("hint__challenge").order_by("-redeemed_at")
    history = (
        Submission.objects.filter(user=user, eligible=True)
        .select_related("question__challenge")
        .order_by("-submitted_at", "-id")
    )
    return render(request, "progress.html", {
        "summary": overall,
        "redemptions": redemptions,
        "history": history,
    })


@approved_page
def notifications(request):
    rows = request.user.notifications.all()[:100]
    return render(request, "notifications.html", {"rows": rows})


@approved_required
def notifications_poll(request):
    """Lightweight polling endpoint for toast notifications (10s client loop)."""
    unseen = request.user.notifications.filter(read_at__isnull=True)
    return JsonResponse({
        "unread": unseen.count(),
        "messages": [{"id": row.id, "message": row.message} for row in unseen.order_by("-id")[:5]],
    })


@approved_page
def mark_notifications_read(request):
    if request.method == "POST":
        request.user.notifications.filter(read_at__isnull=True).update(read_at=timezone.now())
        messages.success(request, "Notifications marked read.")
    return redirect("notifications")


@approved_page
def submit_answer(request, slug, question_id):
    challenge = get_object_or_404(Challenge, slug=slug)
    question = get_object_or_404(Question, pk=question_id, challenge=challenge)
    if request.method == "POST":
        # Flood control (§23 alert posture): per-IP fixed window, production
        # only. Generous enough that a human answering on cooldown can never
        # trip it; the per-user cooldown remains the fairness mechanism.
        limit = int(getattr(settings, "ANSWER_IP_RATE_LIMIT", 60))
        if _rate_limited(request, "answer", limit, 60):
            messages.error(request, "Too many attempts from this network. Wait a minute and try again.")
            return redirect("challenge_detail", slug=slug)
        answer = request.POST.get("answer", "")
        request_id = request.POST.get("request_id", "") or uuid.uuid4().hex
        try:
            result = services.submit(request.user, question.id, answer, request_id)
            if result["correct"]:
                messages.success(request, result["message"])
            else:
                messages.info(request, result["message"])
        except services.ServiceError as error:
            messages.error(request, error.message)
    return redirect("challenge_detail", slug=slug)


@approved_page
def redeem_hint(request, slug, hint_id):
    challenge = get_object_or_404(Challenge, slug=slug)
    hint = get_object_or_404(Hint, pk=hint_id, challenge=challenge)
    if request.method == "POST":
        try:
            result = services.redeem(request.user, hint.id)
            messages.success(request, result["message"])
        except services.ServiceError as error:
            messages.error(request, error.message)
    return redirect("challenge_detail", slug=slug)
