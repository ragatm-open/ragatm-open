"""L'AI Gateway est-elle libre ? — LECTURE SEULE, avant toute opération lourde.

Le calcul des deux instances de Gateway est PARTAGÉ. Une campagne de qualification, un
banc de latence ou une réindexation lancés pendant qu'elle travaille produisent deux
dommages distincts :

- ils **dégradent** le service en cours — mesuré, ×2,5 sur la latence de reranking de
  la production ;
- ils **faussent leur propre mesure**, puisqu'ils s'ajoutent à la charge qu'ils
  observent. C'est ainsi qu'un banc se disqualifie lui-même.

Cette commande répond à la seule question qui précède : peut-on y aller maintenant ?
Elle croise deux signaux : l'occupation que la Gateway DÉCLARE (`/health/runtime`) et
celle qu'une sonde SUBIT. Le premier est certain mais aveugle à l'instance voisine ; le
second la voit, par son ombre sur le GPU partagé.

    python manage.py check_gateway_quiet
    python manage.py check_gateway_quiet --base-url http://…:8200 --samples 9
"""
from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

# On importe le MODULE et non les noms : une fonction importée par son nom est
# figée à l'import, et devient impossible à substituer proprement en test — un
# premier test qui charge ce module sous patch fige le mock pour tous les suivants.
from apps.llm_router import gateway_load


class Command(BaseCommand):
    help = "Vérifie que l'AI Gateway n'est pas déjà occupée (lecture seule)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--base-url", default="", help="Défaut : la Gateway de reranking de l'environnement.")
        parser.add_argument("--samples", type=int, default=5)
        parser.add_argument("--json", dest="json_out", default="")

    def handle(self, *args, **options) -> None:
        base_url = options["base_url"]
        if not base_url:
            from apps.rag_core.services import rerank_gateway as rg

            try:
                base_url = rg._base_url()
            except Exception as exc:  # noqa: BLE001
                raise CommandError(
                    "aucune Gateway configurée : passez --base-url. Sans cible, la "
                    "commande ne prouve rien."
                ) from exc

        charge = gateway_load.runtime_load(base_url)
        seuil = gateway_load.busy_threshold_ms()
        declaree_occupee = bool(charge and (charge["inflight"] > 0 or charge["queued"] > 0))

        # La sonde n'est lancée QUE si le premier signal n'a pas déjà tranché : sonder
        # une Gateway qui se déclare occupée ajouterait précisément la charge que cette
        # commande sert à ne pas ajouter.
        mesure = (
            None if declaree_occupee
            else gateway_load.probe_latency_ms(base_url, samples=max(1, options["samples"]))
        )

        if declaree_occupee:
            verdict = "occupée"
        elif mesure is None:
            verdict = "inconnu"
        else:
            verdict = "libre" if mesure <= seuil else "occupée"

        rapport = {
            "base_url": base_url,
            "inflight": (None if charge is None else charge["inflight"]),
            "queued": (None if charge is None else charge["queued"]),
            "isolation": (None if charge is None else charge["isolation"]),
            "sonde_mediane_ms": (None if mesure is None else round(mesure, 1)),
            "seuil_ms": seuil,
            "verdict": verdict,
        }
        if options["json_out"]:
            Path(options["json_out"]).write_text(
                json.dumps(rapport, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

        w = self.stdout.write
        w(f"Gateway {base_url}")
        if charge is None:
            # Ne pas publier de compteur n'est pas publier zéro : on le dit, plutôt
            # que de laisser croire que le premier signal a répondu « au repos ».
            w("  occupation déclarée : NON PUBLIÉE (/health/runtime muet)")
        else:
            w(f"  occupation déclarée : {charge['inflight']} en cours, "
              f"{charge['queued']} en attente   (isolation : {charge['isolation']})")
        if declaree_occupee:
            w("")
            w("VERDICT : OCCUPÉE — la Gateway le déclare elle-même.")
            w("  Aucune sonde n'est nécessaire pour trancher, et en envoyer une")
            w("  ajouterait à la charge qu'on cherche à ne pas aggraver.")
            raise CommandError("exit 1 — la Gateway travaille déjà.")
        if mesure is None:
            w("  sonde INJOIGNABLE — la garde est inopérante, pas rassurante.")
            w("  Une Gateway qui ne répond pas à une sonde ne répondra pas à une campagne.")
            raise CommandError("exit 1 — impossible de conclure sur la disponibilité.")

        w(f"  occupation subie    : sonde médiane {mesure:.0f} ms   (seuil {seuil:.0f} ms)")
        if charge is not None:
            # La distinction n'est pas décorative : le compteur ne voit PAS l'instance
            # voisine (`sibling_inflight: not_observable_here`), et c'est elle qui a
            # provoqué l'incident du 22/08.
            w("  (le compteur ci-dessus ne voit que CETTE instance ; la sonde, elle,")
            w("   subit aussi la charge de l'instance voisine sur le GPU partagé)")
        w("")
        if mesure > seuil:
            w("VERDICT : OCCUPÉE.")
            w("  Le calcul est PARTAGÉ avec la production. Lancer une opération lourde")
            w("  maintenant dégraderait un service en cours, et fausserait la mesure par")
            w("  sa propre charge.")
            raise CommandError("exit 1 — la Gateway travaille déjà.")
        w("VERDICT : libre — une opération lourde peut démarrer.")
