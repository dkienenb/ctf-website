"""Operator pages (handoff §21: admin content authoring, correction previews,
approvals/pre-approvals, suspension). Django's /admin/ remains for bootstrap
and data inspection; these pages implement the operator workflows.
"""
from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from portal.apps.accounts.decorators import admin_page
from portal.apps.accounts.models import ApprovalDecision, User
from portal.apps.content.models import Attachment, Challenge, Hint, Pool, Question
from portal.apps.labs import services as lab_services
from portal.apps.labs.models import LabInstance

from . import adminops, corrections
from .models import DataRevision, HintRedemption


class QuestionEditForm(forms.Form):
    prompt = forms.CharField(max_length=1000, widget=forms.Textarea(attrs={"rows": 3}))
    input_format = forms.CharField(max_length=200, required=False)
    points = forms.IntegerField(min_value=0)
    display_order = forms.IntegerField(min_value=0)
    answers = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), help_text="One accepted answer per line.")
    mode = forms.ChoiceField(choices=[
        ("none", "Remove nothing · Question corrected"),
        ("incorrect", "Remove only incorrect · Question updated"),
        ("all", "Remove all · Question replaced"),
    ])
    reason = forms.CharField(max_length=500)

    def cleaned_answers(self):
        return [line.strip() for line in self.cleaned_data["answers"].splitlines() if line.strip()]


class HintEditForm(forms.Form):
    cost = forms.IntegerField(min_value=0)
    description = forms.CharField(max_length=500)
    text = forms.CharField(max_length=4096, widget=forms.Textarea(attrs={"rows": 3}))
    reason = forms.CharField(max_length=500)


class ContentEditForm(forms.Form):
    title = forms.CharField(max_length=200)
    summary = forms.CharField(max_length=300, required=False)
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), required=False)
    pools = forms.ModelMultipleChoiceField(queryset=Pool.objects.all(), widget=forms.CheckboxSelectMultiple)
    lifecycle = forms.ChoiceField(choices=Challenge.Lifecycle.choices)
    go_live = forms.DateTimeField(required=False, help_text="Publication go-live time (leave empty to publish immediately).")
    revision = forms.IntegerField(widget=forms.HiddenInput)


class PreapproveForm(forms.Form):
    discord_id = forms.CharField(max_length=25)


class CreateChallengeForm(forms.Form):
    title = forms.CharField(max_length=200)
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 3}), required=False)
    pool = forms.ModelChoiceField(queryset=Pool.objects.all())
    prompt = forms.CharField(max_length=1000)
    answer = forms.CharField(max_length=4096)
    points = forms.IntegerField(min_value=0, initial=100)


@admin_page
def dashboard(request):
    users = User.objects.order_by("id")
    questions = Question.objects.select_related("challenge").order_by("challenge__id", "display_order")
    hints = []
    for hint in Hint.objects.select_related("challenge"):
        redemptions = list(HintRedemption.objects.filter(hint=hint).order_by("id"))
        hints.append({
            "hint": hint,
            "owners": len(redemptions),
            "first_owner_id": redemptions[0].recipient_id if redemptions else None,
        })
    challenges = Challenge.objects.prefetch_related("memberships__pool").order_by("id")
    return render(request, "manage/dashboard.html", {
        "users": users,
        "questions": questions,
        "hints": hints,
        "challenges": challenges,
        "labs": LabInstance.objects.select_related("host", "challenge").order_by("id"),
        "preapprovals": ApprovalDecision.objects.order_by("-id")[:20],
        "preapprove_form": PreapproveForm(),
        "create_form": CreateChallengeForm(),
        "revision": DataRevision.current(),
    })


@admin_page
def question_edit(request, question_id):
    question = get_object_or_404(Question.objects.select_related("challenge"), pk=question_id)
    initial = {
        "prompt": question.prompt,
        "input_format": question.input_format,
        "points": question.points,
        "display_order": question.display_order,
        "answers": "\n".join(AcceptedAnswer_values(question)),
        "mode": "none",
    }
    form = QuestionEditForm(request.POST or initial)
    preview = None
    if request.method == "POST":
        if form.is_valid():
            data = dict(
                answers=form.cleaned_answers(),
                points=form.cleaned_data["points"],
                mode=form.cleaned_data["mode"],
                prompt=form.cleaned_data["prompt"],
                input_format=form.cleaned_data["input_format"],
                display_order=form.cleaned_data["display_order"],
            )
            if "preview" in request.POST:
                try:
                    preview = corrections.preview_question_edit(question, **data)
                except corrections.ServiceError as error:
                    messages.error(request, error.message)
            elif "apply" in request.POST:
                try:
                    result = corrections.apply_question_edit(
                        request.user, question,
                        reason=form.cleaned_data["reason"],
                        expected_revision=int(request.POST.get("expected_revision", -1)),
                        **data,
                    )
                    messages.success(request, result["message"])
                    return redirect("manage")
                except (corrections.ServiceError, ValueError) as error:
                    messages.error(request, getattr(error, "message", "Apply failed."))
                    return redirect("manage-question-edit", question_id=question.id)
        else:
            messages.error(request, "Check the form fields.")
    return render(request, "manage/question_edit.html", {
        "question": question, "form": form, "preview": preview,
    })


def AcceptedAnswer_values(question):
    from portal.apps.content.models import AcceptedAnswer

    return list(
        AcceptedAnswer.objects.filter(question=question, active=True)
        .values_list("value", flat=True)
    )


@admin_page
def hint_edit(request, hint_id):
    hint = get_object_or_404(Hint.objects.select_related("challenge"), pk=hint_id)
    initial = {"cost": hint.cost, "description": hint.description, "text": hint.text}
    form = HintEditForm(request.POST or initial)
    preview = None
    if request.method == "POST" and form.is_valid():
        data = {key: form.cleaned_data[key] for key in ("cost", "description", "text")}
        if "preview" in request.POST:
            try:
                preview = adminops.preview_hint_edit(hint, **data)
            except adminops.ServiceError as error:
                messages.error(request, error.message)
        elif "apply" in request.POST:
            try:
                result = adminops.apply_hint_edit(
                    request.user, hint,
                    reason=form.cleaned_data["reason"],
                    expected_revision=int(request.POST.get("expected_revision", -1)),
                    **data,
                )
                messages.success(request, result["message"])
                return redirect("manage")
            except (adminops.ServiceError, ValueError) as error:
                messages.error(request, getattr(error, "message", "Apply failed."))
                return redirect("manage-hint-edit", hint_id=hint.id)
    elif request.method == "POST":
        messages.error(request, "Check the form fields.")
    return render(request, "manage/hint_edit.html", {"hint": hint, "form": form, "preview": preview})


@admin_page
def content_edit(request, challenge_id):
    challenge = get_object_or_404(Challenge.objects.prefetch_related("memberships__pool", "attachments"), pk=challenge_id)
    initial = {
        "title": challenge.title,
        "summary": challenge.summary,
        "description": challenge.description,
        "pools": [membership.pool for membership in challenge.memberships.all()],
        "lifecycle": challenge.lifecycle,
        "go_live": challenge.go_live,
        "revision": DataRevision.current(),
    }
    form = ContentEditForm(request.POST or initial)
    if request.method == "POST":
        if form.is_valid():
            go_live = form.cleaned_data["go_live"]
            if go_live and timezone.is_naive(go_live):
                go_live = timezone.make_aware(go_live)
            try:
                adminops.edit_content(
                    request.user, challenge,
                    title=form.cleaned_data["title"],
                    summary=form.cleaned_data["summary"],
                    description=form.cleaned_data["description"],
                    pool_ids=form.cleaned_data["pools"],
                    lifecycle=form.cleaned_data["lifecycle"],
                    go_live=go_live,
                    expected_revision=form.cleaned_data["revision"],
                )
                messages.success(request, "Content updated. Progress retained.")
                return redirect("manage")
            except adminops.ServiceError as error:
                messages.error(request, error.message)
                form = ContentEditForm(initial={**initial, "revision": DataRevision.current()})
        else:
            messages.error(request, "Check the form fields.")
    upload_form = AttachmentUploadForm()
    return render(request, "manage/content_edit.html", {
        "challenge": challenge, "form": form, "upload_form": upload_form,
    })


class AttachmentUploadForm(forms.Form):
    file = forms.FileField(allow_empty_file=False)


@admin_page
def upload_attachment(request, challenge_id):
    challenge = get_object_or_404(Challenge, pk=challenge_id)
    if request.method == "POST":
        form = AttachmentUploadForm(request.POST, request.FILES)
        if form.is_valid():
            uploaded = request.FILES["file"]
            attachment = Attachment(challenge=challenge, filename=uploaded.name[:255])
            attachment.file.save(uploaded.name, uploaded, save=False)
            attachment.filename = uploaded.name[:255]
            attachment.size = uploaded.size
            attachment.save()
            adminops._audit(request.user, "attachment-upload", "", {
                "challenge": challenge.id, "attachment": attachment.id,
            })
            DataRevision.bump()
            messages.success(request, f"Attachment {attachment.filename} uploaded.")
        else:
            messages.error(request, "Choose a file to upload.")
    return redirect("manage-content-edit", challenge_id=challenge.id)


@admin_page
def create_challenge(request):
    if request.method == "POST":
        form = CreateChallengeForm(request.POST)
        if form.is_valid():
            try:
                challenge = adminops.create_challenge(
                    request.user,
                    title=form.cleaned_data["title"],
                    description=form.cleaned_data["description"],
                    pool_id=form.cleaned_data["pool"].id,
                    prompt=form.cleaned_data["prompt"],
                    answer=form.cleaned_data["answer"],
                    points=form.cleaned_data["points"],
                )
                messages.success(request, "Draft created. It is not visible to players.")
                return redirect("manage-content-edit", challenge_id=challenge.id)
            except adminops.ServiceError as error:
                messages.error(request, error.message)
        else:
            messages.error(request, "Check the form fields.")
    return redirect("manage")


@admin_page
def action(request):
    """One endpoint for operator actions with a server-rendered confirm step."""
    if request.method != "POST":
        return redirect("manage")
    action = request.POST.get("action", "")
    target = request.POST.get("target", "")
    if "confirm" not in request.POST:
        title, text = _confirm_text(action, target)
        return render(request, "manage/confirm.html", {
            "title": title, "text": text, "action": action, "target": target,
        })
    try:
        _perform_action(request.user, action, target)
        messages.success(request, _success_text(action))
    except adminops.ServiceError as error:
        messages.error(request, error.message)
    return redirect("manage")


def _confirm_text(action, target):
    texts = {
        "suspend": ("Suspend this account?", "Immediately revoke desktop access, cancel its queued request, and save its active lab. Progress and ownership are retained."),
        "reject": ("Reject this application?", "The pending account is marked rejected. The user can sign in again to reapply; nothing is erased."),
        "refund": ("Refund this hint redemption?", "The ownership record is removed and the current price is returned to the user's score. This is audited; the user may redeem the hint again."),
        "delete-hint": ("Delete this hint?", "Only hints with no redemptions can be deleted. Redeemed hints must be refunded first."),
        "delete-draft": ("Delete this draft?", "The draft, its questions, and its hints are removed permanently. Published challenges are archived instead."),
        "publish": ("Publish now?", "The challenge becomes visible immediately at its go-live time (empty go-live means now)."),
        "archive": ("Archive this challenge?", "It disappears from browsing but retains all scoring history and hint ownership."),
        "reset-target": ("Reset the target only?", "The target returns to its pinned template. The attacker workspace, questions, hints, and scores are preserved."),
        "reset-full": ("Reset both guests?", "Both guests return to their pinned templates. Attacker history is cleared. Questions, hints, and scores are retained."),
        "recover": ("Simulate irrecoverable target loss?", "The affected target returns to its pinned template and the user is notified. Application progress is preserved."),
    }
    return texts.get(action, ("Confirm action?", "Apply this operator action?"))


def _success_text(action):
    return {
        "suspend": "Account suspended.",
        "reject": "Application rejected.",
        "refund": "Hint redemption refunded.",
        "delete-hint": "Hint deleted.",
        "delete-draft": "Draft deleted.",
        "publish": "Challenge published.",
        "archive": "Challenge archived.",
        "reset-target": "Target reset command issued.",
        "reset-full": "Full reset command issued.",
        "recover": "Recovery command issued.",
    }.get(action, "Action applied.")


def _perform_action(actor, action, target):
    if action == "approve":
        adminops.approve_user(actor, int(target))
    elif action == "reject":
        adminops.reject_user(actor, int(target))
    elif action == "suspend":
        adminops.suspend_user(actor, int(target))
    elif action == "preapprove":
        adminops.preapprove(actor, target)
    elif action == "refund":
        adminops.refund_hint(actor, int(request_user_id(target)), int(request_hint_id(target)))
    elif action == "delete-hint":
        adminops.delete_hint(actor, int(target))
    elif action == "delete-draft":
        adminops.delete_draft(actor, int(target))
    elif action == "publish":
        adminops.set_lifecycle(actor, int(target), Challenge.Lifecycle.PUBLISHED)
    elif action == "archive":
        adminops.set_lifecycle(actor, int(target), Challenge.Lifecycle.ARCHIVED)
    elif action in ("reset-target", "reset-full", "recover"):
        user_id, challenge_id = target.split(":")
        services.admin_reset(actor, int(user_id), int(challenge_id),
                             {"reset-target": "target", "reset-full": "full", "recover": "recover"}[action])
    else:
        raise adminops.ServiceError("Unknown administrator action.")


def request_user_id(target):
    # target encodes "user_id:hint_id" for refunds
    return target.split(":")[0]


def request_hint_id(target):
    return target.split(":")[1]


def preapprove_action(request):
    if request.method == "POST":
        form = PreapproveForm(request.POST)
        if form.is_valid():
            try:
                adminops.preapprove(request.user, form.cleaned_data["discord_id"])
                messages.success(request, "Discord ID pre-approved.")
            except adminops.ServiceError as error:
                messages.error(request, error.message)
        else:
            messages.error(request, "Enter a 5–25 digit Discord ID.")
    return redirect("manage")
