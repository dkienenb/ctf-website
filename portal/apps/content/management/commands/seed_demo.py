"""Seed the owner-approved demo content into PostgreSQL (idempotent).

Ports the prototype's sample content (ctf-prototype/seed.py) so the portal
has reviewable content immediately. Launch content remains owner-supplied —
edit or replace these through /manage/ before going live.

Usage: python manage.py seed_demo [--update]

Without --update, existing records are left untouched. With --update,
prompts/answers/prices/filenames are refreshed from the definitions below
(submissions, solves, and redemptions are never modified).
"""
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand
from django.db import transaction

from portal.apps.content.models import AcceptedAnswer, Attachment, Challenge, Hint, Pool, PoolChallenge, Question
from portal.apps.labs.models import LabTemplate

POOLS = [
    ("first-steps", "First Steps", "Small discoveries. A solid foundation."),
    ("open-source", "Open Source", "Follow the trail and connect the dots."),
    ("the-lab", "The Lab", "Private attacker and target pairs, right in your browser."),
]

LAB_TEMPLATE_DEFAULTS = {
    "attacker_version": "kali-demo-v1",
    "target_version": "demo-target-v1",
    "attacker_ram_mb": 4096, "attacker_vcpu": 2, "attacker_disk_gb": 20,
    "target_ram_mb": 2048, "target_vcpu": 1, "target_disk_gb": 10,
}

CHALLENGES = [
    {
        "slug": "hello-world", "title": "Hello, world.",
        "summary": "Every journey starts with a discovery. Find your first flag.",
        "description": "A flag proves you found something. Read the field notes, then answer both questions. All questions are available immediately.",
        "pools": ["first-steps"],
        "attachment": ("welcome.txt", "FIELD NOTES / 001\nWelcome, explorer.\nYour first flag: CTF{hello_world}\nThe author signed this note: Ada\n"),
        "questions": [
            ("What flag is in the field notes?", "CTF{...}", 100, ["CTF{hello_world}"]),
            ("Who signed the note?", "Name (case-sensitive)", 50, ["Ada", "Ada Lovelace"]),
        ],
        "hint": ("A place to begin", "Read the welcome.txt attachment from top to bottom.", 20),
    },
    {
        "slug": "quiet-channel", "title": "A quiet channel",
        "summary": "A transmission came through. The message is there, if you know how to read it.",
        "description": "Decode the captured transmission. This challenge belongs to two pools; each question and hint still counts only once in your overall score.",
        "pools": ["open-source", "first-steps"],
        "attachment": ("transmission.txt", "Encoding: Base64\nQ1RGe2xpc3Rlbl9jbG9zZWx5fQ==\n"),
        "questions": [
            ("Which encoding was used?", "Encoding name", 50, ["Base64", "base64"]),
            ("What does the transmission say?", "CTF{...}", 150, ["CTF{listen_closely}"]),
        ],
        "hint": ("About the encoding", "Base64 is reversible encoding. The decoded text is your flag.", 30),
    },
    {
        "slug": "open-door", "title": "The open door", "lab": True,
        "summary": "An unfamiliar terminal. A forgotten note. Where will you look first?",
        "description": "Use the private Kali attacker workspace to investigate its paired target. Questions remain usable without starting a lab.",
        "pools": ["the-lab"],
        "questions": [
            ("What is your attacker username?", "Linux username", 50, ["explorer"]),
            ("Recover the target flag.", "CTF{...}", 200, ["CTF{look_around}"]),
        ],
        "hint": ("Find the note", "In the workspace, run ls, then cat notes.txt. It points to /opt/flag.txt on the target.", 40),
    },
    {
        "slug": "paper-trail", "title": "Paper trail",
        "summary": "The story changed, but an old version did not quite disappear.",
        "description": "Compare the two release notes. The removed reference may be useful.",
        "pools": ["open-source"],
        "attachment": ("release-notes.txt", "VERSION 1.0\nInternal reference: CTF{nothing_is_ever_gone}\nVERSION 1.1\nInternal reference: [removed]\n"),
        "questions": [
            ("What reference was removed?", "CTF{...}", 200, ["CTF{nothing_is_ever_gone}"]),
        ],
        "hint": ("Compare the versions", "The internal reference is present in version 1.0.", 25),
    },
    {
        "slug": "hidden-in-sight", "title": "Hidden in sight",
        "summary": "Look a little closer. This one needs only an answer.",
        "description": "Question-only challenges are normal here. Read the prompt carefully; no files or machine are required.",
        "pools": ["first-steps"],
        "questions": [
            ('In the text "f l a g", remove the spaces. What word remains?', "Lowercase word", 150, ["flag"]),
        ],
        "hint": ("Four letters", "Join the four lowercase letters together.", 10),
    },
    {
        "slug": "after-hours", "title": "After hours", "lab": True,
        "summary": "The last person out left a log. Pick up the trail.",
        "description": "Read the target activity log from your attacker workspace. Saving the lab preserves both guests in the production design.",
        "pools": ["the-lab", "open-source"],
        "questions": [
            ("What message was left at the end of the shift?", "CTF{...}", 250, ["CTF{the_night_shift}"]),
        ],
        "hint": ("Read the log", "In the workspace, run cat activity.log from the paired target.", 35),
    },
]


class Command(BaseCommand):
    help = "Seed demo content (pools, challenges, questions, hints, attachments, lab templates). Idempotent."

    def add_arguments(self, parser):
        parser.add_argument("--update", action="store_true", help="Refresh existing content fields from these definitions.")

    def handle(self, *args, **options):
        update = options["update"]
        with transaction.atomic():
            pools = {}
            for slug, name, description in POOLS:
                pool, created = Pool.objects.get_or_create(slug=slug, defaults={"name": name, "description": description})
                if update and not created:
                    pool.name, pool.description = name, description
                    pool.save()
                pools[slug] = pool
            self.stdout.write(f"pools: {len(pools)}")

            for definition in CHALLENGES:
                challenge, created = Challenge.objects.get_or_create(
                    slug=definition["slug"],
                    defaults={
                        "title": definition["title"],
                        "summary": definition["summary"],
                        "description": definition["description"],
                        "lifecycle": Challenge.Lifecycle.PUBLISHED,
                    },
                )
                if not created and update:
                    challenge.title = definition["title"]
                    challenge.summary = definition["summary"]
                    challenge.description = definition["description"]
                    challenge.save()
                if definition.get("lab"):
                    template, _ = LabTemplate.objects.get_or_create(
                        attacker_version=LAB_TEMPLATE_DEFAULTS["attacker_version"],
                        target_version=LAB_TEMPLATE_DEFAULTS["target_version"],
                        defaults=LAB_TEMPLATE_DEFAULTS,
                    )
                    if template.challenge_id != challenge.id:
                        template.challenge = challenge
                        template.save(update_fields=("challenge",))
                for slug in definition["pools"]:
                    PoolChallenge.objects.get_or_create(pool=pools[slug], challenge=challenge)

                existing_prompts = {question.prompt: question for question in challenge.questions.all()}
                for order, (prompt, input_format, points, answers) in enumerate(definition["questions"]):
                    question = existing_prompts.get(prompt)
                    if question is None:
                        question = Question.objects.create(
                            challenge=challenge, prompt=prompt, input_format=input_format,
                            points=points, display_order=order,
                        )
                    elif update:
                        question.input_format, question.points, question.display_order = input_format, points, order
                        question.save()
                    current = set(AcceptedAnswer.objects.filter(question=question, active=True).values_list("value", flat=True))
                    for answer in answers:
                        if answer not in current:
                            row = AcceptedAnswer.objects.filter(question=question, value=answer).first()
                            if row:
                                row.active = True
                                row.save(update_fields=("active",))
                            else:
                                AcceptedAnswer.objects.create(question=question, value=answer)

                hint_definition = definition["hint"]
                hint, created = Hint.objects.get_or_create(
                    challenge=challenge, description=hint_definition[0],
                    defaults={"text": hint_definition[1], "cost": hint_definition[2]},
                )
                if not created and update:
                    hint.text, hint.cost = hint_definition[1], hint_definition[2]
                    hint.save()

                attachment_definition = definition.get("attachment")
                if attachment_definition and (not challenge.attachments.exists() or update):
                    # Re-save on --update so files can be repaired after volume loss.
                    challenge.attachments.all().delete()
                    filename, content = attachment_definition
                    attachment = Attachment(challenge=challenge, filename=filename)
                    attachment.file.save(filename, ContentFile(content.encode()), save=False)
                    attachment.size = len(content)
                    attachment.save()

                self.stdout.write(f"challenge {challenge.slug}: {challenge.questions.count()} question(s), "
                                  f"{challenge.hints.count()} hint(s), {challenge.attachments.count()} attachment(s)")

        self.stdout.write(self.style.SUCCESS("Demo content seeded. Launch content is still owner-supplied — edit or replace before launch."))
