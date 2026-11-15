"""Machine template authoring tests: artifact validation, chunked staging,
distribution idempotency, lab-template binding, and the operator pages."""
import base64
import hashlib
import uuid

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from portal.apps.accounts.models import User
from portal.apps.content.models import Challenge
from portal.apps.labs.models import Host, HostCommand, LabTemplate, MachineImage
from portal.apps.labs import services
from portal.apps.submissions.models import DataRevision
from portal.apps.submissions.services import ServiceError

DOMAIN_XML = (
    "<domain><name>@NAME@</name><disk>@DISK@</disk>"
    "<graphics port='@VNC_PORT@'/><source network='@LAB_NET@'/></domain>"
)


def make_admin(discord_id):
    return User.objects.create_user(discord_id, "Admin", is_admin=True, status=User.Status.APPROVED)


class MachineImageServiceTests(TestCase):
    def setUp(self):
        self.admin = make_admin("900000000000000001")
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
        disk = SimpleUploadedFile("disk-template", b"base-disk-bytes")
        image = self._image(disk=disk)
        image.refresh_from_db()
        self.assertEqual(image.disk_size, len(b"base-disk-bytes"))
        self.assertEqual(image.disk_sha256, hashlib.sha256(b"base-disk-bytes").hexdigest())

    def test_distribute_stages_chunks_idempotently_and_resets_failures(self):
        disk = SimpleUploadedFile("disk-template", b"z" * (services.STAGE_CHUNK_BYTES + 10))
        image = self._image(disk=disk)
        result = services.distribute_machine_image(self.admin, image.id)
        expected_chunks = 2  # ceil((CHUNK+10)/CHUNK)
        self.assertEqual(result["commands"], expected_chunks + 2)  # chunks + domain.xml + verify
        puts = HostCommand.objects.filter(operation="template-put")
        self.assertEqual(puts.count(), expected_chunks + 1)
        self.assertEqual(puts.filter(payload__file="disk-template").count(), expected_chunks)
        verify = HostCommand.objects.get(operation="template-verify")
        self.assertEqual(verify.payload["sha256"], image.disk_sha256)

        # Re-distribution issues nothing new (idempotent op ids).
        again = services.distribute_machine_image(self.admin, image.id)
        self.assertEqual(again["commands"], expected_chunks + 2)
        self.assertEqual(HostCommand.objects.count(), expected_chunks + 2)

        # A failed command returns to pending on re-distribution.
        puts.update(status=HostCommand.Status.FAILED, result={"error": "x"})
        services.distribute_machine_image(self.admin, image.id)
        self.assertFalse(HostCommand.objects.filter(status=HostCommand.Status.FAILED).exists())

        # Chunk payloads decode back to the full disk content.
        parts = sorted(puts.filter(payload__file="disk-template"), key=lambda c: c.payload["seq"])
        joined = b"".join(base64.b64decode(c.payload["data_b64"]) for c in parts)
        self.assertEqual(hashlib.sha256(joined).hexdigest(), image.disk_sha256)

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
        pleb = User.objects.create_user("900000000000000002", "Pleb")
        with self.assertRaises(ServiceError):
            services.save_machine_image(pleb, role="target", version="v", domain_xml=DOMAIN_XML)
        with self.assertRaises(ServiceError):
            services.distribute_machine_image(pleb, 1)


class MachineImagePageTests(TestCase):
    def setUp(self):
        self.admin = make_admin("910000000000000001")
        self.client.force_login(self.admin)

    def test_create_via_page_and_list_matrix(self):
        disk = SimpleUploadedFile("disk-template", b"disk-content")
        response = self.client.post(reverse("manage-machine-image-new"), {
            "role": "target", "version": "victim-1", "notes": "shell injection target",
            "domain_xml": DOMAIN_XML, "disk": disk,
        })
        self.assertEqual(response.status_code, 302)
        image = MachineImage.objects.get(version="victim-1")
        self.assertEqual(image.disk_sha256, hashlib.sha256(b"disk-content").hexdigest())

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
            disk=SimpleUploadedFile("d", b"abc"),
        )
        response = self.client.post(reverse("manage-action"), {
            "action": "distribute-template", "target": image.id, "confirm": "yes",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(HostCommand.objects.filter(operation="template-put").exists())

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
            disk=SimpleUploadedFile("d", b"abc"),
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
