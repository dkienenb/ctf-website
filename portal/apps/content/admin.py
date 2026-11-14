from django.contrib import admin

from .models import Pool


@admin.register(Pool)
class PoolAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "order")
    prepopulated_fields = {"slug": ("name",)}
