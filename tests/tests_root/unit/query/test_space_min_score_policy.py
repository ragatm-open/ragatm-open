"""P3-d — résolution du seuil de pertinence vectorielle PAR ESPACE.

Fonctions pures : ni base, ni réseau, ni LLM (un espace factice suffit ; le seul
test qui touche la base est celui qui vérifie l'absence de fuite, parce qu'il lui
faut un vrai `RagSpace` avec un identifiant et un nom réels à ne PAS retrouver).

Pourquoi ce module existe
-------------------------
`RAG_MIN_SCORE` est un seuil ABSOLU sur une similarité cosinus, mais l'échelle de
cette similarité dépend du modèle d'embedding ET de l'appariement linguistique
entre la question et le fonds. Mesuré sur un corpus bilingue (questions FR,
documents EN) : les documents PERTINENTS culminent à 0,739 et les questions HORS
SUJET à 0,687, alors que le défaut global vaut 0,76 — au-dessus des DEUX
populations. Le seuil ne discriminait donc plus rien : il coupait des documents
classés premiers par le leg vectoriel. Le calibrage appartient à l'espace, seul
porteur du couple (modèle d'embedding, langue du fonds).

L'invariant central testé ici est un invariant de PRUDENCE : le défaut global est
la valeur de repli universelle. Flag fermé, espace absent, valeur absente, valeur
aberrante, valeur non numérique — dans tous ces cas on retombe sur le défaut, et
jamais avec une exception. Un réglage mal saisi ne doit ni rendre un espace muet,
ni désactiver silencieusement le garde-fou.

Références d'implémentation :
- ``apps/rag_core/services/query/space_policy.py``
- ``apps/rag_core/models/rag_space.py`` (``RagSpace.min_score``)
- ``config/settings/base.py`` (``RAG_SPACE_MIN_SCORE_ENABLED``)
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from apps.rag_core.services.query.space_policy import (
    MIN_SCORE_CEILING,
    MIN_SCORE_FLOOR,
    min_score_audit,
    resolve_min_score,
    space_min_score_enabled,
)

GLOBAL_DEFAUT = 0.76


@pytest.fixture
def flag_ouvert(settings):
    """Flag ouvert + défaut global explicite, pour ne dépendre d'aucun `.env`."""
    settings.RAG_SPACE_MIN_SCORE_ENABLED = True
    settings.RAG_MIN_SCORE = GLOBAL_DEFAUT
    return settings


class _EspaceFactice:
    """Espace minimal : seul `min_score` est lu par la politique.

    On n'utilise pas un vrai `RagSpace` ici à dessein — la politique ne doit
    dépendre que de l'attribut, jamais du modèle ni de la base. Un objet nu
    permet en outre de porter des valeurs que le champ `FloatField` refuserait
    (une chaîne, un objet), ce qui est justement le cas à couvrir : la valeur
    peut arriver d'ailleurs (fixture, import, shell) et ne doit pas faire tomber
    la requête.
    """

    def __init__(self, min_score=None):
        self.min_score = min_score


# --------------------------------------------------------------------------- #
# 1) Le flag commande. Fermé, rien ne change — c'est la garantie de rollback.
# --------------------------------------------------------------------------- #
class TestFlag:
    @override_settings(RAG_SPACE_MIN_SCORE_ENABLED=False, RAG_MIN_SCORE=GLOBAL_DEFAUT)
    def test_flag_ferme_ignore_la_valeur_de_l_espace(self):
        """Flag fermé : la valeur portée par l'espace n'est même pas regardée.

        C'est le contrat de rollback : repasser le flag à 0 restaure exactement le
        comportement antérieur, sans avoir à nettoyer les valeurs déjà saisies en
        base. Une valeur oubliée sur un espace ne doit pas se réveiller toute seule.
        """
        assert space_min_score_enabled() is False
        assert resolve_min_score(_EspaceFactice(min_score=0.10)) == GLOBAL_DEFAUT

    @override_settings(RAG_SPACE_MIN_SCORE_ENABLED=True, RAG_MIN_SCORE=GLOBAL_DEFAUT)
    def test_flag_ouvert_la_valeur_de_l_espace_prime(self):
        assert space_min_score_enabled() is True
        assert resolve_min_score(_EspaceFactice(min_score=0.70)) == pytest.approx(0.70)

    def test_flag_absent_des_settings_vaut_ferme(self, settings):
        """L'absence de réglage est traitée comme un refus, pas comme un défaut ouvert.

        Un déploiement dont les settings n'ont pas encore la variable doit se
        comporter comme avant P3-d, pas basculer sur les valeurs d'espace.
        """
        settings.RAG_MIN_SCORE = GLOBAL_DEFAUT
        del settings.RAG_SPACE_MIN_SCORE_ENABLED

        assert space_min_score_enabled() is False
        assert resolve_min_score(_EspaceFactice(min_score=0.10)) == GLOBAL_DEFAUT


# --------------------------------------------------------------------------- #
# 2) Repli sur le défaut global — le comportement de sûreté.
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("flag_ouvert")
class TestRepliSurLeDefautGlobal:
    def test_espace_sans_valeur(self):
        """`NULL` en base = « je n'ai pas d'avis » = défaut global."""
        assert resolve_min_score(_EspaceFactice(min_score=None)) == GLOBAL_DEFAUT

    def test_espace_absent(self):
        """Aucun espace résolu : on applique quand même un seuil, jamais aucun."""
        assert resolve_min_score(None) == GLOBAL_DEFAUT

    def test_objet_sans_attribut_min_score(self):
        """Un objet d'un autre type ne fait pas tomber la résolution."""

        class SansChamp:
            pass

        assert resolve_min_score(SansChamp()) == GLOBAL_DEFAUT

    @pytest.mark.parametrize("valeur", [-0.1, -1.0, 1.5, 42.0, float("inf")])
    def test_valeur_hors_bornes(self, valeur):
        """Hors [0, 1] : la valeur est une faute de saisie, pas un réglage.

        Les deux sens comptent autant. Un négatif désactiverait le garde-fou (tout
        passe) ; un 1,5 rendrait l'espace définitivement muet (rien ne passe). Dans
        les deux cas on revient au défaut global, et **sans lever** : une saisie
        invalide dégrade la finesse du réglage, elle ne casse pas la recherche.
        """
        assert resolve_min_score(_EspaceFactice(min_score=valeur)) == GLOBAL_DEFAUT

    @pytest.mark.parametrize("valeur", ["abc", "", object(), [], {}, ("0.5",)])
    def test_valeur_non_numerique(self, valeur):
        """Non convertible en flottant : défaut global, toujours sans exception."""
        assert resolve_min_score(_EspaceFactice(min_score=valeur)) == GLOBAL_DEFAUT

    def test_nan_ne_passe_pas(self):
        """NaN échoue à toute comparaison : il doit être rejeté, pas propagé.

        Un NaN transmis au filtre de score rendrait chaque comparaison fausse et
        écarterait silencieusement tous les candidats.
        """
        assert resolve_min_score(_EspaceFactice(min_score=float("nan"))) == GLOBAL_DEFAUT

    def test_le_defaut_global_suit_les_settings(self):
        """Le repli n'est pas la constante 0,76 en dur : c'est bien le réglage courant."""
        with override_settings(RAG_MIN_SCORE=0.42):
            assert resolve_min_score(None) == pytest.approx(0.42)


# --------------------------------------------------------------------------- #
# 3) Bornes ACCEPTÉES — 0.0 et 1.0 sont dans l'intervalle, pas au bord dehors.
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("flag_ouvert")
class TestBornesIncluses:
    def test_zero_est_accepte(self):
        """0.0 = « aucun filtrage vectoriel » : réglage légitime, pas une absence.

        Le piège serait de traiter 0.0 comme une valeur fausse (`if not raw`) et de
        retomber sur 0,76 — c'est-à-dire de faire exactement l'inverse de ce que
        l'opérateur a demandé, en silence.
        """
        assert resolve_min_score(_EspaceFactice(min_score=0.0)) == 0.0

    def test_un_est_accepte(self):
        assert resolve_min_score(_EspaceFactice(min_score=1.0)) == 1.0

    def test_les_bornes_declarees_sont_bien_celles_appliquees(self):
        assert (MIN_SCORE_FLOOR, MIN_SCORE_CEILING) == (0.0, 1.0)
        assert resolve_min_score(_EspaceFactice(min_score=MIN_SCORE_FLOOR)) == MIN_SCORE_FLOOR
        assert resolve_min_score(_EspaceFactice(min_score=MIN_SCORE_CEILING)) == MIN_SCORE_CEILING

    def test_entier_et_chaine_numerique_sont_convertis(self):
        """`0` (int) et `"0.7"` (str) sont exploitables : on les accepte."""
        assert resolve_min_score(_EspaceFactice(min_score=0)) == 0.0
        assert resolve_min_score(_EspaceFactice(min_score="0.7")) == pytest.approx(0.7)

    def test_le_resultat_est_toujours_un_flottant(self):
        for valeur in (0, "0.7", 0.5, None, "abc"):
            assert isinstance(resolve_min_score(_EspaceFactice(min_score=valeur)), float)


# --------------------------------------------------------------------------- #
# 4) Observabilité — la provenance, et RIEN d'autre.
# --------------------------------------------------------------------------- #
class TestAuditProvenance:
    @override_settings(RAG_SPACE_MIN_SCORE_ENABLED=False, RAG_MIN_SCORE=GLOBAL_DEFAUT)
    def test_flag_ferme_provenance_globale(self):
        espace = _EspaceFactice(min_score=0.10)
        audit = min_score_audit(espace, resolve_min_score(espace))
        assert audit["min_score_source"] == "global_default"
        assert audit["min_score_applied"] == GLOBAL_DEFAUT
        assert audit["min_score_global_default"] == GLOBAL_DEFAUT

    @override_settings(RAG_SPACE_MIN_SCORE_ENABLED=True, RAG_MIN_SCORE=GLOBAL_DEFAUT)
    def test_flag_ouvert_valeur_portee_provenance_espace(self):
        espace = _EspaceFactice(min_score=0.70)
        audit = min_score_audit(espace, resolve_min_score(espace))
        assert audit["min_score_source"] == "space_override"
        assert audit["min_score_applied"] == pytest.approx(0.70)
        # Le défaut global reste exposé : sans lui, on ne peut pas relire un écart.
        assert audit["min_score_global_default"] == GLOBAL_DEFAUT

    @override_settings(RAG_SPACE_MIN_SCORE_ENABLED=True, RAG_MIN_SCORE=GLOBAL_DEFAUT)
    def test_flag_ouvert_sans_valeur_provenance_globale(self):
        audit = min_score_audit(_EspaceFactice(None), resolve_min_score(_EspaceFactice(None)))
        assert audit["min_score_source"] == "global_default"

    @override_settings(RAG_SPACE_MIN_SCORE_ENABLED=True, RAG_MIN_SCORE=GLOBAL_DEFAUT)
    def test_valeur_egale_au_defaut_est_distinguee(self):
        """Un espace qui pose explicitement 0,76 n'est pas « sans avis ».

        La distinction compte pour l'exploitation : elle dit qu'un opérateur a
        tranché, et qu'un futur changement du défaut global ne bougera pas cet espace.
        """
        espace = _EspaceFactice(min_score=GLOBAL_DEFAUT)
        audit = min_score_audit(espace, resolve_min_score(espace))
        assert audit["min_score_source"] == "space_override_equal_default"
        assert audit["min_score_applied"] == GLOBAL_DEFAUT

    @override_settings(RAG_SPACE_MIN_SCORE_ENABLED=True, RAG_MIN_SCORE=GLOBAL_DEFAUT)
    def test_clefs_exactes(self):
        """Le contrat de l'audit est fermé : trois clés, pas une de plus."""
        audit = min_score_audit(_EspaceFactice(0.70), 0.70)
        assert set(audit) == {
            "min_score_source",
            "min_score_applied",
            "min_score_global_default",
        }

    @override_settings(RAG_SPACE_MIN_SCORE_ENABLED=True, RAG_MIN_SCORE=GLOBAL_DEFAUT)
    @pytest.mark.parametrize("valeur", [-0.1, 1.5, "abc", float("nan")])
    def test_audit_ne_leve_jamais_sur_valeur_invalide(self, valeur):
        espace = _EspaceFactice(min_score=valeur)
        audit = min_score_audit(espace, resolve_min_score(espace))
        assert audit["min_score_applied"] == GLOBAL_DEFAUT

    @override_settings(RAG_SPACE_MIN_SCORE_ENABLED=True, RAG_MIN_SCORE=GLOBAL_DEFAUT)
    @pytest.mark.parametrize("valeur", [1.5, -0.1, "abc", float("nan")])
    def test_valeur_rejetee_est_signalee_comme_telle(self, valeur):
        """Une saisie écartée mérite mieux que `global_default`.

        L'exploitant qui a saisi une valeur hors bornes doit pouvoir le LIRE : sinon
        son espace se comporte comme s'il n'avait rien réglé, sans qu'il sache
        pourquoi. `space_value_rejected` distingue donc « aucun avis » de « avis
        invalide » — deux situations qui appellent des actions opposées.
        """
        espace = _EspaceFactice(min_score=valeur)
        audit = min_score_audit(espace, resolve_min_score(espace))
        assert audit["min_score_source"] == "space_value_rejected"
        # Le comportement, lui, reste le repli sûr sur le défaut global.
        assert audit["min_score_applied"] == GLOBAL_DEFAUT


# --------------------------------------------------------------------------- #
# 5) FUITE — l'audit décrit une CONFIGURATION, jamais une donnée.
# --------------------------------------------------------------------------- #
@pytest.mark.django_db
@pytest.mark.usefixtures("flag_ouvert")
class TestAucuneFuiteDansLAudit:
    """Ici on veut un VRAI `RagSpace` : il faut un identifiant et un nom réels
    pour prouver qu'on ne les retrouve pas dans l'audit. Un objet factice
    rendrait le test vide de sens.
    """

    @override_settings(RAG_SPACE_MIN_SCORE_ENABLED=True, RAG_MIN_SCORE=GLOBAL_DEFAUT)
    def test_ni_identifiant_ni_nom_dans_l_audit(self, workspace):
        from apps.rag_core.models import RagSpace

        espace = RagSpace.objects.create(
            name="dossier-confidentiel-TOURMALINE",
            workspace=workspace,
            min_score=0.70,
        )
        audit = min_score_audit(espace, resolve_min_score(espace))

        serialise = repr(audit)
        assert str(espace.id) not in serialise
        assert espace.name not in serialise
        assert "TOURMALINE" not in serialise
        assert str(workspace.id) not in serialise

        # Aucune valeur de l'audit n'est une chaîne libre : ce sont des étiquettes
        # closes et des nombres. Un audit ne doit pas pouvoir transporter de texte.
        assert audit["min_score_source"] in {
            "global_default",
            "space_override",
            "space_override_equal_default",
        }
        assert isinstance(audit["min_score_applied"], float)
        assert isinstance(audit["min_score_global_default"], float)

    @override_settings(RAG_SPACE_MIN_SCORE_ENABLED=True, RAG_MIN_SCORE=GLOBAL_DEFAUT)
    def test_l_audit_ne_depend_pas_de_la_question(self, workspace):
        """La signature ne prend pas la question — et ne doit jamais la prendre.

        Contrôle de contrat : si un jour l'audit acceptait la question, il
        deviendrait un canal de fuite de données utilisateur vers les journaux.
        """
        import inspect

        parametres = set(inspect.signature(min_score_audit).parameters)
        assert parametres == {"rag_space", "resolved"}
