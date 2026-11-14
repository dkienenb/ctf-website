import uuid as uuid_lib

from django import template

register = template.Library()


@register.simple_tag
def uuid():
    """A fresh idempotency token for answer forms (per-form render)."""
    return uuid_lib.uuid4().hex
