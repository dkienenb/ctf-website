"""Operator pages for machine template authoring (handoff §10, §13).

Machine images are versioned (role, version) artifacts: a pinned domain.xml
plus an uploaded base disk. Distribution to workhorses goes through the
durable command queue; the host matrix on the list page reads the inventory
each agent reports with its heartbeat.
"""
from django import forms
from django.conf import settings
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib import messages

from portal.apps.accounts.decorators import admin_page
from portal.apps.labs.guacamole import issue_build_grant_flow, GrantError as BuildGrantError
from .models import Host, LabTemplate, MachineBuild, MachineImage
from . import services as lab_services


def _host_slug(host_name):
    """Mirror of the portal-side slug used for Guacamole instance routing."""
    import re as _re
    return _re.sub(r"[^a-z0-9-]+", "-", str(host_name).lower()).strip("-") or "host"
from .models import Host, MachineImage
from . import services as lab_services


class MachineImageForm(forms.Form):
    role = forms.ChoiceField(choices=MachineImage.Role.choices)
    version = forms.CharField(max_length=80)
    notes = forms.CharField(max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 2}))
    domain_xml = forms.CharField(widget=forms.Textarea(attrs={"rows": 10, "spellcheck": "false"}))


class BuildStartForm(forms.Form):
    host = forms.ModelChoiceField(queryset=Host.objects.filter(online=True), empty_label=None)
    role = forms.ChoiceField(choices=MachineImage.Role.choices)
    base_version = forms.ChoiceField(required=False, help_text="Staged base to build from (filtered by role; empty = the demo pair on the workhorse).")
    version = forms.CharField(max_length=80, help_text="The new template version you're creating.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        images = MachineImage.objects.order_by("role", "version")
        choices = [("", "Demo pair on the workhorse")]
        for image in images:
            choices.append((image.version, f"{image.get_role_display()} / {image.version}"))
        self.fields["base_version"].choices = choices
        self.fields["base_version"].widget.attrs.update({
            "data-role-options": ",".join(f"{image.version}:{image.role}" for image in images),
        })


@admin_page
def build_start(request):
    active = MachineBuild.objects.filter(
        state__in=(MachineBuild.State.STARTING, MachineBuild.State.READY,
                             MachineBuild.State.CAPTURING, MachineBuild.State.DISCARDING,
                             MachineBuild.State.FAILED)
    ).select_related("host")
    if request.method == "POST":
        form = BuildStartForm(request.POST)
        if form.is_valid():
            try:
                build = lab_services.start_build(
                    request.user,
                    host_id=form.cleaned_data["host"].id,
                    role=form.cleaned_data["role"],
                    version=form.cleaned_data["version"].strip(),
                    base_version=form.cleaned_data["base_version"].strip(),
                    
                )
                messages.success(request, "Build VM booting. Give it a moment, then open its console.")
                return redirect("manage-build", build_id=build.id)
            except lab_services.ServiceError as error:
                messages.error(request, error.message)
        else:
            messages.error(request, "Check the form fields.")
    else:
        form = BuildStartForm()
    return render(request, "manage/build_start.html", {
        "form": form, "active_builds": active,
    })


@admin_page
def build_detail(request, build_id):

    build = get_object_or_404(MachineBuild.objects.select_related("host"), pk=build_id)
    base_versions = list(MachineImage.objects.filter(
        role=build.role).values_list("version", flat=True))
    return render(request, "manage/build_detail.html", {
        "build": build,
        "base_versions": base_versions,
        "console_url": f"/guacamole/{_host_slug(build.host.name)}/#/client/"
                       if settings.GUACAMOLE_INSTANCE_PATHS else None,
    })


@admin_page
def build_action(request, build_id):
    """Capture / discard a build, with the shared confirm flow."""

    build = get_object_or_404(MachineBuild, pk=build_id)
    if request.method != "POST":
        return redirect("manage-build", build_id=build.id)
    action = request.POST.get("do", "")
    try:
        if action == "capture":
            lab_services.capture_build(request.user, build.id)
            messages.success(request, "Capture queued: shutdown, disk compaction, template write.")
        elif action == "discard":
            lab_services.discard_build(request.user, build.id)
            messages.success(request, "Discard queued: the build guest and its scratch disk are destroyed.")
        elif action == "upload":
            lab_services.retry_build_upload(request.user, build.id)
            messages.success(request, "Upload queued: pushing the captured template back to the controller.")
        else:
            messages.error(request, "Unknown build action.")
    except lab_services.ServiceError as error:
        messages.error(request, error.message)
    return redirect("manage-build", build_id=build.id)


@admin_page
def build_console(request, build_id):
    """Guacamole grant for the build guest's VNC (attacker-role builds).

    Renders the console bridge page rather than redirecting straight to
    Guacamole: the bridge POSTs the grant alone, so the JSON provider always
    authenticates it. A direct redirect made the SPA re-send the browser's
    previous auth token with the grant, Guacamole's cached-token provider
    won, and the grant's temporary connection was silently dropped — the
    "The requested connection does not exist" the operator kept seeing."""

    build = get_object_or_404(MachineBuild.objects.select_related("host"), pk=build_id)
    try:
        flow = issue_build_grant_flow(request.user, build)
    except BuildGrantError as error:
        messages.error(request, error.message)
        return redirect("manage-build", build_id=build.id)
    return render(request, "labs/console_bridge.html", flow)


@admin_page
def lab_templates(request):
    """Overview of every challenge's pinned machine template: versions,
    sizing, and whether each referenced version actually exists with a
    distributable disk. Pinning itself stays on the challenge edit page —
    this is the read-only fleet view."""
    from portal.apps.submissions.manage_views import LabTemplateForm

    templates = (LabTemplate.objects.select_related("challenge")
                 .order_by("challenge__title"))
    images = {(image.role, image.version): image
              for image in MachineImage.objects.all()}

    def status(role, version):
        if not version:
            return {"chip": "demo", "image": None,
                    "label": "Demo template (workhorse default)"}
        image = images.get((role, version))
        if image is None:
            return {"chip": "missing", "image": None,
                    "label": "Version not in the image library"}
        if not image.disk_file:
            return {"chip": "incomplete", "image": image,
                    "label": "Record exists but has no backing disk"}
        return {"chip": "ok", "image": image, "label": "Ready"}

    rows = [{
        "template": template,
        "attacker": status(MachineImage.Role.ATTACKER, template.attacker_version),
        "target": status(MachineImage.Role.TARGET, template.target_version),
    } for template in templates]
    return render(request, "manage/lab_templates.html", {
        "rows": rows,
        "form": LabTemplateForm(),  # version choices, for reference columns
    })


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
    form = MachineImageForm(data=request.POST or None, initial=initial)
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
