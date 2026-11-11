"""``python manage.py gateway_endpoints_preflight [--probe] [--expect-route pool|direct]``

LECTURE SEULE. Rend, pour les trois URL Gateway
(``AI_GATEWAY_BASE_URL``, ``AI_GATEWAY_EMBEDDINGS_BASE_URL``, ``RAG_RERANKER_BASE_URL``) :
la valeur effective du PROCESSUS, son acheminement (``pool`` / ``direct``), ses
consommateurs, et la présence d'un overlay Core en base sur ``AI_GATEWAY_BASE_URL``.

``--probe`` ajoute deux GET non inférentiels par URL distincte (``/health/runtime``,
``/pool/health/ready``) : environnement et build Gateway, passage effectif par le pool.
Jamais ``/health/ready`` (calcule un embedding).

``--expect-route`` : échoue si une des trois URL n'est pas acheminée comme attendu (à
utiliser après la bascule, ``pool``, et après un rollback, ``direct``).

À lancer DANS chaque conteneur concerné (``docker exec <conteneur> python manage.py …``) :
c'est la vue de CE processus qui compte. Code de sortie : 0 = PASS, 1 = FAIL.
"""
from __future__ import annotations

import sys

from django.core.management.base import BaseCommand

from apps.llm_router.gateway import endpoints


class Command(BaseCommand):
    help = "Préflight lecture seule des trois URL AI Gateway (directe ou AI Gateway Pool)."

    def add_arguments(self, parser):
        parser.add_argument("--probe", action="store_true",
                            help="GET /health/runtime et /pool/health/ready (non inférentiels).")
        parser.add_argument("--expect-route", choices=[endpoints.ROUTE_POOL, endpoints.ROUTE_DIRECT],
                            default=None)

    def handle(self, *args, **opts):
        report = endpoints.build_preflight(probe=bool(opts.get("probe")))
        expect = opts.get("expect_route")
        if expect:
            for e in report.endpoints:
                if e.route != expect:
                    report.problems.append(f"{e.name}:route_{e.route}_expected_{expect}")
        for line in report.as_lines():
            self.stdout.write(line)
        if report.status != "PASS":
            sys.exit(1)
