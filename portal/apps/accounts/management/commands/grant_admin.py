"""Grant admin to an account by Discord ID — the admin-recovery procedure
(handoff §23; owner decision 2026-11-14: CLI grant command, no web path).

Usage: python manage.py grant_admin <discord_id>

Run on the controller (docker compose exec app python manage.py grant_admin …
or the venv directly). Idempotent. If no account exists for the ID (e.g. the
previous Discord account was lost and this is the replacement), the account
is created pre-approved; otherwise its approval status is left untouched and
a suspended account is reinstated so the operator can act.
"""
from django.core.management.base import BaseCommand, CommandError

from portal.apps.accounts.models import ApprovalDecision, User


class Command(BaseCommand):
    help = "Grant admin to the account with the given Discord ID (recovery procedure)."

    def add_arguments(self, parser):
        parser.add_argument("discord_id")

    def handle(self, *args, **options):
        discord_id = str(options["discord_id"])
        if not discord_id.isdigit() or not 5 <= len(discord_id) <= 25:
            raise CommandError("Enter a 5-25 digit Discord ID.")

        user, created = User.objects.get_or_create(
            discord_id=discord_id,
            defaults={"status": User.Status.APPROVED},
        )
        if user.status == User.Status.SUSPENDED:
            # A suspended account cannot use operator pages; recovery must
            # restore access. Ledger records the reinstatement.
            user.status = User.Status.APPROVED
            ApprovalDecision.objects.create(
                discord_id=discord_id, decision="reinstate", actor=None,
                note="admin recovery via grant_admin",
            )
        user.is_admin = True
        user.save(update_fields=("status", "is_admin", "updated_at"))
        if created:
            ApprovalDecision.objects.create(
                discord_id=discord_id, decision="approve", actor=None,
                note="admin recovery via grant_admin (account did not exist)",
            )
        self.stdout.write(self.style.SUCCESS(
            f"Admin granted to {user.cached_name or discord_id} (pk={user.pk},"
            f" {'created' if created else 'existing'} account)."
        ))
