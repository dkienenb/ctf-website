"""Server-rendered page flow tests (server-rendered templates, handoff §14)."""
import uuid
from io import BytesIO

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from portal.apps.accounts.models import User
from portal.apps.content.models import Attachment, Challenge, Pool, PoolChallenge, Question, AcceptedAnswer, Hint
from portal.apps.submissions.models import Solve

from .tests import make_question, make_user  # reuse helpers


@override_settings(SUBMISSION_COOLDOWN_SECONDS=0)
class PageFlowTests(TestCase):
    def setUp(self):
        self.approved = make_user("200000000000000001", "Alex")
        self.pending = make_user("200000000000000002", "Taylor", status=User.Status.PENDING)
        self.client.force_login(self.pending)

    def test_pending_users_see_only_status(self):
        response = self.client.get("/")
        self.assertRedirects(response, "/status/")
        self.assertContains(self.client.get("/status/"), "Awaiting approval")
        response = self.client.get("/leaderboard/")
        self.assertRedirects(response, "/status/")

    def test_approved_user_browses_and_solves(self):
        self.client.force_login(self.approved)
        question = make_question(["CTF{x}"], points=100)
        response = self.client.get(f"/challenges/{question.challenge.slug}/")
        self.assertContains(response, question.challenge.title)
        self.assertContains(response, "Submit answer")
        # Submit the correct answer through the rendered form path.
        self.client.post(
            f"/challenges/{question.challenge.slug}/questions/{question.id}/submit",
            {"answer": " CTF{x} ", "request_id": uuid.uuid4().hex},
        )
        self.assertTrue(Solve.objects.filter(user=self.approved, question=question).exists())
        response = self.client.get(f"/challenges/{question.challenge.slug}/")
        self.assertContains(response, "Solved")
        self.assertNotContains(response, "Submit answer")

    def test_hint_redeem_reveals_protected_text(self):
        self.client.force_login(self.approved)
        question = make_question(["x"])
        hint = Hint.objects.create(challenge=question.challenge, description="Look closer", text="protected text", cost=10)
        response = self.client.get(f"/challenges/{question.challenge.slug}/")
        self.assertContains(response, "Look closer")
        self.assertNotContains(response, "protected text")
        self.client.post(f"/challenges/{question.challenge.slug}/hints/{hint.id}/redeem")
        response = self.client.get(f"/challenges/{question.challenge.slug}/")
        self.assertContains(response, "protected text")

    def test_leaderboard_and_progress(self):
        self.client.force_login(self.approved)
        question = make_question(["CTF{x}"], pools=("Alpha",))
        self.client.post(
            f"/challenges/{question.challenge.slug}/questions/{question.id}/submit",
            {"answer": "CTF{x}", "request_id": uuid.uuid4().hex},
        )
        response = self.client.get("/leaderboard/?pool=alpha")
        self.assertContains(response, self.approved.cached_name)
        response = self.client.get("/progress/")
        self.assertContains(response, "100")  # earned points explanation

    def test_attachment_download_requires_visibility_and_approval(self):
        self.client.force_login(self.approved)
        question = make_question(["x"])
        attachment = Attachment(challenge=question.challenge, filename="notes.txt")
        attachment.file.save("notes.txt", ContentFile(b"FIELD NOTES"), save=True)
        try:
            response = self.client.get(f"/challenges/{question.challenge.slug}/attachments/{attachment.id}/download")
            self.assertEqual(b"".join(response.streaming_content), b"FIELD NOTES")
            # Unavailable challenges never serve files.
            question.challenge.lifecycle = Challenge.Lifecycle.ARCHIVED
            question.challenge.save()
            response = self.client.get(f"/challenges/{question.challenge.slug}/attachments/{attachment.id}/download")
            self.assertEqual(response.status_code, 404)
        finally:
            attachment.file.delete(save=False)
        # Pending accounts are redirected away from protected pages entirely.
        self.client.force_login(self.pending)
        self.assertRedirects(self.client.get("/"), "/status/")

    def test_idempotent_form_resubmission(self):
        """Double-submitted forms reuse the rendered request id: no extra attempt."""
        self.client.force_login(self.approved)
        question = make_question(["CTF{x}"])
        request_id = uuid.uuid4().hex
        for _ in range(2):
            self.client.post(
                f"/challenges/{question.challenge.slug}/questions/{question.id}/submit",
                {"answer": "CTF{x}", "request_id": request_id},
            )
        self.assertEqual(Solve.objects.filter(user=self.approved, question=question).count(), 1)
        self.assertEqual(
            Submission_eligible(question), 1
        )

    def test_discord_profile_refresh_preserves_identity(self):
        """The refresh endpoint cannot change identity/approval; without a token it fails safely."""
        self.client.force_login(self.approved)
        response = self.client.post("/settings/profile/refresh", follow=True)
        self.approved.refresh_from_db()
        self.assertEqual(self.approved.discord_id, "200000000000000001")
        self.assertEqual(self.approved.status, User.Status.APPROVED)
        self.assertContains(response, "unchanged")


def Submission_eligible(question):
    from portal.apps.submissions.models import Submission

    return Submission.objects.filter(question=question, eligible=True).count()
