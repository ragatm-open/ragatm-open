from .base import *
import os

DEBUG = True
os.environ.setdefault("DJANGO_ENV", "dev")

# Do not force HTTPS in dev / local curl calls
SECURE_SSL_REDIRECT = False

# If you set this in prod behind a proxy, keep it there; in dev it can cause confusion.
SECURE_PROXY_SSL_HEADER = None
