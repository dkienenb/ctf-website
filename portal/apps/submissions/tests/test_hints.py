"""Hint economics and their negatives (handoff §9, owner decisions §4/§9):
redemption ownership, retroactive pricing, refunds, deletion rules, and
audit behavior."""
import uuid

from django.test import TestCase, override_settings

from portal.apps.accounts.models import User
from portal.apps.content.models import Challenge, Hint
from portal.apps.submissions import adminops, services
from portal.apps.submissions.models import AuditEvent, DataRevision, HintRedemption, Notification
from portal.apps.submissions.services import ServiceError
from portal.apps.testutils import make_question, make_user


@override_settings(SUBMISSION_COOLDOWN_SECONDS=0)
class HintRedemptionTests(TestCase):
    def setUp(self):
        self.user = make_user("350000000000000001", "Alex")
        self.other = make_user("350000000000000002", "Sam")
        self.question = make_question(["flag"], points=100)
        self.hint = Hint.objects.create(
            challenge_id=self.question.challenge.id, description="d", text="t", cost=20,
        )

    def _score(self, user):
        from portal.apps.submissions.models import Solve
        return Solve.objects.filter(user=user).count()

    def score_value(self, user):
        from portal.apps.submissions.models import Solve, Submission
        raise NotImplementedError  # replaced below; scoring read via services

    def test_redeem_unlocks_text_and_records_ownership(self):
        result = services.redeem(self.user, self.hint.id)
        self.assertEqual(result["text"], "t")
        self.assertTrue(HintRedemption.objects.filter(user=self.user, hint=self.hint).exists())

    def test_redeem_twice_is_idempotent_never_double_deducted(self):
        """One ownership row per user/hint: re-opening a hint re-shows the
        text and is never scored twice."""
        services.redeem(self.user, self.hint.id)
        result = services.redeem(self.user, self.hint.id)
        self.assertEqual(result["text"], "t")
        self.assertEqual(HintRedemption.objects.filter(user=self.user).count(), 1)

    def test_two_users_may_own_the_same_hint(self):
        services.redeem(self.user, self.hint.id)
        services.redeem(self.other, self.hint.id)
        self.assertEqual(HintRedemption.objects.filter(hint=self.hint).count(), 2)

    def test_unapproved_players_cannot_redeem(self):
        locked = make_user("350000000000000003", "Locked", status=User.Status.PENDING)
        with self.assertRaises(ServiceError):
            services.redeem(locked, self.hint.id)

    def test_invisible_challenge_blocks_redeem(self):
        challenge = Challenge.objects.get(id=self.hint.challenge_id)
        challenge.lifecycle = Challenge.Lifecycle.DRAFT
        challenge.save()
        with self.assertRaises(ServiceError):
            services.redeem(self.user, self.hint.id)

    def test_unknown_hint_is_404(self):
        with self.assertRaises(ServiceError) as error:
            services.redeem(self.user, 999999)
        self.assertEqual(error.exception.status, 404)

    def test_price_change_applies_retroactively_to_owner(self):
        """Retroactive pricing (§9): redemptions store ownership only, so the
        preview always compares against the hint's CURRENT price."""
        services.redeem(self.user, self.hint.id)
        preview = adminops.preview_hint_edit(self.hint, cost=5, description="d", text="t")
        self.assertEqual(preview["effects"], [
            {"user_id": self.user.id, "before": -20, "after": -5, "score_delta": 15},
        ])
        # After applying the change, a further preview has no delta to report.
        self.hint.cost = 5
        self.hint.save()
        preview = adminops.preview_hint_edit(self.hint, cost=5, description="d", text="t")
        self.assertEqual(preview["effects"], [
            {"user_id": self.user.id, "before": -5, "after": -5, "score_delta": 0},
        ])


@override_settings(SUBMISSION_COOLDOWN_SECONDS=0)
class HintPricingWorkflowTests(TestCase):
    def setUp(self):
        self.admin = make_user("360000000000000001", "Admin", is_admin=True)
        self.user = make_user("360000000000000002", "Alex")
        self.other = make_user("360000000000000003", "Sam")
        self.question = make_question(["flag"], points=100)
        self.hint = Hint.objects.create(
            challenge_id=self.question.challenge.id, description="d", text="t", cost=20,
        )
        services.redeem(self.user, self.hint.id)
        services.redeem(self.other, self.hint.id)
        DataRevision.current()

    def test_preview_lists_every_owner_and_does_not_change_data(self):
        revision_before = DataRevision.current()
        preview = adminops.preview_hint_edit(self.hint, cost=10, description="d2", text="t2")
        effects = {effect["user_id"]: effect for effect in preview["effects"]}
        self.assertEqual(effects[self.user.id]["score_delta"], 10)
        self.assertEqual(effects[self.other.id]["score_delta"], 10)
        self.assertEqual(preview["redeemed_count"], 2)
        self.assertEqual(DataRevision.current(), revision_before)  # preview mutates nothing

    def test_preview_validation_negatives(self):
        for kwargs in (
            {"cost": -1}, {"cost": True}, {"cost": "10"}, {"cost": None},
            {"description": "   "}, {"text": ""},
        ):
            base = {"cost": 10, "description": "d", "text": "t"}
            base.update(kwargs)
            with self.assertRaises(ServiceError, msg=kwargs):
                adminops.preview_hint_edit(self.hint, **base)

    def test_apply_reprices_everyone_and_notifies(self):
        result = adminops.apply_hint_edit(
            self.admin, self.hint,
            cost=10, description="d2", text="t2",
            reason="typo fix", expected_revision=DataRevision.current(),
        )
        self.assertIn("recalculated", result["message"])
        self.hint.refresh_from_db()
        self.assertEqual(self.hint.cost, 10)
        self.assertEqual(self.hint.text, "t2")
        notified = Notification.objects.filter(message__icontains="price changed")
        self.assertEqual(notified.count(), 2)
        self.assertTrue(AuditEvent.objects.filter(operation="hint-edit").exists())

    def test_apply_with_stale_revision_is_refused(self):
        expected = DataRevision.current()
        DataRevision.bump()
        with self.assertRaises(ServiceError) as error:
            adminops.apply_hint_edit(
                self.admin, self.hint,
                cost=10, description="d", text="t",
                reason="late", expected_revision=expected,
            )
        self.assertEqual(error.exception.status, 409)
        self.hint.refresh_from_db()
        self.assertEqual(self.hint.cost, 20)  # nothing applied

    def test_apply_requires_reason(self):
        with self.assertRaises(ServiceError):
            adminops.apply_hint_edit(
                self.admin, self.hint,
                cost=10, description="d", text="t",
                reason="", expected_revision=DataRevision.current(),
            )

    def test_non_admin_cannot_apply(self):
        with self.assertRaises(ServiceError) as error:
            adminops.apply_hint_edit(
                self.user, self.hint,
                cost=10, description="d", text="t",
                reason="sneaky", expected_revision=DataRevision.current(),
            )
        self.assertEqual(error.exception.status, 403)


@override_settings(SUBMISSION_COOLDOWN_SECONDS=0)
class HintRefundAndDeletionTests(TestCase):
    def setUp(self):
        self.admin = make_user("370000000000000001", "Admin", is_admin=True)
        self.user = make_user("370000000000000002", "Alex")
        self.question = make_question(["flag"], points=100)
        self.hint = Hint.objects.create(
            challenge_id=self.question.challenge.id, description="d", text="t", cost=20,
        )
        services.redeem(self.user, self.hint.id)

    def test_refund_removes_ownership_and_notifies(self):
        hint = adminops.refund_hint(self.admin, self.user.id, self.hint.id)
        self.assertEqual(hint.id, self.hint.id)
        self.assertFalse(HintRedemption.objects.filter(user=self.user, hint=self.hint).exists())
        self.assertTrue(Notification.objects.filter(
            recipient=self.user, message__icontains="refunded").exists())
        self.assertTrue(AuditEvent.objects.filter(operation="hint-refund").exists())

    def test_refunded_hint_can_be_redeemed_again(self):
        adminops.refund_hint(self.admin, self.user.id, self.hint.id)
        services.redeem(self.user, self.hint.id)  # ownership back at the CURRENT price
        self.assertTrue(HintRedemption.objects.filter(user=self.user).exists())

    def test_refund_without_redemption_is_404(self):
        stranger = make_user("370000000000000003", "Nobody")
        with self.assertRaises(ServiceError) as error:
            adminops.refund_hint(self.admin, stranger.id, self.hint.id)
        self.assertEqual(error.exception.status, 404)

    def test_refund_of_wrong_user_hint_pair_is_404(self):
        other_hint = Hint.objects.create(challenge_id=self.question.challenge.id, description="x", text="t", cost=1)
        with self.assertRaises(ServiceError):
            adminops.refund_hint(self.admin, self.user.id, other_hint.id)  # never redeemed

    def test_redeemed_hint_is_never_deletable(self):
        with self.assertRaises(ServiceError) as error:
            adminops.delete_hint(self.admin, self.hint.id)
        self.assertEqual(error.exception.status, 409)
        self.assertTrue(Hint.objects.filter(pk=self.hint.id).exists())

    def test_unredeemed_hint_is_deletable(self):
        spare = Hint.objects.create(challenge_id=self.question.challenge.id, description="s", text="t", cost=3)
        adminops.delete_hint(self.admin, spare.id)
        self.assertFalse(Hint.objects.filter(pk=spare.id).exists())

    def test_delete_unknown_hint_is_404(self):
        with self.assertRaises(ServiceError):
            adminops.delete_hint(self.admin, 999999)

    def test_non_admin_cannot_refund_or_delete(self):
        with self.assertRaises(ServiceError):
            adminops.refund_hint(self.user, self.user.id, self.hint.id)
        with self.assertRaises(ServiceError):
            adminops.delete_hint(self.user, self.hint.id)
        self.assertTrue(HintRedemption.objects.filter(user=self.user).exists())
