from django.apps import AppConfig


class ContentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "portal.apps.content"
    verbose_name = "Pools, challenges, questions, hints"
