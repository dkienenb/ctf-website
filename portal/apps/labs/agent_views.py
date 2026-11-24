"""Workhorse agent API (handoff §14, §18): heartbeat, command poll, results.

Auth: per-host bearer token; only its sha256 hash is stored. The agent is a
restricted service — it may register heartbeats, fetch its own pending
commands, and report results for commands assigned to it. Nothing else.
"""
import hashlib
import re

from django.http import FileResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .models import Host, HostCommand
from . import services

AGENT_PROTOCOL_VERSION = "1"


def _host_from_request(request):
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    token = header.removeprefix("Bearer ").strip()
    if not token:
        return None
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return Host.objects.filter(token_hash=token_hash).first()


def _reject(message, status):
    return JsonResponse({"error": message}, status=status)


@require_http_methods(["POST"])
@csrf_exempt
def build_artifact(request):
    """Reverse transfer for interactive builds: the AGENT pushes captured
    artifacts back to the controller, chunked (memory-light on both ends).

    Token auth identifies the host; the payload identifies the build. Chunks
    assemble under private storage staging, verify against the per-file
    hash, and are attached to the build's MachineImage record (which the
    command result may create independently — both paths are idempotent)."""
    host = _host_from_request(request)
    if host is None:
        return _reject("Invalid agent credentials.", 401)
    import base64
    import hashlib
    import json

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return _reject("Invalid JSON body.", 400)
    from .models import MachineBuild, MachineImage
    from . import services

    build = MachineBuild.objects.filter(host=host, pk=payload.get("build_id")).first()
    if build is None:
        return _reject("Unknown build for this host.", 404)
    file_name = payload.get("file", "")
    if file_name not in ("domain.xml", "disk-template"):
        return _reject("Refusing artifact file (closed list).", 400)
    sha = str(payload.get("sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        return _reject("Artifact requires a 64-hex sha256.", 400)
    try:
        seq, total = int(payload.get("seq", 0)), int(payload.get("total", 0))
    except (TypeError, ValueError):
        return _reject("Bad chunk indexes.", 400)
    if not 1 <= seq <= total:
        return _reject("Chunk seq outside 1..total.", 400)
    try:
        blob = base64.b64decode(payload.get("data_b64", ""), validate=True)
    except Exception:
        return _reject("Bad base64 data.", 400)

    try:
        result = services.receive_build_artifact(
            build, file_name=file_name, seq=seq, total=total, blob=blob, sha=sha,
            size=int(payload.get("size", 0)),
            chunk_bytes=payload.get("chunk_bytes"),
        )
    except services.ServiceError as error:
        return _reject(error.message, error.status)
    return JsonResponse({"status": "ok", **result})

@require_http_methods(["GET"])
@csrf_exempt
def template_artifact(request):
    """Stream one staged template file to a workhorse (Path A v2 transfer).

    Token auth (the fetching host); the file must exist on the controller —
    host-local captures cannot be served until uploaded. Streaming FileResponse
    keeps multi-gigabyte images out of memory."""
    host = _host_from_request(request)
    if host is None:
        return _reject("Invalid agent credentials.", 401)
    from .models import MachineImage

    role = request.GET.get("role", "")
    version = request.GET.get("version", "")
    file_name = request.GET.get("file", "")
    image = MachineImage.objects.filter(role=role, version=version).first()
    if image is None:
        return _reject("Unknown template version.", 404)
    if file_name == "domain.xml":
        from django.http import HttpResponse
        return HttpResponse(image.domain_xml, content_type="application/xml")
    if file_name == "disk-template":
        if not image.disk_file:
            return _reject("This version has no controller-side disk (host-local capture).", 409)
        return FileResponse(image.disk_file.open("rb"), content_type="application/octet-stream")
    return _reject("Refusing artifact file (closed list).", 400)


@require_http_methods(["POST"])
@csrf_exempt
def heartbeat(request):
    host = _host_from_request(request)
    if host is None:
        return _reject("Invalid agent credentials.", 401)
    import json

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return _reject("Invalid JSON body.", 400)
    host.last_heartbeat = timezone.now()
    host.agent_version = str(payload.get("agent_version", ""))[:50]
    for field in ("free_disk_gb", "total_disk_gb", "free_ram_gb", "total_ram_gb"):
        try:
            value = float(payload.get(field, host.__getattribute__(field)))
            if value >= 0:
                setattr(host, field, value)
        except (TypeError, ValueError):
            pass
    host.running_labs = int(payload.get("running_labs", host.running_labs) or 0)
    management_ip = str(payload.get("management_ip", "") or "")
    if management_ip:
        host.management_ip = management_ip
    inventory = payload.get("inventory")
    if isinstance(inventory, dict):
        host.inventory = inventory
    host.save(update_fields=(
        "last_heartbeat", "agent_version", "free_disk_gb", "total_disk_gb",
        "free_ram_gb", "total_ram_gb", "running_labs", "management_ip", "inventory",
    ))
    # Reconcile: hand the agent its acknowledged deadlines for local
    # enforcement when the controller link drops (§18).
    deadlines = {}
    for lab in host.labs.filter(state="running"):
        lease = lab.leases.filter(active=True).order_by("-id").first()
        if lease:
            deadlines[str(lab.id)] = lease.expires_at.isoformat()
    return JsonResponse({"protocol": AGENT_PROTOCOL_VERSION, "lease_deadlines": deadlines})


@require_http_methods(["GET"])
@csrf_exempt
def poll(request):
    host = _host_from_request(request)
    if host is None:
        return _reject("Invalid agent credentials.", 401)
    host.last_heartbeat = timezone.now()
    host.save(update_fields=("last_heartbeat",))
    pending = HostCommand.objects.filter(host=host, status=HostCommand.Status.PENDING).order_by("created_at")
    commands = [{
        "op_id": command.op_id,
        "operation": command.operation,
        "expected_generation": command.expected_generation,
        "lab": command.lab_id,
        "payload": command.payload,
        "acknowledged_deadline": command.acknowledged_deadline.isoformat() if command.acknowledged_deadline else None,
    } for command in pending[:10]]
    return JsonResponse({"commands": commands})


@require_http_methods(["POST"])
@csrf_exempt
def result(request, op_id):
    host = _host_from_request(request)
    if host is None:
        return _reject("Invalid agent credentials.", 401)
    import json

    try:
        payload = json.loads(request.body or b"{}")
    except json.JSONDecodeError:
        return _reject("Invalid JSON body.", 400)
    status = payload.get("status")
    if status not in ("ok", "failed"):
        return _reject("status must be ok or failed.", 400)
    try:
        outcome = services.complete_command(host, op_id, status, payload.get("result", {}))
    except services.ServiceError as error:
        return _reject(error.message, error.status)
    return JsonResponse(outcome)
