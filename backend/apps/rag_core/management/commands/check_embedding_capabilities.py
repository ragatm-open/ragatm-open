"""Confronte les capacités d'embedding DÉCLARÉES au registre publié par la Gateway.

Pourquoi cette commande existe
------------------------------
`GOVERNED_MODEL_MAX_INPUT_CHARS` est déclarée dans le code, délibérément : l'ingestion
ne doit pas dépendre d'un appel réseau pour savoir comment découper un document. Une
Gateway lente ou absente ne doit pas décider de la granularité d'un corpus.

Mais une déclaration figée qui s'écarte de la réalité est PIRE qu'une absence de
déclaration : elle donne à un plafond faux l'autorité d'un plafond vérifié. Le mode de
défaillance est silencieux et différé — le découpage passe, et c'est l'indexation qui
échoue, document par document, des semaines plus tard.

Cette commande est le pont : elle lit `/health/embeddings`, compare, et échoue en cas
d'écart. Elle ne corrige rien et n'écrit rien.

    python manage.py check_embedding_capabilities
    python manage.py check_embedding_capabilities --base-url http://…:8100 --json rapport.json
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.llm_router.capabilities import (
    GOVERNED_MODEL_MAX_INPUT_CHARS,
    GOVERNED_MODEL_PREFIX_CHARS,
    governed_model_max_input_chars,
)


def _fetch(base_url: str, timeout_s: float) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/health/embeddings"
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise CommandError(f"Gateway injoignable ({url}) : {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CommandError(f"{url} n'a pas rendu du JSON : {exc}") from exc


class Command(BaseCommand):
    help = "Vérifie que les plafonds d'entrée déclarés correspondent au registre de la Gateway."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--base-url", default="",
            help="Gateway à interroger (défaut : AI_GATEWAY_EMBEDDINGS_BASE_URL de l'environnement).",
        )
        parser.add_argument("--timeout", type=float, default=8.0)
        parser.add_argument("--json", dest="json_out", default="", help="Écrit le rapport JSON ici.")

    def handle(self, *args, **options) -> None:
        base_url = options["base_url"]
        if not base_url:
            from apps.llm_router.embeddings_gateway import gateway_embeddings_base_url

            base_url = gateway_embeddings_base_url()
        if not base_url:
            raise CommandError(
                "aucune Gateway d'embeddings configurée : passez --base-url. La commande "
                "compare une déclaration à une réalité — sans la seconde, elle ne prouve rien."
            )

        sante = _fetch(base_url, options["timeout"])
        publies = {
            m.get("model_logical"): m
            for m in (sante.get("models") or [])
            if m.get("kind") == "embedding" or m.get("dimension")
        }

        ecarts: list[dict[str, Any]] = []
        for logique, declare in sorted(GOVERNED_MODEL_MAX_INPUT_CHARS.items()):
            publie = publies.get(logique)
            if publie is None:
                ecarts.append({
                    "model_logical": logique, "severity": "absent",
                    "declare": declare, "publie": None,
                    "reason": "déclaré par RagATM, absent du registre de cette Gateway",
                })
                continue
            reel = publie.get("max_input_chars")
            if reel != declare:
                ecarts.append({
                    "model_logical": logique, "severity": "divergent",
                    "declare": declare, "publie": reel,
                    "reason": "plafond déclaré ≠ plafond publié : le découpage repose sur une valeur fausse",
                })

        # L'inverse compte aussi : un modèle gouverné que la Gateway sert et que
        # RagATM ne connaît pas retombera SILENCIEUSEMENT sur la borne conservatrice.
        non_declares = sorted(
            nom for nom, m in publies.items()
            if nom and nom not in GOVERNED_MODEL_MAX_INPUT_CHARS and m.get("allowed_here")
        )

        rapport = {
            "schema_version": 1,
            "base_url": base_url,
            "environment": sante.get("environment"),
            "declares": dict(sorted(GOVERNED_MODEL_MAX_INPUT_CHARS.items())),
            "ecarts": ecarts,
            "servis_non_declares": non_declares,
            "verdict": "divergence" if ecarts else "conforme",
        }
        if options["json_out"]:
            Path(options["json_out"]).write_text(
                json.dumps(rapport, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
            )

        w = self.stdout.write
        w(f"capacités d'embedding : « {sante.get('environment')} » sur {base_url}")
        w("")
        for logique, declare in sorted(GOVERNED_MODEL_MAX_INPUT_CHARS.items()):
            publie = publies.get(logique) or {}
            reel = publie.get("max_input_chars")
            etat = "OK" if reel == declare else "ÉCART"
            ici = publie.get("allowed_here")
            utile = governed_model_max_input_chars(logique)
            prefixes = GOVERNED_MODEL_PREFIX_CHARS.get(logique) or {}
            w(f"  {logique:<22} déclaré {declare:>6}   publié {str(reel):>6}   {etat:<6} "
              f"autorisé ici : {ici}")
            if prefixes:
                # Le plafond publié n'est pas le plafond utile : la Gateway ajoute un
                # préfixe AVANT de compter. C'est le second qui borne le découpage.
                w(f"  {'':<22} préfixe interne {prefixes} -> utile {utile} car.")
        if non_declares:
            w("")
            w(f"servis ici mais NON déclarés ({len(non_declares)}) : {', '.join(non_declares)}")
            w("  (un espace gouverné sur ces modèles recevrait la borne conservatrice, sans")
            w("   qu'aucune mesure ne dise si elle est nécessaire)")
        w("")

        if ecarts:
            for e in ecarts:
                w(f"  {e['model_logical']} : {e['reason']} "
                  f"(déclaré {e['declare']}, publié {e['publie']})")
            raise CommandError(
                f"exit 1 — {len(ecarts)} écart(s) entre les plafonds déclarés et le registre "
                f"de « {sante.get('environment')} ». Le découpage repose sur une valeur non vérifiée."
            )
        w("VERDICT : conforme — chaque plafond déclaré correspond au registre publié.")
