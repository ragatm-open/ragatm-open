"""Les reapers de résumés dans leur forme d'exécution RÉELLE : sans délai explicite.

Pourquoi ce fichier existe
--------------------------
Pendant 37 jours, ``reap_stale_summaries_task`` a échoué à chaque exécution — en
staging comme en production — sur un ``TypeError`` : ``setting_int()`` avait été
resserré à un seul argument (le défaut appartient au registre) sans que les deux
appelants historiques soient mis à jour.

La CI ne l'a pas vu, et la raison est instructive : le code est ::

    timeout = int(timeout_s if timeout_s is not None else _pending_timeout_s())

``_pending_timeout_s()`` — seul endroit où vivait l'appel cassé — n'est atteint que
si ``timeout_s is None``. Or **les cinq appels de test existants passaient tous une
valeur explicite**. Le chemin de la production n'était couvert par rien.

Ces tests appellent donc les reapers, les tâches Celery et la commande de gestion
**sans délai**, c'est-à-dire exactement comme l'ordonnanceur les appelle.
"""
import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from apps.rag_core.models import Document, DocumentStatus, RagSpace
from apps.rag_core.models.summary import DocumentSummary
from apps.workspaces.models import Workspace

pytestmark = pytest.mark.django_db


def _doc():
    ws = Workspace.objects.create(name="W-reaper")
    space = RagSpace.objects.create(name="S-reaper", workspace=ws)
    doc = Document.objects.create(
        rag_space=space, filename="d.pdf", status=DocumentStatus.READY
    )
    return doc, space


def _summary(document, space, *, status="PENDING", variant="bullets:document::fr"):
    return DocumentSummary.objects.create(
        rag_space=space,
        document=document,
        variant_key=variant,
        mode="bullets",
        scope="document",
        language="fr",
        status=status,
    )


def _age(row, seconds):
    """Vieillit ``updated_at`` — ``update()`` ne déclenche pas ``auto_now``."""
    past = timezone.now() - timezone.timedelta(seconds=seconds)
    DocumentSummary.objects.filter(id=row.id).update(updated_at=past)


# --------------------------------------------------------------------------- #
# 4.1 — reaper privé, sans délai explicite
# --------------------------------------------------------------------------- #
def test_private_reaper_without_explicit_timeout_uses_the_registry():
    """Le chemin de la production : aucun ``timeout_s`` transmis."""
    from apps.rag_core.services.summary_service import reap_stale_summaries

    doc, space = _doc()
    stale = _summary(doc, space, variant="a")
    fresh = _summary(doc, space, variant="b")
    ready = _summary(doc, space, status="READY", variant="c")
    _age(stale, 1200)  # au-delà des 900 s du registre
    _age(fresh, 60)

    reaped = reap_stale_summaries()  # <- SANS timeout_s

    assert reaped == 1
    stale.refresh_from_db()
    fresh.refresh_from_db()
    ready.refresh_from_db()
    assert stale.status == "FAILED"
    assert "stale_timeout" in stale.error_message
    # Un PENDING récent n'est PAS un PENDING bloqué.
    assert fresh.status == "PENDING"
    # Un résumé abouti n'est jamais touché.
    assert ready.status == "READY"


def test_private_reaper_without_timeout_returns_zero_when_nothing_is_stale():
    from apps.rag_core.services.summary_service import reap_stale_summaries

    doc, space = _doc()
    _age(_summary(doc, space, variant="fresh"), 60)

    assert reap_stale_summaries() == 0


@override_settings(SUMMARY_PENDING_TIMEOUT_S=60)
def test_the_configured_value_is_honoured_without_a_local_fallback():
    """Supprimer le repli local n'a rien coûté en configurabilité — au contraire.

    Un résumé vieux de 120 s n'était PAS bloqué sous le défaut de 900 s ; il l'est
    sous les 60 s configurés. C'est précisément ce qu'un repli local codé en dur
    aurait masqué si le registre avait été surchargé.
    """
    from apps.rag_core.services.summary_service import _pending_timeout_s, reap_stale_summaries

    assert _pending_timeout_s() == 60

    doc, space = _doc()
    row = _summary(doc, space, variant="cfg")
    _age(row, 120)

    assert reap_stale_summaries() == 1
    row.refresh_from_db()
    assert row.status == "FAILED"


# --------------------------------------------------------------------------- #
# 4.3 — la tâche Celery, sans argument
# --------------------------------------------------------------------------- #
def test_private_reaper_task_runs_with_no_argument():
    """La forme sous laquelle l'ordonnanceur l'appelle réellement."""
    from apps.rag_core.tasks import reap_stale_summaries_task

    doc, space = _doc()
    _age(_summary(doc, space, variant="task"), 1200)

    result = reap_stale_summaries_task()  # <- aucun argument

    assert isinstance(result, dict)
    assert result.get("reaped") == 1


def test_private_reaper_task_never_raises_a_type_error():
    """Garde explicite sur la panne exacte qui a duré 37 jours."""
    from apps.rag_core.tasks import reap_stale_summaries_task

    try:
        reap_stale_summaries_task()
    except TypeError as exc:  # pragma: no cover - c'est l'échec qu'on interdit
        pytest.fail(f"la tâche périodique lève encore un TypeError : {exc}")


# --------------------------------------------------------------------------- #
# 7 — la commande de gestion, SANS --timeout
# --------------------------------------------------------------------------- #
def test_management_command_without_timeout_option():
    """La forme documentée en premier dans le runbook d'exploitation.

    ``python manage.py reap_stale_summaries`` sans option levait le même
    ``TypeError`` : le repli de secours manuel était cassé lui aussi.
    """
    doc, space = _doc()
    _age(_summary(doc, space, variant="cmd"), 1200)

    call_command("reap_stale_summaries")  # <- sans --timeout

    assert DocumentSummary.objects.filter(status="FAILED").count() == 1


def test_management_command_with_explicit_timeout_still_works():
    """Le comportement historique reste valide : l'option n'est pas retirée."""
    doc, space = _doc()
    _age(_summary(doc, space, variant="cmd2"), 1200)

    call_command("reap_stale_summaries", timeout=900)

    assert DocumentSummary.objects.filter(status="FAILED").count() == 1


# --------------------------------------------------------------------------- #
# 6 — le contrat de configuration
# --------------------------------------------------------------------------- #
def test_the_registry_supplies_the_default():
    """Le défaut vient du REGISTRE, et il n'y en a qu'un."""
    from config.settings.access import get_registered_setting

    from apps.rag_core.services import summary_engine as engine

    assert get_registered_setting("SUMMARY_PENDING_TIMEOUT_S") == 900
    assert engine.setting_int("SUMMARY_PENDING_TIMEOUT_S") == 900


def test_setting_int_still_refuses_a_call_time_default():
    """Le contrat n'a PAS été assoupli — c'est le cœur de ce correctif.

    Rendre ``setting_int`` permissif aurait « réparé » l'incident en rouvrant le
    défaut qu'il avait été écrit pour fermer : un nom qui a l'air configurable et
    ne l'est pas.
    """
    from apps.rag_core.services import summary_engine as engine

    with pytest.raises(TypeError):
        engine.setting_int("SUMMARY_PENDING_TIMEOUT_S", 900)
