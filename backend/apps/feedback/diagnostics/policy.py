"""Le dispositif diagnostique est-il activé ? — source de vérité UNIQUE.

Deux appelants s'en servent, et ils ne font pas le même travail :

- le **producteur** (`create_feedback`) évite d'enfiler une tâche inutile ;
- le **worker** (`capture_feedback_snapshot_task`) garantit l'invariant, y compris
  pour une tâche en retard, un appel direct, un producteur d'une version
  antérieure, ou un changement de configuration survenu entre l'enfilement et
  l'exécution.

La garde vit ici et nulle part ailleurs : deux expressions dupliquées finiraient
par diverger, et c'est exactement le genre de divergence qui rend un drapeau
menteur.

POURQUOI CETTE GARDE EXISTE
---------------------------
Constaté en smoke test production du 2026-07-28 : avec les cinq drapeaux à `0`,
un simple clic produisait quand même un ``FeedbackDiagnosticSnapshot`` en mode
``partial`` — donc une **seconde copie** de la question, de la réponse et des
extraits, conservée 30 jours. Un opérateur ayant désactivé le diagnostic
collectait davantage que ce qu'il croyait, et le rollback du lot 2 était
incomplet.

``CAPTURE_PROMPT`` n'entre PAS dans cette décision : il ne gouverne pas
l'existence du snapshot, seulement le contenu sensible qu'on accepte d'y mettre.
"""
from __future__ import annotations

from django.conf import settings


def diagnostics_enabled() -> bool:
    """Vrai si au moins UNE capacité de capture est explicitement activée.

    ``buffer`` capture pendant l'exécution, ``replay`` reconstitue après coup :
    l'un ou l'autre suffit à justifier un snapshot. Aucun des deux ⇒ le feedback
    reste seul, durable, et rien d'autre n'est écrit.
    """
    return bool(
        getattr(settings, "RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED", False)
        or getattr(settings, "RAG_FEEDBACK_DIAGNOSTIC_REPLAY_ENABLED", False)
    )
