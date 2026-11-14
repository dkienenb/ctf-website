"""
Django settings for the CTF practice portal (production baseline).

Handoff reference: ctf-prototype/docs/CTF_Website_Technician_Handoff.md
Owner decisions: ctf-prototype/docs/DECISIONS.md ("Owner decisions recorded November 14, 2026")
"""
from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent
PRIVATE_ATTACHMENTS_DIR = BASE_DIR / "private_attachments"


def env(name, default=""):
    return os.environ.get(name, default)


SECRET_KEY = env("DJANGO_SECRET_KEY", "insecure-dev-key-change-me")
DEBUG = env("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = [h for h in env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]

# The public Worker/proxy path forwards to this application; never trust
# arbitrary Host headers. Behind the tunnel Caddy binds a specific host/port.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_COOKIE_HTTPONLY = False  # allauth/JS may need to read the token
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
REFERRER_POLICY = "same-origin"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # allauth
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.discord",
    # application
    "portal.apps.accounts",
    "portal.apps.content",
    "portal.apps.submissions",
    "portal.apps.labs",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

ROOT_URLCONF = "portal.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "portal" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "portal.wsgi.application"

# Authoritative database: PostgreSQL (handoff §14). Parse DATABASE_URL if set,
# otherwise discrete POSTGRES_* variables (docker-compose / .env).
if env("DATABASE_URL"):
    from urllib.parse import urlparse, unquote

    _u = urlparse(env("DATABASE_URL"))
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": unquote(_u.path.lstrip("/")),
            "USER": unquote(_u.username or ""),
            "PASSWORD": unquote(_u.password or ""),
            "HOST": _u.hostname or "",
            "PORT": _u.port or 5432,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("POSTGRES_DB", "ctf"),
            "USER": env("POSTGRES_USER", "ctf"),
            "PASSWORD": env("POSTGRES_PASSWORD", "ctf"),
            "HOST": env("POSTGRES_HOST", "127.0.0.1"),
            "PORT": env("POSTGRES_PORT", "5432"),
        }
    }

AUTH_PASSWORD_VALIDATORS = []  # No local passwords: Discord OAuth is the only auth path.

# --- Identity and approval (handoff §3) -------------------------------------
# The Discord user ID is the external identity key; cached profile fields are
# never ownership keys. Approval is a backend authorization condition.
AUTH_USER_MODEL = "accounts.User"

# Owner decision 2026-11-14: 10 seconds between counted attempts.
SUBMISSION_COOLDOWN_SECONDS = int(env("SUBMISSION_COOLDOWN_SECONDS", "10"))

# --- allauth / Discord (handoff §3) ------------------------------------------
AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

SOCIALACCOUNT_PROVIDERS = {
    "discord": {
        "APP": {
            "client_id": env("DISCORD_CLIENT_ID", ""),
            "secret": env("DISCORD_CLIENT_SECRET", ""),
            "key": "",
        },
        "SCOPE": ["identify"],
    }
}

# Social signups only: account creation happens through Discord first login.
# ACCOUNT_SIGNUP_ENABLED must stay True for allauth's social auto-signup;
# local signup is made unreachable by the /accounts/signup/ URL override.
ACCOUNT_SIGNUP_ENABLED = True
ACCOUNT_EMAIL_VERIFICATION = "none"
ACCOUNT_UNIQUE_EMAIL = False
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_QUERY_EMAIL = False
SOCIALACCOUNT_ADAPTER = "portal.apps.accounts.adapter.DiscordAccountAdapter"
# No email on the user model: the Discord identify scope supplies no address.
ACCOUNT_USER_MODEL_EMAIL_FIELD = None
# The user model has no username field either (Discord-keyed identity);
# allauth must not look it up during social signup.
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
# Nominal values: local login/signup forms are unreachable by URL override.
# An empty login-methods set is what allauth expects for social-only users
# whose model has neither username nor email fields.
ACCOUNT_LOGIN_METHODS = set()
ACCOUNT_SIGNUP_FIELDS = ["password1*", "password2*"]
# No email on the user model: Discord identify scope supplies no address.
# Routes under /accounts/ per allauth defaults; login is Discord-only.
LOGIN_REDIRECT_URL = "/"
ACCOUNT_LOGOUT_REDIRECT_URL = "/"

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "portal" / "static"]

# Production static serving through the app itself (compressed; WhiteNoise
# sets far-future cache headers and handles immutable-versioned URLs).
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
