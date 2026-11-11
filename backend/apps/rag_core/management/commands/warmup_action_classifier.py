# backend/apps/rag_core/management/commands/warmup_action_classifier.py
"""Warm-up du modèle classifieur (lot 4 / staging, §4).

Envoie un prompt FIXE, non sensible (aucune donnée utilisateur) au modèle
classifieur pour charger le modèle en mémoire (évite le cold-load sur la première
requête réelle). Échec NON bloquant mais observable (code retour + log).

À lancer au démarrage backend (staging) et/ou via un keep-alive (§5).
"""
from __future__ import annotations

import time

from django.conf import settings
from django.core.management.base import BaseCommand

# Prompt FIXE non sensible (ne JAMAIS utiliser une question réelle).
_WARMUP_TEXT = "warmup ping classifieur"


class Command(BaseCommand):
    help = "Charge le modèle classifieur (warm-up) avec un prompt fixe non sensible."

    def add_arguments(self, parser):
        parser.add_argument("--repeat", type=int, default=1, help="Nombre d'appels de chauffe.")

    def handle(self, *args, **opts):
        if not getattr(settings, "ACTIONS_LLM_CLASSIFIER_ENABLED", False):
            self.stdout.write(self.style.WARNING("classifieur désactivé — warm-up ignoré."))
            return

        from apps.actions.classifier import _classifier_enabled, _config_error, classify_action

        if _config_error():
            self.stdout.write(self.style.ERROR(f"config invalide ({_config_error()}) — warm-up ignoré."))
            return
        if not _classifier_enabled():
            return

        model = getattr(settings, "ACTIONS_LLM_CLASSIFIER_MODEL", "") or "(défaut Gateway)"
        lats = []
        ok = 0
        for i in range(max(1, opts["repeat"])):
            t = time.perf_counter()
            try:
                out = classify_action(_WARMUP_TEXT)  # prompt fixe, non sensible
                ok += out is not None
            except Exception as e:  # non bloquant
                self.stdout.write(self.style.WARNING(f"  warm-up call {i} exception: {e.__class__.__name__}"))
            lats.append(int((time.perf_counter() - t) * 1000))

        self.stdout.write(f"warmup model={model} calls={len(lats)} ok={ok} latencies_ms={lats}")
        if lats:
            self.stdout.write(f"  first={lats[0]}ms last={lats[-1]}ms (le 1er = cold-load attendu)")
