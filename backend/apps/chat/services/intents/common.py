# backend/apps/chat/services/intents/common.py
from __future__ import annotations

import json
import re
from datetime import date
from typing import Any


def safe_json_extract(text: str) -> dict[str, Any] | None:
    """
    Extract a JSON object from a model output that may contain extra prose.

    Uses a non-greedy match to avoid swallowing multiple JSON objects or trailing content.
    """
    if not text:
        return None
    t = text.strip()

    if t.startswith("{") and t.endswith("}"):
        try:
            v = json.loads(t)
            return v if isinstance(v, dict) else None
        except Exception:
            pass

    m = re.search(r"\{.*?\}", t, re.DOTALL)
    if not m:
        return None
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception:
        return None


def model_has_field(model: Any, field_name: str) -> bool:
    try:
        model._meta.get_field(field_name)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False
