"""Le plafond de découpage appartient au MODÈLE, pas au transport.

Matrice de référence — ce que `resolve_chunk_budget` doit rendre. Elle est explicite
plutôt que dérivée : une matrice se relit dans une diff, une dérivation se discute.

    provider      modèle                    plafond réel   budget attendu
    ─────────────────────────────────────────────────────────────────────
    ollama        nomic-embed-text          n/a            (2500, 250)  inchangé
    openai        text-embedding-3-small    n/a            (2500, 250)  inchangé
    ai_gateway    legacy-768                6000 car.      (2500, 250)  rien à écrêter
    ai_gateway    fr-en-retrieval           1200 car.      (1200, 180)  écrêté
    ai_gateway    <inconnu>                 ?              (1200, 180)  repli prudent
    ai_gateway    <vide>                    ?              (1200, 180)  repli prudent
    (aucun espace)                          n/a            (2500, 250)  inchangé

Les plafonds ont été VÉRIFIÉS contre les deux Gateway le 22/08/2026, en caractères et
non en tokens — le refus est littéral : « texte 0 trop long (1201 caractères > 1200) ».
`manage.py check_embedding_capabilities` rejoue cette vérification.
"""
import pytest
from django.test import override_settings

from apps.llm_router.capabilities import (
    GOVERNED_MODEL_MAX_INPUT_CHARS,
    governed_model_max_input_chars,
)
from apps.rag_core.services.ingest.utils import resolve_chunk_budget

STANDARD = (2500, 250)
ECRETE = (1200, 180)


class _Espace:
    def __init__(self, provider, model):
        self.embedding_provider = provider
        self.embedding_model = model


BUDGETS = pytest.mark.parametrize(
    ("provider", "modele", "attendu"),
    [
        ("ollama", "nomic-embed-text", STANDARD),
        ("openai", "text-embedding-3-small", STANDARD),
        ("ai_gateway", "legacy-768", STANDARD),
        ("ai_gateway", "fr-en-retrieval", ECRETE),
        ("ai_gateway", "modele-jamais-declare", ECRETE),
        ("ai_gateway", "", ECRETE),
    ],
)


@override_settings(
    RAG_CHUNK_SIZE_CHARS=2500, RAG_CHUNK_OVERLAP_CHARS=250,
    RAG_GOVERNED_CHUNK_SIZE_CHARS=1200, RAG_GOVERNED_CHUNK_OVERLAP_CHARS=180,
)
@BUDGETS
def test_matrice_du_budget_de_decoupage(provider, modele, attendu):
    assert resolve_chunk_budget(_Espace(provider, modele)) == attendu


@override_settings(RAG_CHUNK_SIZE_CHARS=2500, RAG_CHUNK_OVERLAP_CHARS=250)
def test_aucun_espace_donne_le_comportement_historique():
    assert resolve_chunk_budget(None) == STANDARD


# --- La régression que ce lot corrige --------------------------------------

@override_settings(
    RAG_CHUNK_SIZE_CHARS=2500, RAG_CHUNK_OVERLAP_CHARS=250,
    RAG_GOVERNED_CHUNK_SIZE_CHARS=1200, RAG_GOVERNED_CHUNK_OVERLAP_CHARS=180,
)
def test_gouverner_un_espace_ne_redecoupe_pas_son_corpus_sans_raison():
    """Le cœur du lot, dit sous la forme qui compte pour l'exploitation.

    Migrer `ollama`/`nomic-embed-text` vers `ai_gateway`/`legacy-768` change la
    PROVENANCE des vecteurs — mêmes poids, identité désormais vérifiée. Cela ne doit
    pas changer la STRUCTURE du corpus : mêmes frontières de fragments, donc un corpus
    qui reste comparable à celui d'avant la migration.

    Tant que le budget suivait le provider, cette migration imposait un redécoupage de
    2 500 vers 1 200, soit ×2,3 fragments — mesuré sur l'espace de référence : 89,3
    fragments par document contre 38,9.
    """
    avant = resolve_chunk_budget(_Espace("ollama", "nomic-embed-text"))
    apres = resolve_chunk_budget(_Espace("ai_gateway", "legacy-768"))
    assert avant == apres, (
        "la migration vers le pipeline gouverné redécoupe le corpus alors que le "
        "modèle cible accepte la granularité actuelle"
    )


@override_settings(
    RAG_CHUNK_SIZE_CHARS=2500, RAG_CHUNK_OVERLAP_CHARS=250,
    RAG_GOVERNED_CHUNK_SIZE_CHARS=1200, RAG_GOVERNED_CHUNK_OVERLAP_CHARS=180,
)
def test_le_modele_contraint_reste_ecrete():
    """Le revers : `fr-en-retrieval` DOIT rester borné. Sa limite est dure — au-delà,
    la Gateway refuse le texte et l'indexation du document échoue."""
    assert resolve_chunk_budget(_Espace("ai_gateway", "fr-en-retrieval")) == ECRETE


# --- Politique du modèle inconnu -------------------------------------------

@override_settings(
    RAG_CHUNK_SIZE_CHARS=2500, RAG_GOVERNED_CHUNK_SIZE_CHARS=1200,
    RAG_GOVERNED_CHUNK_OVERLAP_CHARS=180,
)
def test_un_modele_inconnu_ne_recoit_jamais_la_granularite_produit(caplog):
    """« Inconnu » n'est pas « sans limite », et le repli n'est pas silencieux."""
    import logging

    with caplog.at_level(logging.WARNING):
        budget = resolve_chunk_budget(_Espace("ai_gateway", "un-modele-futur"))

    assert budget == ECRETE
    assert any("inconnu de la table de capacités" in r.message for r in caplog.records), (
        "un repli conservateur non journalisé fait passer une lacune de déclaration "
        "pour une décision"
    )


def test_la_table_ne_rend_jamais_une_limite_pour_un_modele_inconnu():
    assert governed_model_max_input_chars("modele-jamais-declare") is None
    assert governed_model_max_input_chars("") is None
    assert governed_model_max_input_chars("  fr-en-retrieval  ") == 1200


def test_le_plafond_utile_deduit_le_prefixe_interne_de_la_gateway():
    """6 000 est ce que la Gateway PUBLIE ; 5 983 est ce qu'un texte peut faire.

    La Gateway préfixe le texte (« search_document: », 17 caractères) AVANT de compter.
    Mesuré par dichotomie contre la Gateway de production le 23/08/2026 :

        task=document  dernier accepté 5983, refus à 5984
        task=query     dernier accepté 5986, refus à 5987

    La distinction n'est pas cosmétique : c'est ce plafond-là qui borne le découpage.
    Rendre 6 000 laissait passer un fragment de 5 990 par toutes nos gardes, pour qu'il
    soit refusé par la Gateway — donc un document en échec, des semaines plus tard.
    """
    from apps.llm_router.capabilities import governed_model_published_max_input_chars

    assert governed_model_published_max_input_chars("legacy-768") == 6000
    assert governed_model_max_input_chars("legacy-768", task="document") == 5983
    assert governed_model_max_input_chars("legacy-768", task="query") == 5986


def test_sans_tache_connue_le_plafond_utile_retient_le_prefixe_le_plus_couteux():
    """Se tromper vers le bas coûte 3 caractères ; vers le haut, un document entier."""
    assert governed_model_max_input_chars("legacy-768") == 5983
    assert governed_model_max_input_chars("legacy-768", task="tache-inventee") == 5983


def test_le_decoupage_reste_sous_le_plafond_reellement_accepte():
    """Le test de bout en bout de P1 : ce que `resolve_chunk_budget` produit doit être
    acceptable par la Gateway, pas seulement conforme à notre table."""
    from apps.llm_router.capabilities import governed_model_published_max_input_chars

    taille, _ = resolve_chunk_budget(_Espace("ai_gateway", "legacy-768"))
    accepte = 5983  # mesuré, cf. ci-dessus
    assert taille <= accepte
    # Et l'écart avec le publié est bien du préfixe, pas une marge arbitraire.
    assert governed_model_published_max_input_chars("legacy-768") - accepte == 17


def test_la_table_est_exprimee_en_caracteres_et_couvre_les_modeles_gouvernes():
    """Garde d'unité et de couverture.

    Les plafonds sont des CARACTÈRES : la Gateway compte des caractères, RagATM
    découpe en caractères. Si une valeur descendait sous 512, ce serait le signe qu'un
    nombre de TOKENS s'est glissé dans la table — la fenêtre de `fr-en-retrieval` fait
    512 tokens, et les confondre diviserait le découpage par deux ou trois.
    """
    assert set(GOVERNED_MODEL_MAX_INPUT_CHARS) >= {"legacy-768", "fr-en-retrieval"}
    for nom, plafond in GOVERNED_MODEL_MAX_INPUT_CHARS.items():
        assert isinstance(plafond, int) and plafond >= 512, (
            f"{nom}: plafond {plafond} suspect — des tokens auraient-ils été déclarés "
            "à la place de caractères ?"
        )


# --- Aucun espace persisté n'est redécoupé rétroactivement -----------------

def test_le_budget_n_est_lu_qu_aux_chemins_d_indexation():
    """§26 — un déploiement ne doit pas redécouper des fragments déjà persistés.

    La garantie n'est pas une intention : elle tient au fait que `resolve_chunk_budget`
    n'est appelée que là où des fragments sont CRÉÉS. Ce test lit le dépôt plutôt que
    d'énumérer une liste, parce qu'une liste se périme.
    """
    import pathlib

    racine = pathlib.Path(__file__).resolve().parents[3] / "backend" / "apps"
    appelants = sorted(
        str(f.relative_to(racine))
        for f in racine.rglob("*.py")
        if "resolve_chunk_budget(" in f.read_text(encoding="utf-8")
        and "def resolve_chunk_budget" not in f.read_text(encoding="utf-8")
    )
    assert appelants == [
        "rag_core/services/ingest/indexing.py",
        "rag_core/services/ingest/pipeline.py",
    ], (
        "un nouvel appelant est apparu hors des chemins d'indexation : vérifier qu'il "
        "ne peut pas modifier la granularité de fragments déjà écrits.\n"
        f"appelants : {appelants}"
    )
