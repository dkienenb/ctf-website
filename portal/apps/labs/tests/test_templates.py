"""Machine template authoring tests: artifact validation, chunked staging,
distribution idempotency, lab-template binding, and the operator pages."""
import base64
import hashlib
import json
import uuid

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from portal.apps.accounts.models import User
from portal.apps.content.models import Challenge
from portal.apps.testutils import make_host, make_question, make_user
from portal.apps.labs.models import Host, HostCommand, LabInstance, LabTemplate, MachineBuild, MachineImage, QueueEntry
from portal.apps.labs import services
from portal.apps.labs.guacamole import issue_build_grant, GrantError as BuildGrantError
from portal.apps.submissions.models import DataRevision
from portal.apps.submissions.services import ServiceError

DOMAIN_XML = (
    "<domain><name>@NAME@</name><disk>@DISK@</disk>"
    "<graphics port='@VNC_PORT@'/><source network='@LAB_NET@'/></domain>"
)


def qcow2_file(data=b"", name="disk-template"):
    return SimpleUploadedFile(name, b"QFI\xfb\x00\x03" + data)


def make_admin(discord_id):
    return User.objects.create_user(discord_id, "Admin", is_admin=True, status=User.Status.APPROVED)


class MachineImageServiceTests(TestCase):
    def setUp(self):
        self.admin = make_admin("900000000000000001")
        self.user = make_user("900000000000000002", "Player")
        self.host = Host.objects.create(name="stage-host")

    def _image(self, role="target", version="v1", **kwargs):
        image, _ = services.save_machine_image(
            self.admin, role=role, version=version, domain_xml=DOMAIN_XML, **kwargs
        )
        return image

    def test_validation_rejects_bad_placeholders_versions_and_roles(self):
        with self.assertRaises(ServiceError) as error:
            services.save_machine_image(
                self.admin, role="target", version="v2",
                domain_xml="<domain>@NAME@</domain>",
            )
        self.assertEqual(error.exception.status, 400)  # missing @DISK@/@VNC_PORT@/@LAB_NET@
        with self.assertRaises(ServiceError):
            services.save_machine_image(
                self.admin, role="target", version="../evil", domain_xml=DOMAIN_XML,
            )
        with self.assertRaises(ServiceError):
            services.save_machine_image(
                self.admin, role="disk", version="v2", domain_xml=DOMAIN_XML,
            )

    def test_disk_upload_records_hash_and_size(self):
        disk = qcow2_file(b"base-disk-bytes", "disk-template")
        image = self._image(disk=disk)
        image.refresh_from_db()
        self.assertEqual(image.disk_size, len(b"QFI\xfb\x00\x03" + b"base-disk-bytes"))
        self.assertEqual(image.disk_sha256, hashlib.sha256(b"QFI\xfb\x00\x03" + b"base-disk-bytes").hexdigest())

    def test_distribute_issues_fetch_instructions_idempotently(self):
        """Distribution issues per-version fetch instructions (no data in
        the queue); identity includes role/version so identical contents in
        two versions never collide; failed instructions reset on re-issue."""
        disk = qcow2_file(b"z" * (services.STAGE_CHUNK_BYTES + 10), "disk-template")
        image = self._image(disk=disk)
        result = services.distribute_machine_image(self.admin, image.id)
        self.assertEqual(result["commands"], 3)  # domain fetch + disk fetch + verify
        fetches = HostCommand.objects.filter(operation="template-fetch")
        self.assertEqual(fetches.count(), 2)
        self.assertEqual(fetches.filter(payload__file="disk-template").count(), 1)
        verify = HostCommand.objects.get(operation="template-verify")
        self.assertEqual(verify.payload["sha256"], image.disk_sha256)
        self.assertEqual(verify.payload["xml_sha256"],
                         hashlib.sha256(image.domain_xml.encode()).hexdigest())
        # Re-distribution issues nothing new.
        again = services.distribute_machine_image(self.admin, image.id)
        self.assertEqual(again["commands"], 3)
        self.assertEqual(HostCommand.objects.count(), 3)
        # A failed instruction returns to pending on re-issue.
        fetches.update(status=HostCommand.Status.FAILED, result={"error": "x"})
        services.distribute_machine_image(self.admin, image.id)
        self.assertFalse(HostCommand.objects.filter(status=HostCommand.Status.FAILED).exists())

    def test_distribute_identity_never_collides_across_versions(self):
        """Two versions with IDENTICAL xml+disk: each gets its own fetch
        instructions (role+version in the identity), so both stage."""
        make_host("collision-host")
        xml = "<domain><name>@NAME@</name><disk>@DISK@</disk><net>@LAB_NET@</net></domain>"
        one, _ = services.save_machine_image(
            self.admin, role="target", version="v1", domain_xml=xml,
            disk=qcow2_file(b"same-bytes", "d1"))
        two, _ = services.save_machine_image(
            self.admin, role="target", version="v2", domain_xml=xml,
            disk=qcow2_file(b"same-bytes", "d2"))
        services.distribute_machine_image(self.admin, one.id)
        services.distribute_machine_image(self.admin, two.id)
        # Per-host: 2 files × (number of hosts). Identity is disjoint across
        # versions despite identical content.
        hosts = Host.objects.count()
        ids_v1 = set(HostCommand.objects.filter(
            operation="template-fetch", payload__version="v1").values_list("op_id", flat=True))
        ids_v2 = set(HostCommand.objects.filter(
            operation="template-fetch", payload__version="v2").values_list("op_id", flat=True))
        self.assertEqual(len(ids_v1), 2 * hosts)
        self.assertEqual(len(ids_v2), 2 * hosts)
        self.assertEqual(ids_v1 & ids_v2, set())

    def test_delete_refused_while_pinned(self):
        image = self._image(role="attacker", version="kali-9")
        challenge = Challenge.objects.create(title="T", slug="t-1")
        LabTemplate.objects.create(challenge=challenge, attacker_version="kali-9", target_version="")
        with self.assertRaises(ServiceError) as error:
            services.delete_machine_image(self.admin, image.id)
        self.assertEqual(error.exception.status, 409)
        # Unpin, then deletion succeeds.
        LabTemplate.objects.update(attacker_version="")
        services.delete_machine_image(self.admin, image.id)
        self.assertFalse(MachineImage.objects.filter(pk=image.id).exists())

    def test_lab_template_binding_requires_uploaded_versions(self):
        challenge = Challenge.objects.create(title="T", slug="t-2")
        with self.assertRaises(ServiceError):
            services.lab_template_save(
                self.admin, challenge.id,
                attacker_version="ghost", target_version="",
                attacker_ram_mb=2048, attacker_vcpu=2, attacker_disk_gb=20,
                target_ram_mb=1024, target_vcpu=1, target_disk_gb=10,
            )
        self._image(role="attacker", version="kali-3")
        self._image(role="target", version="victim-3")
        template = services.lab_template_save(
            self.admin, challenge.id,
            attacker_version="kali-3", target_version="victim-3",
            attacker_ram_mb=4096, attacker_vcpu=2, attacker_disk_gb=20,
            target_ram_mb=2048, target_vcpu=1, target_disk_gb=10,
        )
        self.assertEqual(template.challenge, challenge)
        self.assertEqual(template.target_version, "victim-3")

    def test_admin_required(self):
        pleb = make_user("900000000000000009", "Pleb")
        with self.assertRaises(ServiceError):
            services.save_machine_image(pleb, role="target", version="v", domain_xml=DOMAIN_XML)
        with self.assertRaises(ServiceError):
            services.distribute_machine_image(pleb, 1)


    def test_lab_template_clear_makes_the_challenge_machine_less(self):
        """'No machine': the lab panel disappears for players, saved labs and
        their queues are discarded, and the pin can be re-created later."""
        challenge = Challenge.objects.create(title="Machine-less", slug="machine-less",
                                             lifecycle=Challenge.Lifecycle.PUBLISHED)
        template = services.lab_template_save(
            self.admin, challenge.id,
            attacker_version="", target_version="",
            attacker_ram_mb=4096, attacker_vcpu=2, attacker_disk_gb=20,
            target_ram_mb=2048, target_vcpu=1, target_disk_gb=10,
        )
        lab = LabInstance.objects.create(user=self.user, challenge=challenge,
                                         template=template, state="saved")
        QueueEntry.objects.create(user=self.user, challenge=challenge, active=True)
        result = services.lab_template_clear(self.admin, challenge.id)
        self.assertEqual(result["instances_discarded"], 1)
        self.assertFalse(LabTemplate.objects.filter(pk=template.id).exists())
        self.assertFalse(LabInstance.objects.filter(pk=lab.id).exists())
        self.assertFalse(QueueEntry.objects.filter(user=self.user, challenge=challenge, active=True).exists())
        # The player page no longer offers a lab at all.
        self.client.force_login(self.user)
        response = self.client.get(reverse("challenge_detail", args=[challenge.slug]))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Your lab")
        self.assertNotContains(response, "Request lab")
        # Re-pinning after a clear works (a fresh template is created).
        re_pinned = services.lab_template_save(
            self.admin, challenge.id,
            attacker_version="", target_version="",
            attacker_ram_mb=4096, attacker_vcpu=2, attacker_disk_gb=20,
            target_ram_mb=2048, target_vcpu=1, target_disk_gb=10,
        )
        self.assertEqual(re_pinned.challenge, challenge)

    def test_lab_template_clear_refuses_holding_labs(self):
        challenge = Challenge.objects.create(title="Busy", slug="busy-1",
                                             lifecycle=Challenge.Lifecycle.PUBLISHED)
        LabInstance.objects.create(user=self.user, challenge=challenge, state="running")
        with self.assertRaises(ServiceError) as error:
            services.lab_template_clear(self.admin, challenge.id)
        self.assertEqual(error.exception.status, 409)
        self.assertTrue(LabInstance.objects.filter(challenge=challenge).exists())

    def test_lab_template_clear_requires_admin(self):
        challenge = Challenge.objects.create(title="C", slug="c-9")
        pleb = make_user("900000000000000009", "Pleb")
        with self.assertRaises(ServiceError) as error:
            services.lab_template_clear(pleb, challenge.id)
        self.assertEqual(error.exception.status, 403)

    def test_clear_machine_through_the_confirm_flow(self):
        challenge = Challenge.objects.create(title="Via page", slug="via-page",
                                             lifecycle=Challenge.Lifecycle.PUBLISHED)
        services.lab_template_save(
            self.admin, challenge.id,
            attacker_version="", target_version="",
            attacker_ram_mb=4096, attacker_vcpu=2, attacker_disk_gb=20,
            target_ram_mb=2048, target_vcpu=1, target_disk_gb=10,
        )
        self.client.force_login(self.admin)
        response = self.client.post(reverse("manage-action"),
                                    {"action": "clear-machine", "target": challenge.id})
        self.assertEqual(response.status_code, 200)  # server-rendered confirm
        self.assertContains(response, "Remove the machine from this challenge?")
        response = self.client.post(reverse("manage-action"),
                                    {"action": "clear-machine", "target": challenge.id, "confirm": "yes"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(LabTemplate.objects.filter(challenge=challenge).exists())

    def test_clear_machine_refused_through_the_page_while_running(self):
        challenge = Challenge.objects.create(title="Via page 2", slug="via-page-2",
                                             lifecycle=Challenge.Lifecycle.PUBLISHED)
        LabInstance.objects.create(user=self.user, challenge=challenge, state="reserved")
        self.client.force_login(self.admin)
        response = self.client.post(reverse("manage-action"),
                                    {"action": "clear-machine", "target": challenge.id, "confirm": "yes"},
                                    follow=True)
        self.assertContains(response, "still holds lab slots")
        self.assertTrue(LabInstance.objects.filter(challenge=challenge).exists())


    def test_build_lifecycle_through_services_and_results(self):
        from portal.apps.labs.models import MachineBuild
        from portal.apps.labs.models import MachineBuild

        host = make_host("build-host")
        build = services.start_build(
            self.admin, host_id=host.id, role="attacker",
            version="kali-v7", base_version="",         )
        self.assertEqual(build.state, MachineBuild.State.STARTING)
        start_cmd = HostCommand.objects.get(op_id=f"build-{build.id}-start")
        self.assertEqual(start_cmd.operation, "build-start")
        # Agent boots it: result folds into the record.
        services.complete_command(host, start_cmd.op_id, "ok",
                                  {"build": "ctf-build-attacker", "vnc_port": "6911"})
        build.refresh_from_db()
        self.assertEqual(build.state, MachineBuild.State.READY)
        self.assertEqual(build.vnc_port, "6911")
        # One build slot per host.
        with self.assertRaises(ServiceError) as error:
            services.start_build(self.admin, host_id=host.id, role="target",
                                 version="target-v7", base_version="")
        self.assertIn("already has an active build", error.exception.message)
        # Capture creates a HOST-LOCAL machine image from the agent's sha.
        sha = hashlib.sha256(b"captured-disk").hexdigest()
        cap_cmd = services.capture_build(self.admin, build.id)
        services.complete_command(host, cap_cmd.op_id, "ok",
                                  {"captured": "attacker/kali-v7", "sha256": sha, "size": 42})
        build.refresh_from_db()
        self.assertEqual(build.state, MachineBuild.State.CAPTURED)
        image = MachineImage.objects.get(role="attacker", version="kali-v7")
        self.assertEqual(image.disk_sha256, sha)
        self.assertFalse(image.disk_file)  # host-local: no controller-side file
        self.assertIn("host-local", image.notes)
        # Distributing a host-local image is refused until the file is uploaded.
        with self.assertRaises(ServiceError):
            services.distribute_machine_image(self.admin, image.id)

    def test_build_requires_admin_and_ready_state(self):
        from portal.apps.labs.models import MachineBuild

        host = make_host("build-host-2")
        with self.assertRaises(ServiceError):
            services.start_build(self.user, host_id=host.id, role="attacker",
                                 version="kali-x", base_version="")
        build = services.start_build(self.admin, host_id=host.id, role="attacker",
                                     version="kali-x", base_version="")
        disc = services.discard_build(self.admin, build.id)
        services.complete_command(host, disc.op_id, "ok", {"discarded": "ctf-build-attacker"})
        with self.assertRaises(ServiceError):
            services.discard_build(self.admin, build.id)  # already discarded

    def test_duplicate_version_name_refused_at_start(self):
        from portal.apps.labs.models import MachineBuild

        host = make_host("build-host-3")
        MachineImage.objects.create(role="attacker", version="exists", domain_xml="<x/>")
        with self.assertRaises(ServiceError) as error:
            services.start_build(self.admin, host_id=host.id, role="attacker",
                                 version="exists", base_version="")
        self.assertEqual(error.exception.status, 409)


@override_settings(GUACAMOLE_JSON_SECRET="ab" * 16)
class MachineImagePageTests(TestCase):
    def setUp(self):
        self.admin = make_admin("910000000000000001")
        self.client.force_login(self.admin)

    def test_build_pages_flow(self):
        from portal.apps.labs.models import MachineBuild

        host = make_host("page-build-host")
        self.client.force_login(self.admin)
        response = self.client.post(reverse("manage-build-start"), {
            "host": host.id, "role": "attacker", "base_version": "",
            "version": "kali-page", "ssh_user": "root",
        })
        self.assertEqual(response.status_code, 302)
        build = MachineBuild.objects.get(version="kali-page")
        # Boot the build by completing the start command, then visit the page.
        services.complete_command(host, f"build-{build.id}-start", "ok", {"vnc_port": "6911"})
        response = self.client.get(reverse("manage-build", args=[build.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Console")
        self.assertContains(response, "Capture as template")
        # The start page renders on GET too.
        self.assertEqual(self.client.get(reverse("manage-build-start")).status_code, 200)
        # Non-admins are bounced.
        pleb = make_user("910000000000000009", "Pleb")
        self.client.force_login(pleb)
        self.assertEqual(self.client.get(reverse("manage-build-start")).status_code, 302)

    def test_build_console_renders_the_grant_bridge(self):
        """The console is handed off through the bridge page, not a bare
        redirect: the SPA re-sends any stored auth token alongside a
        redirect's grant, Guacamole's cached-token provider wins, and the
        grant's connection is silently dropped ("connection does not
        exist"). The bridge POSTs the grant alone."""
        from portal.apps.labs.models import MachineBuild
        import re as re_mod

        host = make_host("bridge-host")
        response = self.client.post(reverse("manage-build-start"), {
            "host": host.id, "role": "attacker", "base_version": "",
            "version": "kali-bridge", "ssh_user": "root",
        })
        build = MachineBuild.objects.get(version="kali-bridge")
        services.complete_command(host, f"build-{build.id}-start", "ok", {"vnc_port": "6911"})

        response = self.client.get(reverse("manage-build-console", args=[build.id]))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "labs/console_bridge.html")
        self.assertContains(response, "guacamole/api/tokens")
        self.assertContains(response, "guacamole/#/client/")
        # The embedded grant is present and decryptable with the test secret.
        match = re_mod.search(
            r'id="grant-data"[^>]*>([^<]+)<', response.content.decode())
        self.assertTrue(match)
        import json as json_lib
        import base64
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding
        data = json_lib.loads(match.group(1))
        key = bytes.fromhex("ab" * 16)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(b"\x00" * 16)).decryptor()
        padded = base64.b64decode(data)
        unpadder = padding.PKCS7(128).unpadder()
        raw = unpadder.update(decryptor.update(padded) + decryptor.finalize()) + unpadder.finalize()
        payload = json_lib.loads(raw[32:])  # skip HMAC signature
        connection = list(payload["connections"].values())[0]
        self.assertEqual(connection["parameters"]["port"], "6911")
        self.assertEqual(connection["parameters"]["hostname"], "127.0.0.1")

    def test_build_console_refused_when_guest_is_not_booted(self):
        from portal.apps.labs.models import MachineBuild

        host = make_host("bridge-noboost-host")
        self.client.post(reverse("manage-build-start"), {
            "host": host.id, "role": "attacker", "base_version": "",
            "version": "kali-noboost", "ssh_user": "root",
        })
        build = MachineBuild.objects.get(version="kali-noboost")
        response = self.client.get(reverse("manage-build-console", args=[build.id]))
        self.assertEqual(response.status_code, 302)  # back to the build page with an error message


    def test_create_via_page_and_list_matrix(self):
        disk = qcow2_file(b"disk-content", "disk-template")
        response = self.client.post(reverse("manage-machine-image-new"), {
            "role": "target", "version": "victim-1", "notes": "shell injection target",
            "domain_xml": DOMAIN_XML, "disk": disk,
        })
        self.assertEqual(response.status_code, 302)
        image = MachineImage.objects.get(version="victim-1")
        self.assertEqual(image.disk_sha256, image.disk_sha256)  # recorded from upload
        self.assertTrue(image.disk_file)

        Host.objects.create(name="wh-01", inventory={"target": {"victim-1": {
            "domain.xml": 10, "disk-template": 12,
        }}})
        response = self.client.get(reverse("manage-machine-images"))
        self.assertContains(response, "target/victim-1")
        self.assertContains(response, "wh-01")

    def test_distribute_action_queues_commands(self):
        Host.objects.create(name="wh-x")
        image, _ = services.save_machine_image(
            self.admin, role="attacker", version="kali-1", domain_xml=DOMAIN_XML,
            disk=qcow2_file(b"abc", "d"),
        )
        response = self.client.post(reverse("manage-action"), {
            "action": "distribute-template", "target": image.id, "confirm": "yes",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(HostCommand.objects.filter(operation="template-fetch").exists())

    def test_heartbeat_stores_inventory(self):
        host = Host.objects.create(name="inv-host")
        token = uuid.uuid4().hex
        host.token_hash = hashlib.sha256(token.encode()).hexdigest()
        host.save()
        response = self.client.post(reverse("agent-heartbeat"), data='{"inventory": {"target": {"v1": {"domain.xml": 5}}}}',
                                    content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(response.status_code, 200)
        host.refresh_from_db()
        self.assertEqual(host.inventory["target"]["v1"], {"domain.xml": 5})

    def test_lab_template_action_pins_challenge(self):
        services.save_machine_image(
            self.admin, role="attacker", version="kali-5", domain_xml=DOMAIN_XML,
            disk=qcow2_file(b"abc", "d"),
        )
        challenge = Challenge.objects.create(title="T", slug="t-5")
        response = self.client.post(reverse("manage-lab-template", args=[challenge.id]), {
            "attacker_version": "kali-5", "target_version": "",
            "attacker_ram_mb": 4096, "attacker_vcpu": 2, "attacker_disk_gb": 20,
            "target_ram_mb": 2048, "target_vcpu": 1, "target_disk_gb": 10,
        })
        self.assertEqual(response.status_code, 302)
        template = LabTemplate.objects.get(challenge=challenge)
        self.assertEqual(template.attacker_version, "kali-5")


class BuildLifecycleHardeningTests(TestCase):
    """Owner feedback round: locking, idempotent capture/discard, exec
    accuracy, password scrubbing, and version immutability."""

    def setUp(self):
        self.admin = make_admin("530000000000000001")
        self.user = make_user("530000000000000002", "Player")
        self.host = make_host("lock-host")

    def _build(self, state=None):
        from portal.apps.labs.models import MachineBuild
        build = services.start_build(self.admin, host_id=self.host.id,
                                     role="attacker", version="kali-lock", base_version="")
        if state is not None:
            MachineBuild.objects.filter(pk=build.id).update(state=state)
            build.refresh_from_db()
        return build

    def test_start_locks_host_and_reserves_version(self):
        from portal.apps.labs.models import MachineBuild
        build = self._build()
        # Same version reserved by an active build.
        with self.assertRaises(ServiceError):
            services.start_build(self.admin, host_id=self.host.id,
                                 role="target", version="kali-lock", base_version="")
        # Different role still refused while the slot is held.
        with self.assertRaises(ServiceError):
            services.start_build(self.admin, host_id=self.host.id,
                                 role="target", version="other-v", base_version="")

    def test_capture_is_idempotent_and_enters_capturing(self):
        from portal.apps.labs.models import MachineBuild
        build = self._build(MachineBuild.State.READY)
        first = services.capture_build(self.admin, build.id)
        build.refresh_from_db()
        self.assertEqual(build.state, MachineBuild.State.CAPTURING)
        second = services.capture_build(self.admin, build.id)  # double click
        self.assertEqual(first.op_id, second.op_id)
        self.assertEqual(HostCommand.objects.filter(operation="build-capture").count(), 1)
        # While capturing, discard is refused.
        with self.assertRaises(ServiceError):
            services.discard_build(self.admin, build.id)

    def test_discard_enters_discarding(self):
        from portal.apps.labs.models import MachineBuild
        build = self._build(MachineBuild.State.READY)
        services.discard_build(self.admin, build.id)
        build.refresh_from_db()
        self.assertEqual(build.state, MachineBuild.State.DISCARDING)

    def test_versions_are_immutable(self):
        from portal.apps.labs.models import MachineImage
        xml = DOMAIN_XML
        image, created = services.save_machine_image(
            self.admin, role="attacker", version="kali-imm", domain_xml=xml,
            disk=qcow2_file(b"original"))
        self.assertTrue(created)
        # A new disk for the same version is refused.
        with self.assertRaises(ServiceError) as error:
            services.save_machine_image(self.admin, role="attacker", version="kali-imm",
                                        domain_xml=xml, disk=qcow2_file(b"changed"))
        self.assertIn("immutable", error.exception.message)
        # A changed domain.xml is refused too.
        with self.assertRaises(ServiceError):
            services.save_machine_image(self.admin, role="attacker", version="kali-imm",
                                        domain_xml=xml.replace("</domain>", "<vcpu>2</vcpu></domain>"))
        # Notes stay editable.
        _, created_again = services.save_machine_image(
            self.admin, role="attacker", version="kali-imm", domain_xml=xml, notes="new notes")
        self.assertFalse(created_again)
        image.refresh_from_db()
        self.assertEqual(image.notes, "new notes")
        self.assertEqual(image.disk_sha256,
                         hashlib.sha256(b"QFI\xfb\x00\x03" + b"original").hexdigest())

    def test_qcow2_validation_refuses_iso_uploads(self):
        with self.assertRaises(ServiceError) as error:
            services.save_machine_image(
                self.admin, role="attacker", version="kali-iso", domain_xml=DOMAIN_XML,
                disk=SimpleUploadedFile("d", b"CD001" + b"\x00" * 16))
        self.assertIn("qcow2", error.exception.message)


class BuildEdgeCaseTests(TestCase):
    """Second feedback round edge cases: incomplete-capture completion,
    duplicate version-name races, artifact chunk ordering."""

    def setUp(self):
        self.admin = make_admin("540000000000000001")
        self.user = make_user("540000000000000002", "Player")

    def test_incomplete_host_local_capture_can_be_completed(self):
        """A host-local capture whose agent transfer failed is not wedged:
        uploading the disk + domain_xml completes the record; the completed
        version is then immutable again. (Created exactly as
        _apply_build_result creates it: empty domain_xml, no disk.)"""
        from portal.apps.labs.models import MachineImage
        challenge = Challenge.objects.create(title="Inc", slug="inc")
        services.lab_template_save(self.admin, challenge.id, attacker_version="",
                                   target_version="", attacker_ram_mb=2048, attacker_vcpu=1,
                                   attacker_disk_gb=20, target_ram_mb=1024, target_vcpu=1,
                                   target_disk_gb=10)
        image = MachineImage.objects.create(role="attacker", version="kali-inc",
                                            domain_xml="")  # host-local, transfer failed
        with self.assertRaises(ServiceError) as error:
            services.save_machine_image(self.admin, role="attacker", version="kali-inc",
                                        domain_xml=DOMAIN_XML)  # no disk again → refused
        self.assertIn("no backing disk", error.exception.message)
        image2, created = services.save_machine_image(
            self.admin, role="attacker", version="kali-inc",
            domain_xml=DOMAIN_XML, disk=qcow2_file(b"completed-disk"))
        self.assertFalse(created)
        self.assertTrue(image2.disk_file)
        self.assertEqual(image2.disk_sha256,
                         hashlib.sha256(b"QFI\xfb\x00\x03" + b"completed-disk").hexdigest())
        self.assertEqual(image2.domain_xml, DOMAIN_XML)
        # Now it behaves like any completed version: immutable.
        with self.assertRaises(ServiceError):
            services.save_machine_image(self.admin, role="attacker", version="kali-inc",
                                        domain_xml=DOMAIN_XML, disk=qcow2_file(b"swap"))

    def test_incomplete_capture_completion_validates_qcow2(self):
        from portal.apps.labs.models import MachineImage
        MachineImage.objects.create(role="target", version="t-inc", domain_xml="")
        with self.assertRaises(ServiceError) as error:
            services.save_machine_image(self.admin, role="target", version="t-inc",
                                        domain_xml=DOMAIN_XML,
                                        disk=SimpleUploadedFile("d", b"CD001" + b"\x00" * 8))
        self.assertIn("qcow2", error.exception.message)

    def test_build_artifact_out_of_order_chunks(self):
        """Chunks land at their file offsets in any order; re-sending a
        chunk is an idempotent overwrite; assembly needs exactly the
        artifact's size on disk (no per-chunk parts, no assembled copy)."""
        from portal.apps.labs.models import MachineBuild
        host = make_host("order-host")
        build = MachineBuild.objects.create(host=host, role="target",
                                            version="kali-ooo", base_version="")
        content = b"QFI\xfb\x00\x03half-one-half-two"
        half = len(content) // 2
        chunk_bytes = half
        sha = hashlib.sha256(content).hexdigest()
        # Chunk 2 lands FIRST, chunk 1 completes the transfer.
        r2 = services.receive_build_artifact(build, file_name="disk-template",
                                             seq=2, total=2, chunk_bytes=chunk_bytes,
                                             blob=content[half:], sha=sha, size=len(content))
        self.assertFalse(r2["complete"])
        # Re-sending the same chunk is idempotent (still not complete).
        r2_again = services.receive_build_artifact(build, file_name="disk-template",
                                                   seq=2, total=2, chunk_bytes=chunk_bytes,
                                                   blob=content[half:], sha=sha, size=len(content))
        self.assertFalse(r2_again["complete"])
        r1 = services.receive_build_artifact(build, file_name="disk-template",
                                             seq=1, total=2, chunk_bytes=chunk_bytes,
                                             blob=content[:half], sha=sha, size=len(content))
        self.assertTrue(r1["complete"])
        image = MachineImage.objects.get(role="target", version="kali-ooo")
        self.assertEqual(image.disk_file.name, f"images/target-kali-ooo-disk-template")
        image.disk_file.open("rb")
        self.assertEqual(image.disk_file.read(), content)
        image.disk_file.close()
        # The staging area is gone once the artifact is attached.
        import pathlib
        from django.conf import settings
        self.assertFalse(
            (pathlib.Path(settings.PRIVATE_TEMPLATES_DIR) / ".build-upload" / f"build-{build.id}").exists())


class BuildUploadRetryTests(TestCase):
    """Re-issuing the reverse artifact upload after a failed capture push."""

    def setUp(self):
        self.admin = make_admin("550000000000000001")
        self.user = make_user("550000000000000002", "Player")
        self.host = make_host("upload-host")

    def _captured_build(self):
        from portal.apps.labs.models import MachineBuild
        build = services.start_build(self.admin, host_id=self.host.id,
                                     role="attacker", version="kali-up", base_version="")
        MachineBuild.objects.filter(pk=build.id).update(state=MachineBuild.State.CAPTURED)
        build.refresh_from_db()
        return build

    def test_retry_queues_a_build_upload_command(self):
        from portal.apps.labs.models import HostCommand, MachineBuild
        build = self._captured_build()
        command = services.retry_build_upload(self.admin, build.id)
        self.assertEqual(command.operation, "build-upload")
        self.assertEqual(command.status, HostCommand.Status.PENDING)
        self.assertEqual(command.payload, {"role": "attacker", "version": "kali-up",
                                           "build_id": build.id})
        self.assertEqual(command.host, self.host)

    def test_retry_is_idempotent_and_reissues_completed(self):
        from portal.apps.labs.models import HostCommand, MachineBuild
        build = self._captured_build()
        first = services.retry_build_upload(self.admin, build.id)
        HostCommand.objects.filter(pk=first.pk).update(status=HostCommand.Status.FAILED,
                                                       result={"error": "chunk rejected"})
        second = services.retry_build_upload(self.admin, build.id)
        self.assertEqual(second.pk, first.pk)
        second.refresh_from_db()
        self.assertEqual(second.status, HostCommand.Status.PENDING)
        self.assertEqual(second.result, {})

    def test_retry_refused_for_active_and_uncaptured_builds(self):
        from portal.apps.labs.models import MachineBuild
        build = services.start_build(self.admin, host_id=self.host.id,
                                     role="attacker", version="kali-up2", base_version="")
        with self.assertRaises(ServiceError) as error:
            services.retry_build_upload(self.admin, build.id)  # still starting
        self.assertIn("captured", error.exception.message)

    def test_retry_requires_admin_and_online_host(self):
        from portal.apps.labs.models import Host, MachineBuild
        build = self._captured_build()
        with self.assertRaises(ServiceError):
            services.retry_build_upload(self.user, build.id)
        Host.objects.filter(pk=self.host.pk).update(online=False)
        with self.assertRaises(ServiceError) as error:
            services.retry_build_upload(self.admin, build.id)
        self.assertIn("offline", error.exception.message)
