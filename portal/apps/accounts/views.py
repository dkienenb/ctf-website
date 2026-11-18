"""Account pages: approval status, settings, and explicit Discord profile refresh."""
import json
import urllib.request

from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from .models import User


def status_page(request):
    if not request.user.is_authenticated:
        return redirect("discord_login")
    if request.user.status == User.Status.APPROVED:
        return redirect("home")
    return render(request, "status.html")


@require_http_methods(["GET"])
def status_check(request):
    """Lightweight poll for the waiting page: redirects the moment the
    operator approves the account (no refresh needed)."""
    if not request.user.is_authenticated:
        return JsonResponse({"authenticated": False})
    approved = request.user.status == User.Status.APPROVED
    return JsonResponse({"authenticated": True, "approved": approved})


def settings_page(request):
    if not request.user.is_authenticated:
        return redirect("discord_login")
    return render(request, "settings.html")


@require_http_methods(["POST"])
def refresh_profile(request):
    """Explicit 'Reload from Discord' (handoff §3). Never changes identity,
    approval, ownership, or progress; only the cached display profile."""
    from allauth.socialaccount.models import SocialToken

    user = request.user
    if not user.is_authenticated:
        return redirect("discord_login")
    token = (
        SocialToken.objects.filter(account__user=user, account__provider="discord")
        .order_by("-id")
        .values_list("token", flat=True)
        .first()
    )
    if not token:
        messages.error(request, "No active Discord session token. Sign in again to refresh your profile.")
        return redirect("settings")
    request_ = urllib.request.Request(
        "https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request_, timeout=10) as response:
            profile = json.loads(response.read())
    except Exception:
        messages.error(request, "Discord did not respond. Your cached profile is unchanged.")
        return redirect("settings")
    uid = str(profile.get("id", ""))
    if uid != user.discord_id:
        messages.error(request, "Discord returned a different identity. Profile refresh cancelled.")
        return redirect("settings")
    display = profile.get("global_name") or profile.get("username") or user.cached_name
    avatar_hash = profile.get("avatar")
    avatar = (
        f"https://cdn.discordapp.com/avatars/{uid}/{avatar_hash}.png"
        if avatar_hash
        else user.cached_avatar
    )
    user.cached_name = display[:100]
    user.cached_avatar = avatar[:500]
    user.profile_refreshed_at = timezone_now()
    user.save(update_fields=("cached_name", "cached_avatar", "profile_refreshed_at", "updated_at"))
    messages.success(request, "Discord name and avatar refreshed. Your identity and progress are unchanged.")
    return redirect("settings")


def timezone_now():
    from django.utils import timezone

    return timezone.now()
