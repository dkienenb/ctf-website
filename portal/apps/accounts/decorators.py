"""Access decorators: page variants redirect to the approval status page."""
from functools import wraps

from django.http import JsonResponse
from django.shortcuts import redirect

from .models import User


def approved_required(view):
    """JSON API variant: 401/403 responses."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Sign in first."}, status=401)
        if request.user.status != User.Status.APPROVED:
            return JsonResponse({"error": "Approved access is required."}, status=403)
        return view(request, *args, **kwargs)

    return wrapped


def approved_page(view):
    """Server-rendered variant: approved-only pages redirect to the status page."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect("discord_login")
        if request.user.status != User.Status.APPROVED:
            return redirect("status")
        return view(request, *args, **kwargs)

    return wrapped
