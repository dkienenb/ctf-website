"""On-platform operator alerts (§23; owner decision 2026-11-14: warnings are
delivered through the in-app notification system rather than a webhook).

Usage: python manage.py notify_admins "<message>"

Creates a notification for every admin account. Called by backup/backup.sh
on failure; usable for any operator alert. Empty output is expected when no
admin exists — recovery then starts with `grant_admin` (accounts app).
"""
from django.core.management.base import BaseCommand, CommandError

from portal.apps.accounts.models import User
from portal.apps.submissions.models import Notification


class Command(BaseCommand):
    help = "Create an on-platform notification for every admin account."

    def add_arguments(self, parser):
        parser.add_argument("message")

    def handle(self, *args, **options):
        message = str(options["message"])[:500]
        admins = list(User.objects.filter(is_admin=True))
        if not admins:
            raise CommandError("No admin accounts exist; run grant_admin first.")
        for admin in admins:
            Notification.objects.create(recipient=admin, message=message)
        self.stdout.write(self.style.SUCCESS(
            f"Notified {len(admins)} admin account(s): {message}"
        ))
