"""La mémoire de conversation ne doit plus évincer les sources en silence.

Le défaut : la mémoire était injectée dans la question AVANT l'allocation du budget.
Elle entrait donc dans `overhead` — la part réputée intouchable — et les sources
étaient la seule variable d'ajustement. Mesuré le 23/08/2026 sur 25 fils réels de
staging : mémoire médiane de 3 045 caractères, soit 3 sources perdues sur 8, et un fil
descendu à ZÉRO source. Aucune erreur, aucune trace : l'assistant répondait avec moins
de matière et personne ne pouvait le savoir.

L'inversion était mauvaise. Les sources sont la substance d'un assistant documentaire ;
la mémoire est une aide à la résolution de références. On peut répondre sans se
souvenir ; on ne peut pas prouver sans source.
"""
import pytest
from django.test import override_settings

from apps.rag_core.services.query.prompt_builder import (
    _build_prompt_with_budget,
    _truncate_conversation_context,
)


def _sources(n, taille=800):
    return [
        {"chunk_id": f"c{i}", "text": f"Source {i}. " + "x" * taille,
         "source": {"document_id": f"d{i}", "filename": f"f{i}.pdf"},
         "page_number": 1, "score": 0.9 - i * 0.01}
        for i in range(n)
    ]


def _construire(memoire, *, budget=8300, min_sources=4, n=8):
    return _build_prompt_with_budget(
        question_for_prompt="quels sont les types de fonds ?",
        selected=_sources(n), template_content=None,
        max_prompt_chars=budget, snippet_max_chars=1200,
        conversation_context=memoire, min_sources=min_sources,
    )


# --- Le défaut corrigé -----------------------------------------------------

@pytest.mark.parametrize("memoire_chars", [0, 1000, 3000, 5000, 7500])
def test_le_plancher_de_sources_tient_quelle_que_soit_la_memoire(memoire_chars):
    """La propriété centrale : N sources survivent, quoi qu'il arrive à la mémoire."""
    memoire = "Utilisateur : question. Assistant : réponse. " * 400
    _, retenues, info = _construire(memoire[:memoire_chars] if memoire_chars else "")
    assert len(retenues) >= 4, (
        f"avec {memoire_chars} c de mémoire, seules {len(retenues)} sources survivent — "
        "la mémoire évince encore les sources"
    )
    assert info["min_sources_reserved"] == 4


def test_une_memoire_enorme_est_tronquee_pas_les_sources():
    memoire = "Utilisateur : question longue. Assistant : réponse longue. " * 300
    _, retenues, info = _construire(memoire)
    assert info["conversation_context_truncated"] is True
    assert info["conversation_context_kept_chars"] < info["conversation_context_requested_chars"]
    assert len(retenues) >= 4


def test_une_memoire_qui_tient_n_est_pas_touchee():
    """Le correctif ne doit pas dégrader le cas nominal."""
    memoire = "Utilisateur : et pour X ? Assistant : voici."
    _, retenues, info = _construire(memoire)
    assert info["conversation_context_truncated"] is False
    assert info["conversation_context_kept_chars"] == len(memoire)
    assert memoire in _construire(memoire)[0]


def test_sans_plancher_le_comportement_anterieur_est_restaure():
    """`RAG_MIN_PROMPT_SOURCES=0` doit rendre exactement l'ancien arbitrage — c'est le
    retour arrière, et il doit être réel."""
    memoire = "Utilisateur : question. Assistant : réponse. " * 300
    _, avec, _ = _construire(memoire, min_sources=4)
    _, sans, info = _construire(memoire, min_sources=0)
    assert info["conversation_context_truncated"] is False
    assert len(sans) < len(avec), (
        "sans plancher, la mémoire devrait à nouveau évincer des sources"
    )


# --- La troncature garde le plus récent ------------------------------------

def test_la_troncature_garde_la_fin_de_la_memoire():
    """Couper le début est cohérent avec la politique de la mémoire elle-même ; couper
    la fin retirerait précisément ce à quoi la question fait référence."""
    cc = "ANCIEN " * 100 + "RECENT"
    texte, tronque = _truncate_conversation_context(cc, 40)
    assert tronque and texte.endswith("RECENT")
    assert texte.startswith("…")
    assert len(texte) <= 40


def test_une_allocation_nulle_supprime_la_memoire_entiere():
    texte, tronque = _truncate_conversation_context("quelque chose", 0)
    assert (texte, tronque) == ("", True)


def test_une_allocation_suffisante_ne_touche_a_rien():
    assert _truncate_conversation_context("court", 100) == ("court", False)


def test_pas_de_memoire_pas_de_troncature():
    assert _truncate_conversation_context("", 0) == ("", False)
    assert _truncate_conversation_context(None, 10) == ("", False)


# --- Ce que le correctif ne doit PAS casser --------------------------------

def test_l_invariant_d_au_moins_une_source_tient_encore():
    """Garanti avant ce lot : jamais un prompt sans source. Un budget minuscule tronque
    la dernière source plutôt que de la supprimer."""
    _, retenues, info = _construire("mémoire " * 200, budget=2000, min_sources=4, n=8)
    assert len(retenues) >= 1
    assert info["prompt_overhead_chars"] > 0


def test_la_comptabilite_rend_l_arbitrage_lisible():
    """Une éviction silencieuse est le mode de défaillance à empêcher : elle doit être
    lisible dans la comptabilité, pas seulement dans le résultat."""
    memoire = "Utilisateur : question. Assistant : réponse. " * 300
    _, _, info = _construire(memoire)
    for cle in ("conversation_context_requested_chars", "conversation_context_kept_chars",
                "conversation_context_truncated", "min_sources_reserved",
                "prompt_overhead_chars", "source_budget_chars", "sources_dropped_by_budget"):
        assert cle in info, f"« {cle} » absente de la comptabilité"


# --- L'éviction doit être VISIBLE en audit ---------------------------------

@pytest.mark.django_db
def test_l_eviction_par_le_budget_est_persistee_dans_l_audit():
    """Elle était mesurée depuis toujours et jetée à l'écriture du journal.

    Constaté le 23/08/2026 en lisant un journal réel : on y voyait « 1 source » sans
    pouvoir dire ce qui l'avait réduite à une. Une éviction indétectable après coup
    n'est pas diagnosticable — c'est le mode de défaillance que ce lot supprime.
    """
    from apps.rag_core.services.query.trace import RetrievalTraceBuilder

    t = RetrievalTraceBuilder(entrypoint="chat", request_id="test")
    avant = [{"chunk_id": f"c{i}", "source": {"source_type": "workspace_document"}}
             for i in range(6)]
    apres = avant[:2]
    t.record_prompt_budget(before=avant, after=apres, accounting={
        "prompt_overhead_chars": 5200,
        "source_budget_chars": 3100,
        "sources_dropped_by_budget": 4,
        "prompt_budget_exhausted": True,
        "conversation_context_requested_chars": 6686,
        "conversation_context_kept_chars": 2100,
        "conversation_context_truncated": True,
        "min_sources_reserved": 4,
    })
    resume = t.build(final_candidate_count=120, source_count=2).summary()

    pb = (resume.get("business_corpus") or {}).get("prompt_budget")
    assert pb, "la comptabilité du budget n'est pas persistée"
    assert pb["observed"] is True
    assert pb["sources_dropped_by_budget"] == 4
    assert pb["conversation_context_truncated"] is True
    assert pb["conversation_context_requested_chars"] == 6686
    assert pb["conversation_context_kept_chars"] == 2100
    assert pb["min_sources_reserved"] == 4
    # Aucun texte de conversation ne doit fuiter dans l'audit.
    import json
    assert "Utilisateur" not in json.dumps(pb)
