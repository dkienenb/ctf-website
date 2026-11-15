from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView, TemplateView

from portal.apps.accounts import views as account_views
from portal.apps.content import views as content_views
from portal.apps.submissions import views as submission_views
from portal.apps.submissions import manage_views
from portal.apps.labs import views as labs_views
from portal.apps.labs import agent_views as labs_agent_views
from portal.apps.labs import manage_views as labs_manage_views

urlpatterns = [
    path("admin/", admin.site.urls),
    # Discord is the only auth path; the stock allauth login view expects a
    # local username field we deliberately do not have.
    path("accounts/login/", RedirectView.as_view(url="/accounts/discord/login/", permanent=False)),
    # Local signup does not exist; the URL hands out to the portal instead.
    path("accounts/signup/", RedirectView.as_view(url="/", permanent=False)),
    path("accounts/", include("allauth.urls")),
    path("status/", account_views.status_page, name="status"),
    path("settings/", account_views.settings_page, name="settings"),
    path("settings/profile/refresh", account_views.refresh_profile, name="refresh-profile"),
    path("", content_views.home, name="home"),
    path("pools/", content_views.pools_page, name="pools"),
    path("pools/<slug:pool_slug>/", content_views.home, name="pool"),
    path("challenges/<slug:slug>/", content_views.challenge_detail, name="challenge_detail"),
    path(
        "challenges/<slug:slug>/attachments/<int:attachment_id>/download",
        content_views.download,
        name="attachment-download",
    ),
    path(
        "challenges/<slug:slug>/questions/<int:question_id>/submit",
        submission_views.submit_answer,
        name="submit-answer",
    ),
    path(
        "challenges/<slug:slug>/hints/<int:hint_id>/redeem",
        submission_views.redeem_hint,
        name="redeem-hint",
    ),
    path("leaderboard/", submission_views.leaderboard, name="leaderboard"),
    path("progress/", submission_views.progress, name="progress"),
    path("notifications/", submission_views.notifications, name="notifications"),
    path("notifications/poll", submission_views.notifications_poll, name="notifications-poll"),
    path("notifications/read", submission_views.mark_notifications_read, name="notifications-read"),
    path("manage/", manage_views.dashboard, name="manage"),
    path("manage/questions/<int:question_id>/edit", manage_views.question_edit, name="manage-question-edit"),
    path("manage/hints/<int:hint_id>/edit", manage_views.hint_edit, name="manage-hint-edit"),
    path("manage/challenges/<int:challenge_id>/edit", manage_views.content_edit, name="manage-content-edit"),
    path("manage/challenges/<int:challenge_id>/attachments/upload", manage_views.upload_attachment, name="manage-attachment-upload"),
    path("manage/challenges/create", manage_views.create_challenge, name="manage-create-challenge"),
    path("manage/pools", manage_views.pools, name="manage-pools"),
    path("manage/pools/<int:pool_id>/edit", manage_views.pool_edit, name="manage-pool-edit"),
    path("manage/action", manage_views.action, name="manage-action"),
    path("manage/preapprove", manage_views.preapprove_action, name="manage-preapprove"),
    path("manage/machines", labs_manage_views.machine_images, name="manage-machine-images"),
    path("manage/machines/new", labs_manage_views.machine_image_edit, name="manage-machine-image-new"),
    path("manage/machines/<int:image_id>/edit", labs_manage_views.machine_image_edit, name="manage-machine-image-edit"),
    path("manage/challenges/<int:challenge_id>/lab-template", manage_views.lab_template_action, name="manage-lab-template"),
    path("labs/<slug:slug>/desktop", labs_views.desktop, name="lab-desktop"),
    path("labs/<slug:slug>/<str:action>", labs_views.lab_action, name="lab-action"),
    path("agentapi/heartbeat", labs_agent_views.heartbeat, name="agent-heartbeat"),
    path("agentapi/commands", labs_agent_views.poll, name="agent-poll"),
    path("agentapi/commands/<str:op_id>/result", labs_agent_views.result, name="agent-result"),
]
