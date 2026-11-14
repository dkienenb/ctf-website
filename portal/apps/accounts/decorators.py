"""Access decorators for the JSON API (handoff §3: approval is a backend condition)."""
from functools import wraps

from django.http import JsonResponse

from .models import User


def approved_required(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Sign in first."}, status=401)
        if request.user.status != User.Status.APPROVED:
            return JsonResponse({"error": "Approved access is required."}, status=403)
        return view(request, *args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    @approved_required
    def wrapped(request, *args, **kwargs):
        if not request.user.is_admin:
            return JsonResponse({"error": "Administrator access is required."}, status=403)
        return view(request, *args, **kwargs)

    return wrapped
