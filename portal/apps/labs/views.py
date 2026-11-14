"""Player lab actions: request, claim, refresh, release, reset request."""
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from portal.apps.accounts.decorators import approved_page
from portal.apps.content.models import Challenge

from . import services


@require_POST
@approved_page
def lab_action(request, slug, action):
    challenge = get_object_or_404(Challenge, slug=slug)
    try:
        if action == "request":
            result = services.request_lab(request.user, challenge.id)
        elif action == "claim":
            result = services.claim(request.user, challenge.id)
        elif action == "refresh":
            result = services.refresh(request.user, challenge.id)
        elif action == "release":
            result = services.release(request.user, challenge.id)
        elif action == "request-reset":
            result = services.request_admin_reset(request.user, challenge.id)
        else:
            raise services.ServiceError("Unknown lab action.")
        messages.success(request, result["message"])
    except services.ServiceError as error:
        messages.error(request, error.message)
    return redirect("challenge_detail", slug=slug)
