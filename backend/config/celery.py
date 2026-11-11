# backend/config/celery.py
from __future__ import annotations

import os

from celery import Celery

# Django settings module
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("ragATM")

# Read CELERY_* from Django settings (namespace CELERY_)
app.config_from_object("django.conf:settings", namespace="CELERY")

# Autodiscover tasks.py in installed apps
app.autodiscover_tasks()

# ORDONNANCEMENT — le schedule N'EST PLUS DÉFINI ICI.
#
# Il vit désormais dans `settings.CELERY_BEAT_SCHEDULE` (source unique, Lot 4-B)
# et arrive par `config_from_object(..., namespace="CELERY")` ci-dessus. Le
# définir en double ici rouvrirait exactement le défaut corrigé : deux endroits
# où lire une fréquence, donc deux endroits qui peuvent diverger.
#
# Le benchmark nocturne (`rag_benchmark_nightly`) a été RETIRÉ de l'ordonnancement
# par défaut : c'est un outil de qualité, coûteux en GPU, qui n'a rien à faire
# dans le socle d'exploitation d'une instance de production. Il reste déclenchable
# à la demande (`apps.rag_core.tasks_benchmark.run_benchmark`) et par la CI
# dédiée (`.github/workflows/rag-benchmark.yml`).


# chaque worker publie l'empreinte des réglages de capacité qu'il a chargés
# (Redis, TTL) pour le tableau « Capacités documentaires ». Observation seule, ne lève jamais.
def _connect_capability_fingerprint() -> None:
    try:
        from apps.rag_core.services.capabilities.runtime import connect_signals

        connect_signals()
    except Exception:  # noqa: BLE001
        pass


_connect_capability_fingerprint()
