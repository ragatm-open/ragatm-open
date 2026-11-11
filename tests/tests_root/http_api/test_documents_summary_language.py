"""Langue des RÉSUMÉS de document — régression de l'incident « tout en français ».

Contexte : en production, un utilisateur ayant choisi l'anglais recevait malgré tout
des résumés français. Deux causes indépendantes se cumulaient :

1. le flag plateforme ``RAG_ANSWER_LANGUAGE_POLICY_ENABLED`` était absent de
   l'environnement déployé, donc ``resolve_target()`` renvoyait ``None`` ;
2. le frontend et les vues forçaient ``language="fr"``, ce qui transformait
   « langue non précisée » en CHOIX EXPLICITE du français et neutralisait ``auto``.

Ces tests verrouillent les deux : le comportement historique quand le flag est OFF
(compatibilité stricte) et la résolution par préférences de compte quand il est ON.

Le LLM est toujours stubé : aucun appel réseau.
"""
import pytest

import apps.rag_core.services.summary_service as svc
from apps.rag_core.models import Document
from apps.rag_core.services.ingest_facade import ingest_plain_text

pytestmark = pytest.mark.django_db


class _FakeLLM:
    """Stub capturant les prompts, pour inspecter la langue réellement demandée."""

    def __init__(self, text="STUB"):
        self.calls = []
        self._text = text

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "text": self._text,
            "tokens": 10,
            "cost": 0.0,
            "provider": "openai",
            "model": "gpt-stub",
            "usage": None,
            "raw": None,
        }

    @property
    def last_prompt(self) -> str:
        return self.calls[-1].get("prompt", "")


def _seed_ready_doc(rag_space, *, filename="art.txt"):
    ingest_plain_text(
        rag_space_id=rag_space.id, filename=filename, text="Paris est la capitale de la France."
    )
    return Document.objects.get(rag_space=rag_space, filename=filename)


def _set_prefs(user, *, answer_language=None, locale=None):
    from apps.accounts.models import Profile

    profile, _ = Profile.objects.get_or_create(user=user)
    if answer_language is not None:
        profile.preferred_answer_language = answer_language
    if locale is not None:
        profile.preferred_locale = locale
    profile.save()
    return profile


def _summary_row(doc):
    from apps.rag_core.models.summary import DocumentSummary

    return DocumentSummary.objects.get(document=doc)


# ---------------------------------------------------------------------------
# Flag OFF : comportement historique préservé À L'OCTET PRÈS
# ---------------------------------------------------------------------------
def test_policy_off_without_language_stays_french(api_client, rag_space, settings, monkeypatch):
    """Le défaut passé de `fr` à `auto` ne change RIEN tant que le flag est OFF."""
    settings.RAG_ANSWER_LANGUAGE_POLICY_ENABLED = False
    fake = _FakeLLM()
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = _seed_ready_doc(rag_space)

    res = api_client.post(f"/api/v1/documents/{doc.id}/summary/", {}, format="json")

    assert res.status_code == 200
    assert res.data["language"] == "fr"
    assert _summary_row(doc).variant_key.endswith(":fr")
    assert "Réponds en fr" in fake.last_prompt


def test_policy_off_ignores_explicit_english(api_client, user, rag_space, settings, monkeypatch):
    """Documente la CAUSE RACINE : flag OFF => une demande `en` explicite est ignorée."""
    settings.RAG_ANSWER_LANGUAGE_POLICY_ENABLED = False
    _set_prefs(user, answer_language="en", locale="en")
    fake = _FakeLLM()
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = _seed_ready_doc(rag_space)

    res = api_client.post(
        f"/api/v1/documents/{doc.id}/summary/", {"language": "en"}, format="json"
    )

    assert res.data["language"] == "fr"
    assert "Respond in English" not in fake.last_prompt


# ---------------------------------------------------------------------------
# Flag ON : la langue demandée est enfin respectée
# ---------------------------------------------------------------------------
def test_policy_on_explicit_english_produces_english_prompt(
    api_client, rag_space, settings, monkeypatch
):
    settings.RAG_ANSWER_LANGUAGE_POLICY_ENABLED = True
    fake = _FakeLLM()
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = _seed_ready_doc(rag_space)

    res = api_client.post(
        f"/api/v1/documents/{doc.id}/summary/", {"language": "en"}, format="json"
    )

    assert res.data["language"] == "en"
    assert _summary_row(doc).variant_key.endswith(":en")
    assert "Respond in English" in fake.last_prompt
    assert "Réponds en" not in fake.last_prompt


def test_policy_on_auto_follows_account_answer_preference(
    api_client, user, rag_space, settings, monkeypatch
):
    """`auto` sans question ni fil s'appuie sur `preferred_answer_language`."""
    settings.RAG_ANSWER_LANGUAGE_POLICY_ENABLED = True
    _set_prefs(user, answer_language="en", locale="fr")
    fake = _FakeLLM()
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = _seed_ready_doc(rag_space)

    res = api_client.post(f"/api/v1/documents/{doc.id}/summary/", {}, format="json")

    assert res.data["language"] == "en"


def test_policy_on_auto_falls_back_to_interface_locale(
    api_client, user, rag_space, settings, monkeypatch
):
    """Préférence de réponse `auto` : la locale d'interface est le DERNIER repli.

    Un résumé n'a ni question ni historique de fil ; sans ce repli, `auto` retomberait
    invariablement sur `fr` et un utilisateur anglophone n'obtiendrait jamais de
    résumé anglais.
    """
    settings.RAG_ANSWER_LANGUAGE_POLICY_ENABLED = True
    _set_prefs(user, answer_language="auto", locale="en")
    fake = _FakeLLM()
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = _seed_ready_doc(rag_space)

    res = api_client.post(f"/api/v1/documents/{doc.id}/summary/", {}, format="json")

    assert res.data["language"] == "en"


def test_policy_on_french_user_keeps_french(api_client, user, rag_space, settings, monkeypatch):
    """Non-régression pour les utilisateurs français : rien ne bouge."""
    settings.RAG_ANSWER_LANGUAGE_POLICY_ENABLED = True
    _set_prefs(user, answer_language="auto", locale="fr")
    fake = _FakeLLM()
    monkeypatch.setattr(svc, "generate_text", fake)
    doc = _seed_ready_doc(rag_space)

    res = api_client.post(f"/api/v1/documents/{doc.id}/summary/", {}, format="json")

    assert res.data["language"] == "fr"


def test_policy_on_explicit_french_beats_english_profile(
    api_client, user, rag_space, settings, monkeypatch
):
    """Une demande explicite prime sur les préférences du compte."""
    settings.RAG_ANSWER_LANGUAGE_POLICY_ENABLED = True
    _set_prefs(user, answer_language="en", locale="en")
    monkeypatch.setattr(svc, "generate_text", _FakeLLM())
    doc = _seed_ready_doc(rag_space)

    res = api_client.post(
        f"/api/v1/documents/{doc.id}/summary/", {"language": "fr"}, format="json"
    )

    assert res.data["language"] == "fr"


# ---------------------------------------------------------------------------
# Parité POST / polling — le cœur du risque de cache
# ---------------------------------------------------------------------------
def test_polling_without_language_targets_the_variant_created_by_post(
    api_client, user, rag_space, settings, monkeypatch
):
    """POST et GET résolvent `auto` IDENTIQUEMENT : le polling retrouve la variante.

    Si le GET retombait sur `fr` pendant que le POST a créé `…:en`, le polling
    interrogerait une clé inexistante et renverrait `none` en boucle.
    """
    settings.RAG_ANSWER_LANGUAGE_POLICY_ENABLED = True
    _set_prefs(user, answer_language="en", locale="en")
    monkeypatch.setattr(svc, "generate_text", _FakeLLM())
    doc = _seed_ready_doc(rag_space)

    post = api_client.post(f"/api/v1/documents/{doc.id}/summary/", {}, format="json")
    assert post.data["language"] == "en"

    get = api_client.get(f"/api/v1/documents/{doc.id}/summary/")

    assert get.status_code == 200
    assert get.data["status"] == "ready"
    assert get.data["language"] == "en"


def test_french_and_english_variants_coexist_without_eviction(
    api_client, rag_space, settings, monkeypatch
):
    """FR et EN sont deux lignes distinctes : aucune régénération n'est nécessaire."""
    settings.RAG_ANSWER_LANGUAGE_POLICY_ENABLED = True
    monkeypatch.setattr(svc, "generate_text", _FakeLLM())
    doc = _seed_ready_doc(rag_space)

    api_client.post(f"/api/v1/documents/{doc.id}/summary/", {"language": "fr"}, format="json")
    api_client.post(f"/api/v1/documents/{doc.id}/summary/", {"language": "en"}, format="json")

    from apps.rag_core.models.summary import DocumentSummary

    keys = set(
        DocumentSummary.objects.filter(document=doc).values_list("variant_key", flat=True)
    )
    assert keys == {"bullets:document::fr", "bullets:document::en"}


# ---------------------------------------------------------------------------
# Abstentions et bandeaux : textes NON générés, donc non soumis à la directive
# ---------------------------------------------------------------------------
def test_no_sources_answer_follows_resolved_language():
    """Régression trouvée en qualification staging : cible `en` correctement résolue
    et propagée, mais message d'abstention rendu en français sous une réponse anglaise.
    """
    from apps.chat.services.chat_service import _no_sources_answer

    assert _no_sources_answer("en") == "Not enough information in your documents."
    assert "Pas assez" in _no_sources_answer("fr")
    # `None` = politique désactivée → français historique, à l'octet près.
    assert "Pas assez" in _no_sources_answer(None)
    assert "Pas assez" in _no_sources_answer()


def test_detail_prefix_follows_resolved_language():
    from apps.chat.services.chat_service import _detail_prefix

    assert _detail_prefix("partial", "en").startswith("⚠️ The available documents")
    assert _detail_prefix("partial", "fr").startswith("⚠️ Les documents disponibles")
    assert _detail_prefix("partial", None).startswith("⚠️ Les documents disponibles")
    assert _detail_prefix("ok", "en") == ""


def test_partial_coverage_warning_is_localized():
    from apps.rag_core.domain.corpus_mode import (
        PARTIAL_COVERAGE_WARNING,
        PARTIAL_COVERAGE_WARNING_EN,
        localized_answer_prefix,
    )

    assert localized_answer_prefix(PARTIAL_COVERAGE_WARNING, "en") == PARTIAL_COVERAGE_WARNING_EN
    assert localized_answer_prefix(PARTIAL_COVERAGE_WARNING, "fr") == PARTIAL_COVERAGE_WARNING
    assert localized_answer_prefix(PARTIAL_COVERAGE_WARNING, None) == PARTIAL_COVERAGE_WARNING
    # Préfixe inconnu : ressort inchangé, jamais deviné.
    assert localized_answer_prefix("texte inconnu", "en") == "texte inconnu"
