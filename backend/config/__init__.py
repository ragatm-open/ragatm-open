# backend/config/__init__.py
from __future__ import annotations

# Celery is optional in some dev/test setups.
# Do not fail Django startup if celery isn't installed.
try:  # pragma: no cover
    from .celery import app as celery_app
except Exception:  # pragma: no cover
    celery_app = None  # type: ignore

__all__ = ("celery_app",)
