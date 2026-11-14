"""Register a workhorse host and print its agent token once.

Usage: python manage.py register_host <name> [--capacity N]
"""
import hashlib
import secrets

from django.core.management.base import BaseCommand

from portal.apps.labs.models import Host


class Command(BaseCommand):
    help = "Register a workhorse host; prints the agent bearer token exactly once."

    def add_arguments(self, parser):
        parser.add_argument("name")
        parser.add_argument("--capacity", type=int, default=1)

    def handle(self, *args, **options):
        host, created = Host.objects.get_or_create(name=options["name"])
        host.capacity_slots = options["capacity"]
        token = secrets.token_urlsafe(32)
        host.token_hash = hashlib.sha256(token.encode()).hexdigest()
        host.online = True
        host.save()
        self.stdout.write(f"Host '{host.name}' registered (new={'yes' if created else 'token rotated'}).")
        self.stdout.write("Agent token (store in the agent config; shown once):")
        self.stdout.write(token)
