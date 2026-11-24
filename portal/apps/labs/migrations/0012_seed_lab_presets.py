"""Seed the two preconfigured labs: the Kali pair captured on 2026-11-23
and the demo pair (the workhorse's built-in default)."""
from django.db import migrations


def seed_presets(apps, schema_editor):
    LabPreset = apps.get_model("labs", "LabPreset")
    LabPreset.objects.update_or_create(
        name="Kali pair",
        defaults={
            "attacker_version": "kali-base-v1",
            "target_version": "kali-target-v1",
            "attacker_ram_mb": 4096, "attacker_vcpu": 2, "attacker_disk_gb": 20,
            "target_ram_mb": 2048, "target_vcpu": 1, "target_disk_gb": 10,
            "notes": "Captured Kali base (attacker console) and the "
                     "target-role copy of the same disk (no VNC).",
        },
    )
    LabPreset.objects.update_or_create(
        name="Demo pair",
        defaults={
            "attacker_version": "alpine-demo-v1",
            "target_version": "demo-target-v1",
            "attacker_ram_mb": 4096, "attacker_vcpu": 2, "attacker_disk_gb": 20,
            "target_ram_mb": 2048, "target_vcpu": 1, "target_disk_gb": 10,
            "notes": "The Alpine demo pair, pinned explicitly "
                     "(alpine-demo-v1 / demo-target-v1).",
        },
    )


def unseed(apps, schema_editor):
    LabPreset = apps.get_model("labs", "LabPreset")
    LabPreset.objects.filter(name__in=("Kali pair", "Demo pair")).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("labs", "0011_labpreset"),
    ]

    operations = [
        migrations.RunPython(seed_presets, unseed),
    ]
