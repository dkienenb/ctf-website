"""Content authoring workflows and their negatives (handoff §4, §5, §9):
creation validation, lifecycle transitions, go-live scheduling, draft
deletion, edit-mode revision guards, and attachment privacy."""
import uuid

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from portal.apps.accounts.models import User
from portal.apps.content.models import (
    AcceptedAnswer, Attachment, Challenge, Hint, Pool, PoolChallenge, Question,
)
from portal.apps.submissions import adminops, services
from portal.apps.submissions.models import DataRevision, HintRedemption, Submission
from portal.apps.submissions.services import ServiceError
from portal.apps.testutils import make_question, make_user


@override_settings(SUBMISSION_COOLDOWN_SECONDS=0)
class ChallengeCreationTests(TestCase):
    def setUp(self):
        self.admin = make_user("300000000000000001", "Admin", is_admin=True)
        self.pool = Pool.objects.create(name="Pools Are Real", slug="real")

    def _create(self, questions=None, **overrides):
        kwargs = {
            "title": "T", "pool_ids": [self.pool.id],
            "questions": questions if questions is not None else [
                {"prompt": "p", "answers": ["a"], "points": 10},
            ],
        }
        kwargs.update(overrides)
        return adminops.create_challenge(self.admin, **kwargs)

    def test_create_builds_first_question_and_answers(self):
        challenge = self._create(questions=[{"prompt": "p", "answers": [" flag-one "], "points": 42}])
        self.assertEqual(challenge.lifecycle, Challenge.Lifecycle.DRAFT)
        question = challenge.questions.get()
        self.assertEqual(question.points, 42)
        self.assertEqual(list(question.accepted_answers.values_list("value", flat=True)), ["flag-one"])

    def test_create_with_multiple_questions_and_alternate_answers(self):
        challenge = self._create(title="Multi", questions=[
            {"prompt": "q1", "answers": ["a1", "a2"], "points": 10},
            {"prompt": "q2", "answers": ["b1"], "points": 90},
            {"prompt": "q3", "answers": ["c1", "c2", "c3"], "points": 0},
        ])
        self.assertEqual(challenge.questions.count(), 3)
        orders = list(challenge.questions.order_by("display_order").values_list("display_order", "points"))
        self.assertEqual(orders, [(0, 10), (1, 90), (2, 0)])
        second = challenge.questions.order_by("display_order")[1]
        self.assertEqual(second.accepted_answers.count(), 1)
        third = challenge.questions.order_by("display_order")[2]
        self.assertEqual(third.accepted_answers.count(), 3)

    def test_multiple_question_validation_negatives_create_nothing(self):
        bad_lists = [
            [],                                                        # none at all
            [{"prompt": "ok", "answers": ["a"], "points": 1},
             {"prompt": "  ", "answers": ["a"], "points": 1}],          # blank prompt in #2
            [{"prompt": "ok", "answers": [], "points": 1}],             # no answers
            [{"prompt": "ok", "answers": ["a", "  "], "points": 1}],    # blank answer
            [{"prompt": "ok", "answers": ["a"], "points": -3}],         # negative points
            [{"prompt": "ok", "answers": ["a"], "points": True}],       # bool points
        ]
        for questions in bad_lists:
            with self.assertRaises(ServiceError, msg=questions):
                self._create(title="Multi", questions=questions)
        self.assertFalse(Challenge.objects.exists())  # all-or-nothing: no partial challenges

    def test_slashes_and_slugs_are_unique_across_same_titled_challenges(self):
        one, two = self._create(title="Same Name"), self._create(title="Same Name")
        self.assertNotEqual(one.slug, two.slug)

    def test_creation_validation_negatives(self):
        for kwargs in (
            {"title": "   "},
            {"pool_ids": ["not-a-pool"]},
            {"author": "x" * 101},
            {"questions": [{"prompt": "  ", "answers": ["a"], "points": 1}]},
            {"questions": [{"prompt": "p", "answers": ["  "], "points": 1}]},
            {"questions": [{"prompt": "p", "answers": ["a"], "points": -1}]},
            {"questions": [{"prompt": "p", "answers": ["a"], "points": True}]},
            {"questions": [{"prompt": "p", "answers": ["a"], "points": "ten"}]},
        ):
            with self.assertRaises(ServiceError, msg=kwargs):
                self._create(**kwargs)
        self.assertFalse(Challenge.objects.exists())  # nothing partially created

    def test_poolless_draft_is_allowed_and_labeled(self):
        """A pool-less draft is valid at creation; the edit flow requires
        pools before anything is player-visible."""
        challenge = self._create(pool_ids=[])
        self.assertEqual(challenge.memberships.count(), 0)
        with self.assertRaises(ServiceError):
            services._visible(challenge)  # drafts are invisible regardless

    def test_create_in_multiple_pools_at_once(self):
        second = Pool.objects.create(name="Second Pool", slug="second")
        challenge = self._create(pool_ids=[self.pool.id, second.id], title="Dual")
        self.assertEqual(
            set(challenge.memberships.values_list("pool_id", flat=True)),
            {self.pool.id, second.id},
        )

    def test_create_sets_display_author(self):
        challenge = self._create(title="Credited", author=" Dana ")
        self.assertEqual(challenge.author, "Dana")


class LifecycleTests(TestCase):
    def setUp(self):
        self.admin = make_user("310000000000000001", "Admin", is_admin=True)

    def _draft(self):
        return Challenge.objects.create(title="Drafty", slug=f"drafty-{Challenge.objects.count()}")

    def test_set_lifecycle_publishes_and_archives(self):
        challenge = self._draft()
        adminops.set_lifecycle(self.admin, challenge.id, Challenge.Lifecycle.PUBLISHED)
        challenge.refresh_from_db()
        self.assertEqual(challenge.lifecycle, Challenge.Lifecycle.PUBLISHED)
        adminops.set_lifecycle(self.admin, challenge.id, Challenge.Lifecycle.ARCHIVED)
        challenge.refresh_from_db()
        self.assertEqual(challenge.lifecycle, Challenge.Lifecycle.ARCHIVED)

    def test_lifecycle_negatives(self):
        challenge = self._draft()
        with self.assertRaises(ServiceError):
            adminops.set_lifecycle(self.admin, challenge.id, "vanished")
        with self.assertRaises(ServiceError):
            adminops.set_lifecycle(self.admin, 99999, Challenge.Lifecycle.PUBLISHED)  # unknown challenge

    def test_edit_content_publishes_with_future_go_live(self):
        """Datetime go-live flows through edit_content (set_lifecycle is the
        numeric variant; the operator page uses datetimes)."""
        from django.utils import timezone as tz
        self._pool = Pool.objects.create(name="G", slug="g")
        challenge = self._draft()
        future = tz.make_aware(tz.datetime(3000, 1, 1))
        adminops.edit_content(
            self.admin, challenge,
            title=challenge.title, summary="", description="",
            pool_ids=[self._pool.id],
            expected_revision=DataRevision.current(),
            lifecycle=Challenge.Lifecycle.PUBLISHED, go_live=future,
        )
        challenge.refresh_from_db()
        self.assertEqual(challenge.lifecycle, Challenge.Lifecycle.PUBLISHED)
        self.assertEqual(challenge.go_live, future)
        with self.assertRaises(ServiceError):
            services._visible(challenge, when=tz.now())
        # go_live=None means "keep the current value" in edit_content — the
        # documented rule for operator edits (lifecycle is what changes).
        adminops.edit_content(
            self.admin, challenge,
            title=challenge.title, summary="", description="",
            pool_ids=[self._pool.id],
            expected_revision=DataRevision.current(),
            lifecycle=Challenge.Lifecycle.PUBLISHED, go_live=None,
        )
        challenge.refresh_from_db()
        self.assertEqual(challenge.go_live, future)

    def test_non_admin_cannot_change_lifecycle(self):
        challenge = self._draft()
        with self.assertRaises(ServiceError) as error:
            adminops.set_lifecycle(None, challenge.id, Challenge.Lifecycle.PUBLISHED)
        self.assertEqual(error.exception.status, 403)


class DraftDeletionTests(TestCase):
    def setUp(self):
        self.admin = make_user("320000000000000001", "Admin", is_admin=True)

    def _draft_with_kit(self, *, submissions=False, redeemed=False):
        challenge = Challenge.objects.create(title="Kit", slug=f"kit-{Challenge.objects.count()}")
        question = Question.objects.create(challenge=challenge, prompt="p", points=10)
        AcceptedAnswer.objects.create(question=question, value="a")
        hint = Hint.objects.create(challenge=challenge, description="d", text="t", cost=5)
        if submissions:
            services.submit(self.admin, question.id, "x", f"req-{uuid.uuid4().hex[:8]}")
        if redeemed:
            HintRedemption.objects.create(user=self.admin, hint=hint)
        return challenge, question, hint

    def test_deletes_clean_draft_completely(self):
        challenge, question, hint = self._draft_with_kit()
        adminops.delete_draft(self.admin, challenge.id)
        self.assertFalse(Challenge.objects.filter(pk=challenge.id).exists())
        self.assertFalse(Question.objects.filter(pk=question.id).exists())
        self.assertFalse(Hint.objects.filter(pk=hint.id).exists())

    def test_refuses_published_and_archived(self):
        for lifecycle in (Challenge.Lifecycle.PUBLISHED, Challenge.Lifecycle.ARCHIVED):
            challenge, _, _ = self._draft_with_kit()
            challenge.lifecycle = lifecycle
            challenge.save()
            with self.assertRaises(ServiceError) as error:
                adminops.delete_draft(self.admin, challenge.id)
            self.assertEqual(error.exception.status, 409)

    def test_refuses_submission_history(self):
        """A challenge that gained submissions then went back to draft still
        cannot be deleted: attempts are audit data, never erased."""
        challenge, _, _ = self._draft_with_kit()
        adminops.set_lifecycle(self.admin, challenge.id, Challenge.Lifecycle.PUBLISHED)
        question = challenge.questions.get()
        services.submit(self.admin, question.id, "x", f"req-{uuid.uuid4().hex[:8]}")
        adminops.set_lifecycle(self.admin, challenge.id, Challenge.Lifecycle.DRAFT)
        with self.assertRaises(ServiceError) as error:
            adminops.delete_draft(self.admin, challenge.id)
        self.assertEqual(error.exception.status, 409)

    def test_refuses_redeemed_hints_instead_of_crashing(self):
        """Regression: a redeemed hint made draft deletion die with a raw
        ProtectedError; operators must see a refusal and a remedy."""
        challenge, _, _ = self._draft_with_kit(redeemed=True)
        with self.assertRaises(ServiceError) as error:
            adminops.delete_draft(self.admin, challenge.id)
        self.assertEqual(error.exception.status, 409)
        self.assertTrue(Challenge.objects.filter(pk=challenge.id).exists())

    def test_refuses_unknown_challenge(self):
        with self.assertRaises(ServiceError):
            adminops.delete_draft(self.admin, 999999)


class ContentEditTests(TestCase):
    def setUp(self):
        self.admin = make_user("330000000000000001", "Admin", is_admin=True)
        self.pool_a = Pool.objects.create(name="A", slug="a")
        self.pool_b = Pool.objects.create(name="B", slug="b")
        self.challenge = make_question(["x"]).challenge

    def _edit(self, **overrides):
        kwargs = {
            "title": "Renamed", "summary": "s", "description": "d",
            "pool_ids": [self.pool_a.id], "expected_revision": DataRevision.current(),
        }
        kwargs.update(overrides)
        return adminops.edit_content(self.admin, self.challenge, **kwargs)

    def test_edit_updates_and_rewires_pools(self):
        self._edit(pool_ids=[self.pool_a.id, self.pool_b.id])
        pools = set(self.challenge.memberships.values_list("pool_id", flat=True))
        self.assertEqual(pools, {self.pool_a.id, self.pool_b.id})
        self._edit(pool_ids=[self.pool_b.id])
        pools = set(self.challenge.memberships.values_list("pool_id", flat=True))
        self.assertEqual(pools, {self.pool_b.id})  # rewired, not appended

    def test_stale_revision_is_refused(self):
        DataRevision.bump()  # someone else changed data after the page loaded
        with self.assertRaises(ServiceError) as error:
            self._edit(expected_revision=DataRevision.current() - 1)
        self.assertEqual(error.exception.status, 409)

    def test_edit_negatives(self):
        for kwargs in ({"title": "  "}, {"pool_ids": []}, {"pool_ids": [None]},
                       {"pool_ids": ["nope"]}, {"lifecycle": "weird"}):
            with self.assertRaises(ServiceError, msg=kwargs):
                self._edit(**kwargs)


class AttachmentPrivacyTests(TestCase):
    def setUp(self):
        self.admin = make_user("340000000000000001", "Admin", is_admin=True)
        self.player = make_user("340000000000000002", "Player")
        self.stranger = make_user("340000000000000003", "Stranger", status=User.Status.PENDING)
        self.client.force_login(self.admin)
        self.challenge = make_question(["x"]).challenge

    def _upload(self, name="notes.txt", body=b"secret-flag-material"):
        return self.client.post(
            reverse("manage-attachment-upload", args=[self.challenge.id]),
            {"file": SimpleUploadedFile(name, body)},
        )

    def test_upload_records_size_and_private_storage(self):
        response = self._upload()
        self.assertEqual(response.status_code, 302)
        attachment = Attachment.objects.get(challenge=self.challenge)
        self.assertEqual(attachment.size, len(b"secret-flag-material"))
        # Private storage (§19): never served by a static route.
        self.assertEqual(attachment.file.storage.location, str(settings.PRIVATE_ATTACHMENTS_DIR))

    def test_download_requires_approved_account(self):
        self._upload()
        attachment = Attachment.objects.get(challenge=self.challenge)
        url = reverse("attachment-download", args=[self.challenge.slug, attachment.id])
        self.client.force_login(self.stranger)  # pending
        self.assertEqual(self.client.get(url).status_code, 302)  # redirected to status
        self.client.force_login(self.player)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)  # anonymous → login

    def test_download_unknown_attachment_is_404(self):
        self._upload()
        self.client.force_login(self.player)
        response = self.client.get(reverse("attachment-download", args=[self.challenge.slug, 99999]))
        self.assertEqual(response.status_code, 404)

    def test_upload_requires_a_file(self):
        response = self.client.post(reverse("manage-attachment-upload", args=[self.challenge.id]), {})
        self.assertEqual(response.status_code, 302)  # back to edit page with an error message
        self.assertFalse(Attachment.objects.exists())

    def test_attachment_upload_requires_admin(self):
        self.client.force_login(self.player)
        response = self._upload()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Attachment.objects.exists())
