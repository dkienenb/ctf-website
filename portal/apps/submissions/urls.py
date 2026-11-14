from django.urls import path

from . import views

urlpatterns = [
    path("api/questions/<int:question_id>/submit", views.submit, name="question-submit"),
    path("api/hints/<int:hint_id>/redeem", views.redeem, name="hint-redeem"),
    path("api/leaderboard", views.leaderboard, name="leaderboard"),
    path("api/me", views.me, name="me"),
]
