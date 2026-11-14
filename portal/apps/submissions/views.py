"""JSON API views. Session-authenticated; server-rendered UI consumes these
directly or via templates in later build steps."""
import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt  # JSON+session API interim; CSRF review lands with the UI layer.
from django.views.decorators.http import require_http_methods

from portal.apps.accounts.decorators import approved_required
from portal.apps.content.models import Pool

from . import services


def _body(request):
    try:
        data = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        raise services.ServiceError("Invalid JSON body.", 400)
    if not isinstance(data, dict):
        raise services.ServiceError("Expected a JSON object.", 400)
    return data


def _handle(request, handler):
    try:
        result = handler()
    except services.ServiceError as error:
        return JsonResponse({"error": error.message}, status=error.status)
    return JsonResponse(result)


@require_http_methods(["POST"])
@csrf_exempt
@approved_required
def submit(request, question_id):
    body = _body(request)
    return _handle(request, lambda: services.submit(
        request.user, question_id, body.get("answer", ""), body.get("request_id", "")
    ))


@require_http_methods(["POST"])
@csrf_exempt
@approved_required
def redeem(request, hint_id):
    return _handle(request, lambda: services.redeem(request.user, hint_id))


@require_http_methods(["GET"])
@approved_required
def leaderboard(request):
    pool_id = request.GET.get("pool")
    pool = None
    if pool_id and pool_id != "all":
        try:
            pool = Pool.objects.get(pk=pool_id)
        except Pool.DoesNotExist:
            return JsonResponse({"error": "Pool not found."}, status=404)
    return JsonResponse({"entries": services.leaderboard(pool)})


@require_http_methods(["GET"])
@approved_required
def me(request):
    pool_id = request.GET.get("pool")
    pool = None
    if pool_id and pool_id != "all":
        try:
            pool = Pool.objects.get(pk=pool_id)
        except Pool.DoesNotExist:
            return JsonResponse({"error": "Pool not found."}, status=404)
    return JsonResponse(services.summary(request.user, pool))
