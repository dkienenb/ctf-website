"""Lease scheduler loop: expires leases, issues warnings, promotes the queue.

Run as a periodic process (systemd timer / compose service / cron):
    python manage.py lab_tick            # single pass
    python manage.py lab_tick --loop     # loop with an interval
"""
import time

from django.core.management.base import BaseCommand

from portal.apps.labs import services


class Command(BaseCommand):
    help = "Process lab lease expiries, warnings, and queue promotion."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Run continuously.")
        parser.add_argument("--interval", type=float, default=1.0, help="Loop interval seconds.")

    def handle(self, *args, **options):
        if not options["loop"]:
            services.tick()
            self.stdout.write("tick complete")
            return
        while True:
            try:
                services.tick()
            except Exception as error:  # keep the scheduler alive; errors surface per tick
                self.stderr.write(f"tick error: {type(error).__name__}: {error}")
            time.sleep(options["interval"])
