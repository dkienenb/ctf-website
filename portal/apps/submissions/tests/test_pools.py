"""Pool management: create/edit/delete services, operator pages, and the
player-facing effects of every pool operation."""
import uuid

from django.test import TestCase, override_settings
from django.urls import reverse

from portal.apps.content.models import Challenge, Pool, PoolChallenge
from portal.apps.submissions import adminops
from portal.apps.submissions.models import AuditEvent, DataRevision
from portal.apps.submissions.services import ServiceError
from portal.apps.testutils import make_question, make_user


class PoolServiceTests(TestCase):
    def setUp(self):
        self.admin = make_user("600000000000000090", "Admin", is_admin=True)

    def test_create_assigns_unique_slug_and_fields(self):
        one = adminops.create_pool(self.admin, name="Web", description="web stuff", order=3)
        self.assertEqual(one.name, "Web")
        self.assertEqual(one.description, "web stuff")
        self.assertEqual(one.order, 3)
        self.assertTrue(one.slug.startswith("web-"))  # slugified, uniqueness-suffixed
        # A different name that slugifies identically still gets a unique slug.
        two = adminops.create_pool(self.admin, name="Web!")
        self.assertNotEqual(one.slug, two.slug)
        self.assertTrue(two.slug.startswith("web-"))

    def test_create_negatives(self):
        adminops.create_pool(self.admin, name="Real")
        with self.assertRaises(ServiceError) as error:
            adminops.create_pool(self.admin, name="real")  # case-insensitive duplicate
        self.assertEqual(error.exception.status, 409)
        for kwargs in (
            {"name": "   "}, {"name": None},
            {"name": "x" * 101},
            {"name": "Ok", "order": -1}, {"name": "Ok", "order": True}, {"name": "Ok", "order": "1"},
            {"name": "Ok", "description": 12},
        ):
            with self.assertRaises(ServiceError, msg=kwargs):
                adminops.create_pool(self.admin, **kwargs)
        self.assertEqual(Pool.objects.count(), 1)  # failed creates leave nothing

    def test_non_admin_cannot_manage_pools(self):
        pleb = make_user("600000000000000091", "Pleb")
        pool = adminops.create_pool(self.admin, name="Admin only")
        for operation in (
            lambda: adminops.create_pool(pleb, name="Nope"),
            lambda: adminops.edit_pool(pleb, pool.id, name="Nope"),
            lambda: adminops.delete_pool(pleb, pool.id),
        ):
            with self.assertRaises(ServiceError) as error:
                operation()
            self.assertEqual(error.exception.status, 403)

    def test_edit_updates_fields_but_keeps_slug(self):
        pool = adminops.create_pool(self.admin, name="Original")
        original_slug = pool.slug
        adminops.edit_pool(self.admin, pool.id, name="Renamed", description="new words", order=7)
        pool.refresh_from_db()
        self.assertEqual(pool.name, "Renamed")
        self.assertEqual(pool.description, "new words")
        self.assertEqual(pool.order, 7)
        self.assertEqual(pool.slug, original_slug)  # slugs are immutable: links keep working

    def test_edit_negatives(self):
        pool = adminops.create_pool(self.admin, name="Mine")
        other = adminops.create_pool(self.admin, name="Theirs")
        with self.assertRaises(ServiceError) as error:
            adminops.edit_pool(self.admin, other.id, name="mine")  # case-insensitive clash
        self.assertEqual(error.exception.status, 409)
        adminops.edit_pool(self.admin, pool.id, name="Mine")  # renaming to its own name is fine
        with self.assertRaises(ServiceError):
            adminops.edit_pool(self.admin, 999999, name="Ghost")
        for kwargs in ({"name": ""}, {"order": -2}, {"name": "Ok", "order": None}):
            with self.assertRaises(ServiceError, msg=kwargs):
                adminops.edit_pool(self.admin, pool.id, name=kwargs.get("name", "Mine"), **{
                    k: v for k, v in kwargs.items() if k != "name"
                })

    def test_delete_removes_grouping_but_never_challenges(self):
        question = make_question(["flag"], pools=["Doomed pool"])
        pool = Pool.objects.get(name="Doomed pool")
        challenge = question.challenge
        other = adminops.create_pool(self.admin, name="Survivor")
        PoolChallenge.objects.create(pool=other, challenge=challenge)

        adminops.delete_pool(self.admin, pool.id)
        self.assertFalse(Pool.objects.filter(pk=pool.id).exists())
        self.assertFalse(PoolChallenge.objects.filter(pool_id=pool.id).exists())
        self.assertTrue(Challenge.objects.filter(pk=challenge.id).exists())  # challenge survives
        self.assertTrue(PoolChallenge.objects.filter(pool=other, challenge=challenge).exists())
        self.assertTrue(AuditEvent.objects.filter(operation="pool-delete").exists())

        with self.assertRaises(ServiceError):
            adminops.delete_pool(self.admin, pool.id)  # already gone → clean 404


class PoolPageTests(TestCase):
    def setUp(self):
        self.admin = make_user("610000000000000001", "Admin", is_admin=True)
        self.client.force_login(self.admin)

    def test_create_via_page_and_list(self):
        response = self.client.post(reverse("manage-pools"), {
            "name": "Web", "description": "pwn and web", "order": "2",
        })
        self.assertEqual(response.status_code, 302)
        pool = Pool.objects.get(name="Web")
        self.assertEqual(pool.order, 2)
        listing = self.client.get(reverse("manage-pools"))
        self.assertContains(listing, "Web")
        self.assertContains(listing, "0 challenges")

    def test_duplicate_name_shows_error_on_page(self):
        adminops.create_pool(self.admin, name="Web")
        response = self.client.post(reverse("manage-pools"), {"name": "web", "order": "0"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")

    def test_edit_via_page(self):
        pool = adminops.create_pool(self.admin, name="Before")
        response = self.client.post(reverse("manage-pool-edit", args=[pool.id]), {
            "name": "After", "description": "", "order": "5",
        })
        self.assertEqual(response.status_code, 302)
        pool.refresh_from_db()
        self.assertEqual(pool.name, "After")
        self.assertEqual(pool.order, 5)

    def test_delete_via_confirm_flow(self):
        pool = adminops.create_pool(self.admin, name="Shortlived")
        # First POST without confirm renders the server-rendered confirm page.
        response = self.client.post(reverse("manage-action"), {
            "action": "delete-pool", "target": pool.id,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Delete this pool?")
        # Confirming performs the deletion.
        response = self.client.post(reverse("manage-action"), {
            "action": "delete-pool", "target": pool.id, "confirm": "yes",
        })
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Pool.objects.filter(pk=pool.id).exists())

    def test_non_admin_gets_no_pool_pages(self):
        pleb = make_user("610000000000000002", "Pleb")
        self.client.force_login(pleb)
        self.assertEqual(self.client.get(reverse("manage-pools")).status_code, 302)
        pool = adminops.create_pool(self.admin, name="Hidden")
        self.assertEqual(self.client.get(reverse("manage-pool-edit", args=[pool.id])).status_code, 302)


class PlayerFacingEffectsTests(TestCase):
    def setUp(self):
        self.admin = make_user("620000000000000001", "Admin", is_admin=True)
        self.player = make_user("620000000000000002", "Player")
        self.question = make_question(["flag"], pools=["Stable"])
        self.challenge = self.question.challenge
        self.pool = Pool.objects.get(name="Stable")

    def test_pool_page_lists_member_challenges(self):
        self.client.force_login(self.player)
        response = self.client.get(reverse("pool", args=[self.pool.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.challenge.title)

    def test_renamed_pool_keeps_its_url_and_content(self):
        adminops.edit_pool(self.admin, self.pool.id, name="Renamed in place")
        self.client.force_login(self.player)
        response = self.client.get(reverse("pool", args=[self.pool.slug]))  # same slug
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.challenge.title)

    def test_deleted_pool_page_is_gone_but_challenge_remains_browsable(self):
        adminops.delete_pool(self.admin, self.pool.id)
        self.client.force_login(self.player)
        self.assertEqual(self.client.get(reverse("pool", args=[self.pool.slug])).status_code, 404)
        self.assertEqual(self.client.get("/").status_code, 200)  # all-pools view still works
        self.assertContains(self.client.get("/"), self.challenge.title)

    def test_challenges_without_pools_still_browsable(self):
        """A challenge may end up pool-less after deletions; it must stay
        reachable in the unfiltered challenge browser."""
        adminops.delete_pool(self.admin, self.pool.id)
        self.client.force_login(self.player)
        self.assertContains(self.client.get("/"), self.challenge.title)
