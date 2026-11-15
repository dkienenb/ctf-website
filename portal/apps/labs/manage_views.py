"""Operator pages for machine template authoring (handoff §10, §13).

Machine images are versioned (role, version) artifacts: a pinned domain.xml
plus an uploaded base disk. Distribution to workhorses goes through the
durable command queue; the host matrix on the list page reads the inventory
each agent reports with its heartbeat.
"""
from django import forms
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib import messages

from portal.apps.accounts.decorators import admin_page
from .models import Host, MachineImage
from . import services as lab_services


class MachineImageForm(forms.Form):
    role = forms.ChoiceField(choices=MachineImage.Role.choices)
    version = forms.CharField(max_length=80)
    notes = forms.CharField(max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 2}))
    domain_xml = forms.CharField(widget=forms.Textarea(attrs={"rows": 10, "spellcheck": "false"}))


@admin_page
def machine_images(request):
    hosts = list(Host.objects.order_by("name"))
    images = []
    for image in MachineImage.objects.order_by("role", "version"):
        presence = []
        for host in hosts:
            files = ((host.inventory or {}).get(image.role) or {}).get(image.version) or {}
            complete = "domain.xml" in files and "disk-template" in files
            presence.append({"host": host, "complete": complete, "files": files})
        images.append({"image": image, "presence": presence})
    return render(request, "manage/machine_images.html", {
        "images": images, "hosts": hosts,
    })


@admin_page
def machine_image_edit(request, image_id=None):
    image = get_object_or_404(MachineImage, pk=image_id) if image_id else None
    initial = None
    if image:
        initial = {
            "role": image.role, "version": image.version, "notes": image.notes,
            "domain_xml": image.domain_xml,
        }
    form = MachineImageForm(request.POST or initial)
    if request.method == "POST":
        if form.is_valid():
            disk = request.FILES.get("disk")
            if image and disk is None and not image.disk_file:
                messages.error(request, "Upload a base disk (qcow2 or ISO) for this image.")
            else:
                try:
                    saved, created = lab_services.save_machine_image(
                        request.user,
                        role=form.cleaned_data["role"],
                        version=form.cleaned_data["version"].strip(),
                        domain_xml=form.cleaned_data["domain_xml"],
                        notes=form.cleaned_data["notes"],
                        disk=disk,
                    )
                    messages.success(request, (
                        "Machine image created." if created
                        else "Machine image updated. Re-distribute to refresh workhorses."
                    ))
                    return redirect("manage-machine-images")
                except lab_services.ServiceError as error:
                    messages.error(request, error.message)
        else:
            messages.error(request, "Check the form fields.")
    return render(request, "manage/machine_image_edit.html", {
        "image": image, "form": form,
    })
