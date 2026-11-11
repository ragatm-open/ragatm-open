import secrets

from django.contrib import admin
from django.http import HttpResponse, JsonResponse
from django.urls import path, include
from django_otp.admin import OTPAdminSite

# Le Django admin exige le second facteur au même titre que l'API : sans cela,
# un mot de passe volé ouvrirait par `/admin/` ce que l'API refuse.
admin.site.__class__ = OTPAdminSite


def healthcheck(_request):
    """Liveness technique pour le healthcheck Docker / reverse-proxy.

    - Sans authentification, sans dépendance à un workspace/user/contexte métier.
    - Confirme uniquement que Django répond. Ne divulgue aucun détail interne.
    - La validation ALLOWED_HOSTS s'applique normalement (host strict conservé) :
      le healthcheck doit envoyer un en-tête Host autorisé.
    """
    return JsonResponse({"status": "ok"})


def ops_metrics(request):
    """Compteurs d'exploitation, en texte d'exposition Prometheus.

    Pourquoi cet endpoint existe : la politique d'échec du reranking est *fail-open*.
    Une panne de la passerelle ne casse aucune requête — elle dégrade silencieusement la
    qualité. Sans surface de lecture hors du processus, cette dégradation serait un angle
    mort. Voir docs/06-ai/reranking.md.

    Ne divulgue AUCUNE donnée métier : ni question, ni passage, ni identifiant de
    document, ni URL de service. Uniquement des compteurs à cardinalité bornée.

    **Exposition** — cette vue est servie hors de ``/api/`` **délibérément**. Le
    reverse-proxy proxifie ``/api/`` vers le frontend, qui réécrit vers le backend :
    toute route backend placée sous ``/api/`` devient donc joignable **anonymement
    depuis Internet**. Vérifié en staging : ``/api/ops/metrics`` répondait 200 en public.
    Servie sous ``/ops/``, la vue reste joignable sur le réseau interne (supervision) et
    n'est plus routée par le proxy public.

    ``RAG_OPS_METRICS_TOKEN`` ajoute, quand il est renseigné, une exigence de jeton
    (``Authorization: Bearer <jeton>``) — défense en profondeur, pas substitut au
    cloisonnement réseau.

    ``?format=json`` rend la même chose en JSON, avec les métadonnées d'agrégation.
    """
    from django.conf import settings

    from apps.rag_core.services import ops_counters
    from apps.rag_core.services.ingest.extraction.ocr import OCR_COUNTER_NAMES
    from apps.rag_core.services.rerank_gateway import COUNTER_NAMES as RERANK_COUNTER_NAMES

    # Les compteurs OCR rejoignent l'exposition existante : sans eux, un dépassement de
    # budget de page n'est visible que dans les métadonnées d'un document, donc seulement
    # pour qui sait déjà quel document regarder — inutilisable pour détecter une dérive.
    from apps.rag_core.services.ingest.process_isolation import COUNTER_FORK_REFUSE

    # `COUNTER_FORK_REFUSE` compte les extractions pour lesquelles l'isolation en
    # processus a été REFUSÉE (processus multi-thread) : sans lui, le correctif
    # pourrait être inerte en production sans que personne ne le sache.
    # Vision V2 orchestrée (CV-3E1) : séries à étiquettes FERMÉES (`{code="…"}`), jamais
    # d'identifiant de document, de crop ni d'empreinte.
    from apps.rag_core.services.raster.orchestration import COUNTER_NAMES as VISUAL_V2_COUNTER_NAMES

    COUNTER_NAMES = (
        tuple(RERANK_COUNTER_NAMES) + OCR_COUNTER_NAMES + (COUNTER_FORK_REFUSE,)
        + tuple(VISUAL_V2_COUNTER_NAMES)
    )

    expected = (getattr(settings, "RAG_OPS_METRICS_TOKEN", "") or "").strip()
    if expected:
        presented = request.META.get("HTTP_AUTHORIZATION", "")
        prefix = "Bearer "
        presented = presented[len(prefix):].strip() if presented.startswith(prefix) else ""
        # `compare_digest` : la comparaison ne doit pas fuir la longueur du jeton.
        if not presented or not secrets.compare_digest(presented, expected):
            return HttpResponse("forbidden\n", status=403, content_type="text/plain")

    values = ops_counters.snapshot(list(COUNTER_NAMES))
    meta = ops_counters.describe()

    if request.GET.get("format") == "json":
        return JsonResponse({"counters": values, "meta": meta})

    lines = [
        "# Compteurs RagATM. agregation=%s across_workers=%s"
        % (meta["aggregation"], meta["aggregated_across_workers"]),
        "# Valeurs monotones : exploiter des taux et des deltas, pas des absolus.",
    ]
    typed = set()
    for name in COUNTER_NAMES:
        base = name.split("{", 1)[0]
        if base not in typed:                     # une ligne TYPE par métrique, pas par série
            typed.add(base)
            lines.append("# TYPE %s counter" % base)
        lines.append("%s %d" % (name, values.get(name, 0)))
    # Rend l'agrégation lisible par la supervision elle-même : une métrique dont on
    # ignore la portée est inexploitable.
    lines.append("# TYPE rag_ops_counters_aggregated_across_workers gauge")
    lines.append("rag_ops_counters_aggregated_across_workers %d"
                 % (1 if meta["aggregated_across_workers"] else 0))
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain; version=0.0.4")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/health/", healthcheck, name="health"),
    # Hors de `api/` : voir la note d'exposition dans `ops_metrics`.
    path("ops/metrics", ops_metrics, name="ops-metrics"),
    path("api/v1/", include("api.v1.urls")),
]
