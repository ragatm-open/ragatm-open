"""Le clic ne doit pas ralentir le chat — critère d'acceptation mesuré.

Le seuil vérifié ici porte sur le travail SERVEUR du chemin synchrone, hors
latence réseau. Il est volontairement mesuré plutôt que supposé : la promesse
« le feedback n'ajoute pas de latence perceptible » ne vaut que si elle est
tenue en présence de données (empreintes, comptage de signalants, écriture).

Le test vérifie aussi la propriété qui garantit cette latence dans le temps :
le nombre de requêtes SQL est BORNÉ. Un seuil de millisecondes finirait par
diverger selon la machine ; un compte de requêtes, non.
"""
from __future__ import annotations

import time

import pytest
from django.test import override_settings

from apps.audit.models import RagQueryLog
from apps.feedback.models import AnswerFeedback

pytestmark = pytest.mark.django_db

ENDPOINT = "/api/v1/chat/feedback/"

#: Ce que ce seuil doit attraper : une régression d'ORDRE DE GRANDEUR — appel
#: réseau, balayage de table, appel LLM sur le chemin du clic. Tous coûteraient
#: des centaines de millisecondes au minimum.
#:
#: Ce qu'il ne doit PAS faire : policer le bruit machine. Mesuré 6,2 ms sur une
#: machine au repos, mais 57 ms pendant une compilation d'images Docker — la
#: valeur réelle varie d'un facteur 10 selon la charge, sans qu'aucun code n'ait
#: changé. Un seuil calé sur le critère d'acceptation (50 ms) échouerait donc
#: par intermittence, et un test qui échoue au hasard finit par être ignoré.
#:
#: La garantie de latence dans la durée est portée par le test voisin, qui borne
#: le nombre de REQUÊTES SQL : ce compte-là ne dépend pas de la machine.
#:
#: ⚠️ Mesure À CHAUD. Le premier appel d'un processus paie en plus les imports
#: tardifs (~180 ms observés), coût payé une fois par worker et non par clic.
MAX_SECONDS = 0.25


@pytest.fixture
def crowded(db, user, workspace, rag_space):
    """Un fonds de signalements existants : le cas où le calcul coûte le plus."""
    from apps.feedback.services.create_feedback import create_feedback

    for i in range(30):
        log = RagQueryLog.objects.create(
            user=user, workspace=workspace, rag_space=rag_space,
            question="what is Nordexia Conseil?", answer="I don't know.",
            status="no_answer", policy="no_answer", llm_model="qwen3:30b-instruct",
        )
        create_feedback(
            user=user, log=log, rating=AnswerFeedback.RATING_DOWN, kind="info_in_docs"
        )
        # Les feedbacks partagent la même empreinte : c'est le pire cas pour le
        # comptage de signalants distincts.
    return None


def test_click_is_fast_even_with_existing_incidents(api_client, user, workspace, rag_space, crowded):
    log = RagQueryLog.objects.create(
        user=user, workspace=workspace, rag_space=rag_space,
        question="what is Nordexia Conseil?", answer="I don't know.",
        status="no_answer", policy="no_answer", llm_model="qwen3:30b-instruct",
    )

    # Préchauffage : le PREMIER appel du processus paie les imports tardifs
    # (sérialiseurs, services, throttle). Ce coût est payé une fois par worker,
    # pas par clic — le mesurer reviendrait à mesurer le démarrage.
    warm = RagQueryLog.objects.create(
        user=user, workspace=workspace, rag_space=rag_space, question="warmup", status="ok"
    )
    api_client.post(ENDPOINT, {"log_id": str(warm.id), "score": -1}, format="json")

    started = time.perf_counter()
    res = api_client.post(
        ENDPOINT,
        {"log_id": str(log.id), "score": -1, "kind": "info_in_docs",
         "expected_term": "Nordexia Conseil"},
        format="json",
    )
    elapsed = time.perf_counter() - started

    assert res.status_code == 200
    print(f"\n[latence] chemin synchrone du clic (à chaud) : {elapsed * 1000:.1f} ms")
    assert elapsed < MAX_SECONDS, f"chemin synchrone trop lent : {elapsed * 1000:.0f} ms"


def test_click_issues_a_bounded_number_of_queries(
    api_client, user, workspace, rag_space, crowded, django_assert_max_num_queries
):
    """La borne est ce qui garantit la latence dans la durée.

    Le budget couvre : lecture du journal, comptage des signalants, insertion,
    double écriture historique, plus la mécanique transactionnelle et
    d'authentification de DRF.
    """
    log = RagQueryLog.objects.create(
        user=user, workspace=workspace, rag_space=rag_space,
        question="autre question", status="ok",
    )
    with django_assert_max_num_queries(12):
        res = api_client.post(ENDPOINT, {"log_id": str(log.id), "score": -1}, format="json")
    assert res.status_code == 200


@override_settings(CELERY_TASK_ALWAYS_EAGER=False)
def test_click_does_not_wait_for_the_snapshot(api_client, user, workspace, rag_space, monkeypatch):
    """La capture lourde est enfilée APRÈS commit, jamais exécutée en ligne."""
    from apps.feedback.services import create_feedback as module

    calls: list = []
    monkeypatch.setattr(module, "_enqueue_snapshot", lambda fid: calls.append(fid))

    log = RagQueryLog.objects.create(
        user=user, workspace=workspace, rag_space=rag_space, question="q", status="ok"
    )
    res = api_client.post(ENDPOINT, {"log_id": str(log.id), "score": -1}, format="json")

    assert res.status_code == 200
    # Le snapshot n'existe pas encore à la réponse : c'est tout l'intérêt.
    from apps.feedback.models import FeedbackDiagnosticSnapshot

    assert not FeedbackDiagnosticSnapshot.objects.exists()
