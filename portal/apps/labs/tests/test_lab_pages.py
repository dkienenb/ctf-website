"""Lab page flows: the challenge page's request/claim/refresh/release/reset
buttons, the desktop route, and the register_host command (the operator
entry point for provisioning)."""
import json

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse

from portal.apps.accounts.models import User
from portal.apps.labs.models import Host, LabInstance, LabTemplate, QueueEntry
from portal.apps.testutils import complete, make_host, make_lab_challenge, make_user


@override_settings(SUBMISSION_COOLDOWN_SECONDS=0, GUACAMOLE_JSON_SECRET="ab" * 16)
class LabPageFlowTests(TestCase):
    def setUp(self):
        self.host = make_host()
        self.user = make_user("920000000000000001", "Alex")
        self.admin = make_user("920000000000000002", "Admin", is_admin=True)
        self.challenge = make_lab_challenge()
        self.client.force_login(self.user)

    def _action(self, action, slug=None, **extra):
        return self.client.post(
            reverse("lab-action", args=[slug or self.challenge.slug, action]), **extra
        )

    def test_request_claim_and_desktop_route_through_the_page(self):
        response = self._action("request")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._lab().state, "reserved")
        self._action("claim")
        self.assertEqual(self._lab().state, "starting")
        complete(self.host, "allocate")
        self.assertEqual(self._lab().state, "running")
        # The desktop opens for a running lab (grant issued, console bridge
        # page rendered: the browser POSTs the grant, stores the fresh
        # authToken, then enters the viewer).
        response = self.client.get(reverse("lab-desktop", args=[self.challenge.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "labs/console_bridge.html")
        self.assertContains(response, "guacamole/api/tokens")
        self.assertContains(response, "guacamole/#/client/")

    def _lab(self):
        return LabInstance.objects.get(user=self.user, challenge=self.challenge)

    def test_get_to_lab_action_is_405(self):
        self.assertEqual(
            self.client.get(reverse("lab-action", args=[self.challenge.slug, "request"])).status_code,
            405,
        )

    def test_unknown_action_is_refused_cleanly(self):
        response = self._action("detonate")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(LabInstance.objects.exists())

    def test_refresh_and_release_refuse_wrong_states_via_page(self):
        self._action("request")
        self._action("claim")
        complete(self.host, "allocate")
        # Release while running works; releasing again (already saving) refuses.
        self._action("release")
        self.assertEqual(self._lab().state, "saving")
        response = self._action("release")
        self.assertEqual(response.status_code, 302)  # back to the challenge with an error toast

    def test_actions_require_approved_accounts(self):
        locked = make_user("920000000000000003", "Locked", status=User.Status.PENDING)
        self.client.force_login(locked)
        response = self._action("request")
        self.assertIn(response.status_code, (302, 403))
        self.assertFalse(LabInstance.objects.filter(user=locked).exists())

    def test_request_reset_notifies_admins_via_page(self):
        self._action("request")
        response = self._action("request-reset")
        self.assertEqual(response.status_code, 302)
        entry = QueueEntry.objects.filter(user=self.user, challenge=self.challenge).order_by("-id").first()
        self.assertIsNotNone(entry)  # the request is recorded for the operator flow

    def test_desktop_refused_before_running(self):
        self._action("request")
        self._action("claim")
        response = self.client.get(reverse("lab-desktop", args=[self.challenge.slug]))
        self.assertEqual(response.status_code, 302)  # redirected with an error, no grant
        self.assertNotIn("data=", getattr(response, "url", ""))


class RegisterHostCommandTests(TestCase):
    def test_registers_and_prints_token_once(self):
        out = self._run("workhorse-new", capacity=2)
        host = Host.objects.get(name="workhorse-new")
        self.assertEqual(host.capacity_slots, 2)
        self.assertTrue(host.online)
        printed = out.strip().splitlines()
        token_line = [line for line in printed if line.strip()]
        self.assertTrue(any("token" in line.lower() for line in token_line), out)
        self.assertNotEqual(host.token_hash, "")  # only the hash is stored

    def _run(self, name, capacity=1):
        import io
        buffer = io.StringIO()
        call_command("register_host", name, capacity=capacity, stdout=buffer)
        return buffer.getvalue()

    def test_rerun_rotates_the_token(self):
        self._run("workhorse-rot")
        host = Host.objects.get(name="workhorse-rot")
        first_hash = host.token_hash
        self._run("workhorse-rot", capacity=3)
        host.refresh_from_db()
        self.assertEqual(host.capacity_slots, 3)
        self.assertNotEqual(host.token_hash, first_hash)  # re-registration rotates credentials


@override_settings(GUACAMOLE_JSON_SECRET="ab" * 16)
class LabTemplatesPageTests(TestCase):
    """The fleet view of every challenge's pinned machine template."""

    def setUp(self):
        self.admin = make_user("930000000000000009", "Admin", is_admin=True)
        self.user = make_user("930000000000000010", "Player")
        self.challenge = make_lab_challenge()  # pins kali-v1 / target-v1

    def test_admin_sees_pinned_versions_and_sizing(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("manage-lab-templates"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("kali-v1", content)
        self.assertIn("target-v1", content)
        self.assertIn("4096 MB", content)   # attacker sizing recorded at pin time
        self.assertIn("2048 MB", content)   # target sizing

    def test_versions_are_chipped_by_library_state(self):
        from portal.apps.labs.models import MachineImage
        MachineImage.objects.create(role="attacker", version="kali-v1",
                                    domain_xml="<x/>")  # record without a disk
        self.client.force_login(self.admin)
        response = self.client.get(reverse("manage-lab-templates"))
        content = response.content.decode()
        # attacker kali-v1 exists but has no disk → incomplete chip
        self.assertIn("no backing disk", content)
        # target-v1 has no record at all → missing chip
        self.assertIn("not in the image library", content)

    def test_empty_versions_show_the_demo_label(self):
        from portal.apps.labs.models import LabTemplate
        LabTemplate.objects.update(attacker_version="", target_version="")
        self.client.force_login(self.admin)
        response = self.client.get(reverse("manage-lab-templates"))
        self.assertContains(response, "Demo template")

    def test_non_admins_are_bounced(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("manage-lab-templates")).status_code, 302)

    def test_anonymous_are_bounced(self):
        self.assertEqual(self.client.get(reverse("manage-lab-templates")).status_code, 302)

    def test_orphaned_template_renders_without_a_challenge_link(self):
        """SET_NULL leaves LabTemplate rows whose challenge was deleted;
        the fleet view renders them as orphans instead of crashing."""
        from portal.apps.labs.models import LabTemplate
        LabTemplate.objects.update(challenge=None)
        self.client.force_login(self.admin)
        response = self.client.get(reverse("manage-lab-templates"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Orphaned")


@override_settings(GUACAMOLE_JSON_SECRET="ab" * 16)
class LabPresetFlowTests(TestCase):
    """Challenges pick a preconfigured lab from a dropdown (or none);
    pinning COPIES the preset's values."""

    def setUp(self):
        self.admin = make_user("940000000000000009", "Admin", is_admin=True)
        self.user = make_user("940000000000000010", "Player")
        self.challenge = make_lab_challenge()  # pins kali-v1 / target-v1
        self.client.force_login(self.admin)
        # The seeded Kali pair references these versions; create the records
        # so pin validation passes (the live system has them).
        from portal.apps.labs.models import MachineImage
        MachineImage.objects.get_or_create(
            role="attacker", version="kali-base-v1",
            defaults={"domain_xml": "<domain/>", "disk_sha256": "a" * 64})
        MachineImage.objects.get_or_create(
            role="target", version="kali-target-v1",
            defaults={"domain_xml": "<domain/>", "disk_sha256": "a" * 64})

    def _presets(self):
        from portal.apps.labs.models import LabPreset
        return {p.name: p for p in LabPreset.objects.all()}

    def test_seed_migration_created_both_defaults(self):
        presets = self._presets()
        self.assertIn("Kali pair", presets)
        self.assertIn("Demo pair", presets)
        self.assertEqual(presets["Kali pair"].attacker_version, "kali-base-v1")
        self.assertEqual(presets["Kali pair"].target_version, "kali-target-v1")
        self.assertEqual(presets["Demo pair"].attacker_version, "alpine-demo-v1")
        self.assertEqual(presets["Demo pair"].target_version, "demo-target-v1")

    def test_dropdown_pins_the_preset_values(self):
        preset = self._presets()["Kali pair"]
        response = self.client.post(
            reverse("manage-lab-template-preset", args=[self.challenge.id]),
            {"preset": preset.id})
        self.assertEqual(response.status_code, 302)
        # Fetch fresh: the fixture pre-populated the challenge's cached
        # reverse OneToOne with the pre-pin template object.
        template = LabTemplate.objects.get(challenge=self.challenge)
        self.assertEqual(template.attacker_version, "kali-base-v1")
        self.assertEqual(template.target_version, "kali-target-v1")
        self.assertEqual(template.attacker_ram_mb, 4096)
        self.assertEqual(template.target_ram_mb, 2048)

    def test_none_clears_the_lab(self):
        from portal.apps.labs.models import LabTemplate
        preset = self._presets()["Kali pair"]
        self.client.post(reverse("manage-lab-template-preset", args=[self.challenge.id]),
                         {"preset": preset.id})
        response = self.client.post(
            reverse("manage-lab-template-preset", args=[self.challenge.id]),
            {"preset": "none"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(LabTemplate.objects.filter(challenge=self.challenge).exists())

    def test_content_edit_preselects_the_matching_preset(self):
        preset = self._presets()["Kali pair"]
        self.client.post(reverse("manage-lab-template-preset", args=[self.challenge.id]),
                         {"preset": preset.id})
        response = self.client.get(reverse("manage-content-edit", args=[self.challenge.id]))
        content = response.content.decode()
        self.assertIn(f'value="{preset.id}"', content)
        self.assertIn("matches “Kali pair”", content)
        # The "choose" placeholder is not selected once a lab is pinned.
        self.assertNotIn('<option value="" disabled selected', content)

    def test_custom_pins_report_no_matching_preset(self):
        # The fixture pin (kali-v1/target-v1) matches no seeded preset.
        response = self.client.get(reverse("manage-content-edit", args=[self.challenge.id]))
        self.assertContains(response, "no preset matches")

    def test_preset_create_edit_delete_on_the_lab_templates_page(self):
        from portal.apps.labs.models import LabPreset
        payload = {"name": "Tiny pair", "attacker_version": "kali-base-v1",
                   "target_version": "", "attacker_ram_mb": 1024, "attacker_vcpu": 1,
                   "attacker_disk_gb": 20, "target_ram_mb": 512, "target_vcpu": 1,
                   "target_disk_gb": 10, "notes": "lightweight"}
        response = self.client.post(reverse("manage-lab-preset-save"), payload)
        self.assertEqual(response.status_code, 302)
        preset = LabPreset.objects.get(name="Tiny pair")
        self.assertEqual(preset.attacker_ram_mb, 1024)
        # Duplicate names refused.
        dupe = dict(payload)
        response = self.client.post(reverse("manage-lab-preset-save"), dupe)
        self.assertFalse(LabPreset.objects.filter(name="Tiny pair").count() > 1)
        # Edit.
        response = self.client.get(reverse("manage-lab-preset-edit", args=[preset.id]))
        self.assertContains(response, 'value="Tiny pair"')
        payload["preset_id"] = preset.id
        payload["attacker_ram_mb"] = 2048
        self.client.post(reverse("manage-lab-preset-save"), payload)
        preset.refresh_from_db()
        self.assertEqual(preset.attacker_ram_mb, 2048)
        # Existing challenges are untouched by preset edits.
        self.client.post(reverse("manage-lab-template-preset", args=[self.challenge.id]),
                         {"preset": preset.id})
        before = LabTemplate.objects.get(challenge=self.challenge).attacker_ram_mb
        payload["attacker_ram_mb"] = 8192
        self.client.post(reverse("manage-lab-preset-save"), payload)
        self.assertEqual(LabTemplate.objects.get(challenge=self.challenge).attacker_ram_mb, before)
        # Delete.
        self.client.post(reverse("manage-lab-preset-delete", args=[preset.id]))
        self.assertFalse(LabPreset.objects.filter(name="Tiny pair").exists())

    def test_preset_with_unknown_version_refused(self):
        payload = {"name": "Ghost", "attacker_version": "no-such-v1",
                   "target_version": "", "attacker_ram_mb": 1024, "attacker_vcpu": 1,
                   "attacker_disk_gb": 20, "target_ram_mb": 512, "target_vcpu": 1,
                   "target_disk_gb": 10}
        self.client.post(reverse("manage-lab-preset-save"), payload)
        self.assertFalse(self._presets().get("Ghost"))

    def test_lab_templates_page_lists_presets_with_usage(self):
        preset = self._presets()["Kali pair"]
        self.client.post(reverse("manage-lab-template-preset", args=[self.challenge.id]),
                         {"preset": preset.id})
        response = self.client.get(reverse("manage-lab-templates"))
        content = response.content.decode()
        self.assertIn("Kali pair", content)
        self.assertIn("1 challenge", content)
        self.assertIn("kali-target-v1", content)

    def test_preset_flow_requires_admin(self):
        self.client.force_login(self.user)
        preset = self._presets()["Kali pair"]
        self.assertEqual(self.client.post(
            reverse("manage-lab-template-preset", args=[self.challenge.id]),
            {"preset": preset.id}).status_code, 302)  # bounced
        template = self.challenge.lab_template
        self.assertNotEqual(template.attacker_version, "kali-base-v1")
