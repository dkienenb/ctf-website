"""Discord social-account adapter implementing handoff §3 identity rules.

- Account creation keys on the stable Discord user ID (allauth's social uid).
- Cached name/avatar are captured at account creation only; later logins never
  overwrite them (explicit refresh is a separate owner action, built later).
- Status resolution at login:
    * no account + pre-approval record  -> approved on first login
    * no account, no pre-approval       -> pending
    * existing pending + pre-approval   -> approved
    * existing rejected                 -> back to pending (reapply, owner decision 2026-11-14)
    * existing suspended                -> stays suspended; the portal shows the
      suspended status page and revokes lab access elsewhere.
- Admin rights are never granted through approval.
"""
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from .models import ApprovalDecision, User


def _discord_profile(sociallogin):
    extra = sociallogin.account.extra_data or {}
    uid = str(sociallogin.account.uid)
    display = extra.get("global_name") or extra.get("username") or f"Discord {uid[-4:]}"
    avatar_hash = extra.get("avatar")
    if avatar_hash:
        avatar = f"https://cdn.discordapp.com/avatars/{uid}/{avatar_hash}.png"
    else:
        avatar = f"https://cdn.discordapp.com/embed/avatars/{int(uid) % 6}.png"
    return uid, display[:100], avatar[:500]


def _is_preapproved(discord_id):
    """A pre-approval stands unless the LATEST relevant ledger decision for
    the ID is a revocation (append-only; revoke after upgrade cancels the
    future auto-approval)."""
    latest = ApprovalDecision.objects.filter(
        discord_id=discord_id,
        decision__in=("preapprove", "approve", "revoke-preapprove"),
    ).order_by("-id").first()
    return latest is not None and latest.decision in ("preapprove", "approve")


class DiscordAccountAdapter(DefaultSocialAccountAdapter):
    def populate_user(self, request, sociallogin, data):
        user = super().populate_user(request, sociallogin, data)
        uid, display, avatar = _discord_profile(sociallogin)
        user.discord_id = uid
        user.cached_name = display
        user.cached_avatar = avatar
        return user

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)
        uid, _, _ = _discord_profile(sociallogin)
        user.status = User.Status.APPROVED if _is_preapproved(uid) else User.Status.PENDING
        user.save()
        return user

    def pre_social_login(self, request, sociallogin):
        super().pre_social_login(request, sociallogin)
        if sociallogin.is_existing:
            user = sociallogin.user
            if user.status == User.Status.REJECTED:
                user.status = User.Status.PENDING
                user.save(update_fields=("status", "updated_at"))
            elif user.status == User.Status.PENDING and _is_preapproved(user.discord_id):
                user.status = User.Status.APPROVED
                user.save(update_fields=("status", "updated_at"))
