"""Operator pages (handoff §21: admin content authoring, correction previews,
approvals/pre-approvals, suspension). Django's /admin/ remains for bootstrap
and data inspection; these pages implement the operator workflows.
"""
from django import forms
from django.contrib import messages
from django.forms import formset_factory
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from portal.apps.accounts.decorators import admin_page
from portal.apps.submissions import corrections
from portal.apps.submissions.corrections import MODE_MESSAGES
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
    author = forms.CharField(max_length=100, required=False, help_text="Display-only credit shown on the challenge page.")
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 4}), required=False)
    pools = forms.ModelMultipleChoiceField(queryset=Pool.objects.all(), widget=forms.CheckboxSelectMultiple)
    lifecycle = forms.ChoiceField(choices=Challenge.Lifecycle.choices)
    go_live = forms.DateTimeField(required=False, help_text="Publication go-live time (leave empty to publish immediately).")
    revision = forms.IntegerField(widget=forms.HiddenInput)


class PreapproveForm(forms.Form):
    discord_id = forms.CharField(max_length=25)


class CreateChallengeForm(forms.Form):
    title = forms.CharField(
        max_length=200,
        help_text="Players see this in the challenge browser. You can rename it later.",
    )
    author = forms.CharField(
        max_length=100, required=False,
        help_text="Display-only credit shown on the challenge page (e.g. your name or a co-author list).",
    )
    description = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}), required=False,
        help_text="Markdown-ish flavor text shown on the challenge page. Optional.",
    )
    pools = forms.ModelMultipleChoiceField(
        queryset=Pool.objects.all(), widget=forms.CheckboxSelectMultiple,
        help_text="Which pools the challenge appears in. More can be assigned later on the edit page.",
    )


class QuestionForm(forms.Form):
    """One question of a new challenge: text fields only — prompt and
    accepted answers (one per line, exact-match alternates like the edit
    page). Points default to 100 and are adjusted in the challenge editor."""

    prompt = forms.CharField(
        max_length=1000, required=False,
        label="Prompt",
        help_text="What players must answer.",
    )
    answers = forms.CharField(
        label="Accepted answers (one per line)",
        widget=forms.Textarea(attrs={"rows": 2}), required=False,
        help_text="Each line is an accepted answer; matching is exact.",
    )

    def clean(self):
        data = super().clean()
        prompt = (data.get("prompt") or "").strip()
        answers = [line.strip() for line in (data.get("answers") or "").splitlines() if line.strip()]
        if not prompt and not answers:
            self.skipped = True  # entirely blank extra row
            return data
        if not prompt:
            self.add_error("prompt", "Prompt is required.")
        if not answers:
            self.add_error("answers", "At least one accepted answer is required.")
        return data

    def cleaned_answers(self):
        return [line.strip() for line in (self.cleaned_data.get("answers") or "").splitlines() if line.strip()]


QuestionFormSet = formset_factory(QuestionForm, extra=0, can_delete=True)


@admin_page
def dashboard(request):
    def _preapproval_rows():
        """Only live pre-approvals: awaiting first sign-in. Entries with an
        account or a revocation are dropped from the list (the accounts
        panel and the ledger history cover them)."""
        rows = []
        for decision in ApprovalDecision.objects.filter(decision="preapprove").order_by("-id")[:20]:
            if User.objects.filter(discord_id=decision.discord_id).exists():
                continue  # already used: the accounts panel shows the account
            latest = ApprovalDecision.objects.filter(
                discord_id=decision.discord_id,
                decision__in=("preapprove", "approve", "revoke-preapprove"),
            ).order_by("-id").first()
            if latest is None or latest.decision == "revoke-preapprove":
                continue  # revoked: not pending anything
            rows.append({
                "decision": decision,
                "account_pk": None,
                "account_status": "",
                "revoked": False,
            })
        return rows

    users = User.objects.order_by("id")
    questions = Question.objects.select_related("challenge").order_by("challenge__id", "display_order")
    hints = []
    for hint in Hint.objects.select_related("challenge"):
        redemptions = list(HintRedemption.objects.filter(hint=hint).order_by("id"))
        hints.append({
            "hint": hint,
            "owners": len(redemptions),
            "first_owner_id": redemptions[0].user_id if redemptions else None,
        })
    challenges = Challenge.objects.prefetch_related("memberships__pool").order_by("id")
    return render(request, "manage/dashboard.html", {
        "users": users,
        "questions": questions,
        "hints": hints,
        "challenges": challenges,
        "labs": LabInstance.objects.select_related("host", "challenge").order_by("id"),
        "preapprovals": _preapproval_rows(),
        "preapprove_form": PreapproveForm(),
        "revision": DataRevision.current(),
    })


class InlineQuestionForm(forms.Form):
    """Compact per-question editor embedded in the challenge edit page.
    Applies through the corrections engine (audit + regrade semantics)."""

    prompt = forms.CharField(max_length=1000)
    answers = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}))
    points = forms.IntegerField(min_value=0)
    display_order = forms.IntegerField(min_value=0)
    input_format = forms.CharField(max_length=500, required=False)
    mode = forms.ChoiceField(choices=[(m, MODE_MESSAGES[m]) for m in corrections.MODES])
    reason = forms.CharField(max_length=500)
    revision = forms.IntegerField(widget=forms.HiddenInput)

    def cleaned_answers(self):
        return [line.strip() for line in self.cleaned_data["answers"].splitlines() if line.strip()]


@admin_page
def question_inline(request, question_id):
    """Apply an inline question edit from the challenge edit page."""
    question = get_object_or_404(Question.objects.select_related("challenge"), pk=question_id)
    if request.method != "POST":
        return redirect("manage-content-edit", challenge_id=question.challenge_id)
    form = InlineQuestionForm(request.POST)
    if form.is_valid():
        try:
            result = corrections.apply_question_edit(
                request.user, question,
                answers=form.cleaned_answers(),
                points=form.cleaned_data["points"],
                mode=form.cleaned_data["mode"],
                prompt=form.cleaned_data["prompt"],
                input_format=form.cleaned_data["input_format"],
                display_order=form.cleaned_data["display_order"],
                reason=form.cleaned_data["reason"],
                expected_revision=form.cleaned_data["revision"],
            )
            messages.success(request, result["message"])
        except corrections.ServiceError as error:
            messages.error(request, error.message)
    else:
        messages.error(request, "Check the form fields.")
    return redirect("manage-content-edit", challenge_id=question.challenge_id)


def _inline_question_forms(challenge):
    forms = []
    for question in challenge.questions.order_by("display_order", "id"):
        form = InlineQuestionForm(initial={
            "prompt": question.prompt,
            "answers": "\n".join(AcceptedAnswer_values(question)),
            "input_format": question.input_format,
            "points": question.points,
            "display_order": question.display_order,
            "mode": "none",
            "reason": "",
            "revision": DataRevision.current(),
        })
        forms.append((question, form))
    return forms


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
        "author": challenge.author,
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
                    author=form.cleaned_data["author"],
                    description=form.cleaned_data["description"],
                    pool_ids=[pool.id for pool in form.cleaned_data["pools"]],
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
    hint_form = HintEditForm()
    lab_template = getattr(challenge, "lab_template", None)  # reverse OneToOne accessor
    return render(request, "manage/content_edit.html", {
        "challenge": challenge, "form": form, "upload_form": upload_form,
        "hint_form": hint_form,
        "lab_template_form": LabTemplateForm(initial=_lab_template_initial(lab_template)),
        "lab_template": lab_template,
        "question_forms": _inline_question_forms(challenge),
    })


class PoolForm(forms.Form):
    name = forms.CharField(max_length=100, help_text="Shown to players in the challenge browser and navigation.")
    description = forms.CharField(widget=forms.Textarea(attrs={"rows": 2}), required=False,
                                  help_text="Shown at the top of the pool page. Optional.")
    order = forms.IntegerField(min_value=0, initial=0,
                               help_text="Lower numbers appear first in the pool list.")


@admin_page
def pools(request):
    form = PoolForm(request.POST or None)
    if request.method == "POST":
        if form.is_valid():
            try:
                adminops.create_pool(
                    request.user,
                    name=form.cleaned_data["name"],
                    description=form.cleaned_data["description"],
                    order=form.cleaned_data["order"],
                )
                messages.success(request, "Pool created.")
                return redirect("manage-pools")
            except adminops.ServiceError as error:
                messages.error(request, error.message)
        else:
            messages.error(request, "Check the form fields.")
    listing = [
        {"pool": pool, "challenges": pool.memberships.count()}
        for pool in Pool.objects.order_by("order", "id")
    ]
    return render(request, "manage/pools.html", {"listing": listing, "form": form})


@admin_page
def pool_edit(request, pool_id):
    pool = get_object_or_404(Pool, pk=pool_id)
    initial = {"name": pool.name, "description": pool.description, "order": pool.order}
    form = PoolForm(request.POST or initial)
    if request.method == "POST":
        if form.is_valid():
            try:
                adminops.edit_pool(
                    request.user, pool.id,
                    name=form.cleaned_data["name"],
                    description=form.cleaned_data["description"],
                    order=form.cleaned_data["order"],
                )
                messages.success(request, "Pool updated. Its web address (slug) is unchanged, so old links keep working.")
                return redirect("manage-pools")
            except adminops.ServiceError as error:
                messages.error(request, error.message)
        else:
            messages.error(request, "Check the form fields.")
    return render(request, "manage/pool_edit.html", {
        "pool": pool, "form": form,
        "challenges": pool.memberships.count(),
    })


class AttachmentUploadForm(forms.Form):
    file = forms.FileField(allow_empty_file=False)


class LabTemplateForm(forms.Form):
    """Challenge → pinned machine template versions + per-role sizing (§10)."""

    attacker_version = forms.ChoiceField(required=False)
    target_version = forms.ChoiceField(required=False)
    attacker_ram_mb = forms.IntegerField(min_value=128, max_value=65536, initial=4096)
    attacker_vcpu = forms.IntegerField(min_value=1, max_value=64, initial=2)
    attacker_disk_gb = forms.IntegerField(min_value=1, max_value=1024, initial=20)
    target_ram_mb = forms.IntegerField(min_value=128, max_value=65536, initial=2048)
    target_vcpu = forms.IntegerField(min_value=1, max_value=64, initial=1)
    target_disk_gb = forms.IntegerField(min_value=1, max_value=1024, initial=10)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from portal.apps.labs.models import MachineImage

        attacker = MachineImage.objects.filter(role=MachineImage.Role.ATTACKER).order_by("version")
        target = MachineImage.objects.filter(role=MachineImage.Role.TARGET).order_by("version")
        demo = [("", "Demo template (agent/SETUP.md)")]
        self.fields["attacker_version"].choices = demo + [(image.version, image.version) for image in attacker]
        self.fields["target_version"].choices = demo + [(image.version, image.version) for image in target]


def _lab_template_initial(template):
    if template is None:
        return {}
    return {
        "attacker_version": template.attacker_version,
        "target_version": template.target_version,
        "attacker_ram_mb": template.attacker_ram_mb,
        "attacker_vcpu": template.attacker_vcpu,
        "attacker_disk_gb": template.attacker_disk_gb,
        "target_ram_mb": template.target_ram_mb,
        "target_vcpu": template.target_vcpu,
        "target_disk_gb": template.target_disk_gb,
    }


@admin_page
def hint_create(request, challenge_id):
    """Create a hint from the challenge edit page."""
    from portal.apps.content.models import Challenge

    challenge = get_object_or_404(Challenge, pk=challenge_id)
    if request.method != "POST":
        return redirect("manage-content-edit", challenge_id=challenge_id)
    form = HintEditForm(request.POST)
    if form.is_valid():
        try:
            adminops.create_hint(
                request.user, challenge_id,
                description=form.cleaned_data["description"],
                text=form.cleaned_data["text"],
                cost=form.cleaned_data["cost"],
            )
            messages.success(request, "Hint created.")
        except adminops.ServiceError as error:
            messages.error(request, error.message)
    else:
        messages.error(request, "Check the form fields.")
    return redirect("manage-content-edit", challenge_id=challenge_id)


@admin_page
def lab_template_action(request, challenge_id):
    """Bind a challenge to pinned template versions + sizing."""
    from portal.apps.content.models import Challenge
    from portal.apps.labs.models import LabTemplate
    from portal.apps.labs import services as lab_services

    challenge = get_object_or_404(Challenge, pk=challenge_id)
    if request.method != "POST":
        return redirect("manage-content-edit", challenge_id=challenge_id)
    form = LabTemplateForm(request.POST)
    if form.is_valid():
        try:
            lab_services.lab_template_save(
                request.user, challenge_id,
                attacker_version=form.cleaned_data["attacker_version"],
                target_version=form.cleaned_data["target_version"],
                attacker_ram_mb=form.cleaned_data["attacker_ram_mb"],
                attacker_vcpu=form.cleaned_data["attacker_vcpu"],
                attacker_disk_gb=form.cleaned_data["attacker_disk_gb"],
                target_ram_mb=form.cleaned_data["target_ram_mb"],
                target_vcpu=form.cleaned_data["target_vcpu"],
                target_disk_gb=form.cleaned_data["target_disk_gb"],
            )
            messages.success(request, "Machine template pinned for this challenge.")
        except lab_services.ServiceError as error:
            messages.error(request, error.message)
    else:
        messages.error(request, "Check the form fields.")
    return redirect("manage-content-edit", challenge_id=challenge_id)


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
        formset = QuestionFormSet(request.POST, prefix="questions")
        if form.is_valid() and formset.is_valid():
            questions = [
                {
                    "prompt": row.cleaned_data["prompt"],
                    "answers": row.cleaned_answers(),
                    "points": 100,
                }
                for row in formset.forms
                if row.has_changed() and not getattr(row, "skipped", False)
                and not row.cleaned_data.get("DELETE")
            ]
            try:
                challenge = adminops.create_challenge(
                    request.user,
                    title=form.cleaned_data["title"],
                    author=form.cleaned_data["author"],
                    description=form.cleaned_data["description"],
                    pool_ids=[pool.id for pool in form.cleaned_data["pools"]],
                    questions=questions,
                )
                messages.success(request, "Draft created. It is not visible to players.")
                return redirect("manage-content-edit", challenge_id=challenge.id)
            except adminops.ServiceError as error:
                messages.error(request, error.message)
        else:
            messages.error(request, "Check the form fields.")
    else:
        form = CreateChallengeForm(initial={"author": request.user.cached_name or ""})
        formset = QuestionFormSet(prefix="questions", initial=[{}])
    return render(request, "manage/challenge_create.html", {
        "form": form, "formset": formset,
    })


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
        "reinstate": ("Reinstate this account?", "Desktop access is restored and the saved lab can be resumed. Progress and ownership were retained throughout."),
        "reject": ("Reject this application?", "The pending account is marked rejected. The user can sign in again to reapply; nothing is erased."),
        "refund": ("Refund this hint redemption?", "The ownership record is removed and the current price is returned to the user's score. This is audited; the user may redeem the hint again."),
        "delete-hint": ("Delete this hint?", "Only hints with no redemptions can be deleted. Redeemed hints must be refunded first."),
        "delete-draft": ("Delete this draft?", "The draft, its questions, and its hints are removed permanently. Published challenges are archived instead."),
        "publish": ("Publish now?", "The challenge becomes visible immediately at its go-live time (empty go-live means now)."),
        "archive": ("Archive this challenge?", "It disappears from browsing but retains all scoring history and hint ownership."),
        "reset-target": ("Reset the target only?", "The target returns to its pinned template. The attacker workspace, questions, hints, and scores are preserved."),
        "reset-full": ("Reset both guests?", "Both guests return to their pinned templates. Attacker history is cleared. Questions, hints, and scores are retained."),
        "recover": ("Simulate irrecoverable target loss?", "The affected target returns to its pinned template and the user is notified. Application progress is preserved."),
        "distribute-template": ("Distribute this machine image to every workhorse?", "The pinned domain.xml and base disk are staged through the durable command queue; retries are idempotent. Reset any running lab using this version first — replacing template files under running guests is not safe."),
        "delete-template": ("Delete this machine image?", "The artifact and its uploaded disk are removed. Deletion is refused while any challenge template still pins this version."),
        "delete-pool": ("Delete this pool?", "The pool disappears from the challenge browser and its members lose this grouping. Challenges, scores, and progress are untouched."),
        "revoke-preapproval": ("Revoke this pre-approval?", "The Discord ID no longer auto-approves at first sign-in. If an account already used the pre-approval, its access is untouched — suspend that account instead."),
        "clear-machine": ("Remove the machine from this challenge?", "The challenge page stops offering desktops entirely. Saved labs for it are discarded (their leases end); questions, files, hints, and scores are untouched. Challenges still holding lab slots are refused."),
    }
    return texts.get(action, ("Confirm action?", "Apply this operator action?"))


def _success_text(action):
    return {
        "suspend": "Account suspended.",
        "reinstate": "Account reinstated.",
        "reject": "Application rejected.",
        "refund": "Hint redemption refunded.",
        "delete-hint": "Hint deleted.",
        "delete-draft": "Draft deleted.",
        "publish": "Challenge published.",
        "archive": "Challenge archived.",
        "reset-target": "Target reset command issued.",
        "reset-full": "Full reset command issued.",
        "recover": "Recovery command issued.",
        "distribute-template": "Distribution queued.",
        "delete-template": "Machine image deleted.",
        "delete-pool": "Pool deleted.",
        "revoke-preapproval": "Pre-approval revoked.",
        "clear-machine": "Machine removed from this challenge.",
    }.get(action, "Action applied.")


def _perform_action(actor, action, target):
    if action == "approve":
        adminops.approve_user(actor, int(target))
    elif action == "reject":
        adminops.reject_user(actor, int(target))
    elif action == "suspend":
        adminops.suspend_user(actor, int(target))
    elif action == "reinstate":
        adminops.reinstate_user(actor, int(target))
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
        lab_services.admin_reset(actor, int(user_id), int(challenge_id),
                                 {"reset-target": "target", "reset-full": "full", "recover": "recover"}[action])
    elif action == "distribute-template":
        lab_services.distribute_machine_image(actor, int(target))
    elif action == "delete-template":
        lab_services.delete_machine_image(actor, int(target))
    elif action == "delete-pool":
        adminops.delete_pool(actor, int(target))
    elif action == "revoke-preapproval":
        adminops.revoke_preapproval(actor, target)
    elif action == "clear-machine":
        lab_services.lab_template_clear(actor, int(target))
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
