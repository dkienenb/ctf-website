"""Shared test factories (moved out of the old per-app tests.py modules so
every app's test package can use them without cross-app imports).

Kept deliberately minimal: Django ORM-backed factories that skip the
approval/discord flows unless a test needs them.
"""
import hashlib
import uuid

from django.utils import timezone

from portal.apps.accounts.models import User
from portal.apps.content.models import (
    AcceptedAnswer, Challenge, Pool, PoolChallenge, Question,
)
from portal.apps.labs.models import Host, HostCommand, LabTemplate


def make_user(discord_id, name, status=User.Status.APPROVED, is_admin=False):
    user = User.objects.create_user(discord_id, name)
    user.status = status
    user.is_admin = is_admin
    user.save()
    return user


def make_question(answers, points=100, pools=(), user=None):
    challenge = Challenge.objects.create(
        title=f"Challenge {Question.objects.count()}",
        slug=f"challenge-{uuid.uuid4().hex[:10]}",
        lifecycle=Challenge.Lifecycle.PUBLISHED,
    )
    for pool_name in pools:
        pool, _ = Pool.objects.get_or_create(name=pool_name, defaults={"slug": pool_name.lower()})
        PoolChallenge.objects.create(pool=pool, challenge=challenge)
    question = Question.objects.create(challenge=challenge, prompt="Prompt", points=points)
    for answer in answers:
        AcceptedAnswer.objects.create(question=question, value=answer)
    return question


def make_lab_challenge():
    template = LabTemplate.objects.create(attacker_version="kali-v1", target_version="target-v1")
    challenge = Challenge.objects.create(
        title=f"Lab {template.id}",
        slug=f"lab-{template.id}",
        lifecycle=Challenge.Lifecycle.PUBLISHED,
        lab_template=template,
    )
    return challenge


def make_host(name="workhorse-01"):
    host, _ = Host.objects.get_or_create(name=name)
    host.online = True
    host.total_disk_gb = 100
    host.free_disk_gb = 80
    host.total_ram_gb = 32
    host.free_ram_gb = 26
    host.last_heartbeat = timezone.now()
    host.save()
    return host


def complete(host, operation_hint=None, status="ok"):
    """Complete the oldest pending command for the host (agent simulation)."""
    pending = HostCommand.objects.filter(host=host, status=HostCommand.Status.PENDING).order_by("id").first()
    assert pending is not None, f"no pending command (wanted {operation_hint})"
    if operation_hint:
        assert pending.operation == operation_hint, f"expected {operation_hint}, got {pending.operation}"
    from portal.apps.labs import services
    services.complete_command(host, pending.op_id, status, {})
    return pending


def host_token(host):
    """Attach a bearer token to a host and return it (agent API tests)."""
    token = "agent-token-" + uuid.uuid4().hex
    host.token_hash = hashlib.sha256(token.encode()).hexdigest()
    host.save()
    return token
