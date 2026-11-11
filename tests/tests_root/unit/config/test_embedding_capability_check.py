"""La déclaration de capacités doit pouvoir être confrontée à la réalité.

`GOVERNED_MODEL_MAX_INPUT_CHARS` vit dans le code — l'ingestion ne doit pas dépendre
d'un appel réseau pour savoir comment découper. Le prix de ce choix est le risque de
dérive : la Gateway peut changer un plafond sans que RagATM l'apprenne, et le
découpage reposerait alors sur une valeur fausse. La panne serait silencieuse et
différée — le découpage passe, l'indexation échoue des semaines plus tard.

Ces tests vérifient que le pont existe et qu'il refuse de mentir.
"""
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

CMD = "check_embedding_capabilities"


def _sante(models):
    return {"environment": "test", "models": models}


def _modele(nom, plafond, ici=True):
    return {"model_logical": nom, "max_input_chars": plafond, "dimension": 768,
            "allowed_here": ici, "kind": "embedding"}


def test_conforme_quand_la_gateway_confirme_les_plafonds():
    from apps.llm_router.capabilities import GOVERNED_MODEL_MAX_INPUT_CHARS

    publies = [_modele(n, p) for n, p in GOVERNED_MODEL_MAX_INPUT_CHARS.items()]
    out = StringIO()
    with patch("apps.rag_core.management.commands.check_embedding_capabilities._fetch",
               return_value=_sante(publies)):
        call_command(CMD, "--base-url", "http://gw.test:8100", stdout=out)
    assert "conforme" in out.getvalue()


def test_echoue_quand_un_plafond_a_derive():
    """Le cas qui justifie la commande : la Gateway a bougé, pas le code."""
    from apps.llm_router.capabilities import GOVERNED_MODEL_MAX_INPUT_CHARS

    publies = [_modele(n, p) for n, p in GOVERNED_MODEL_MAX_INPUT_CHARS.items()]
    publies[0] = {**publies[0], "max_input_chars": publies[0]["max_input_chars"] // 2}

    with patch("apps.rag_core.management.commands.check_embedding_capabilities._fetch",
               return_value=_sante(publies)):
        with pytest.raises(CommandError, match="écart"):
            call_command(CMD, "--base-url", "http://gw.test:8100", stdout=StringIO())


def test_echoue_quand_un_modele_declare_est_absent_du_registre():
    with patch("apps.rag_core.management.commands.check_embedding_capabilities._fetch",
               return_value=_sante([_modele("legacy-768", 6000)])):
        with pytest.raises(CommandError, match="écart"):
            call_command(CMD, "--base-url", "http://gw.test:8100", stdout=StringIO())


def test_signale_un_modele_servi_mais_non_declare():
    """L'inverse compte aussi : un modèle gouverné inconnu de RagATM retombe
    SILENCIEUSEMENT sur la borne conservatrice. Le signaler ne bloque pas, mais le
    taire laisserait un espace mal découpé sans que personne ne l'apprenne."""
    from apps.llm_router.capabilities import GOVERNED_MODEL_MAX_INPUT_CHARS

    publies = [_modele(n, p) for n, p in GOVERNED_MODEL_MAX_INPUT_CHARS.items()]
    publies.append(_modele("un-modele-futur", 4096, ici=True))

    out = StringIO()
    with patch("apps.rag_core.management.commands.check_embedding_capabilities._fetch",
               return_value=_sante(publies)):
        call_command(CMD, "--base-url", "http://gw.test:8100", stdout=out)
    texte = out.getvalue()
    assert "un-modele-futur" in texte
    assert "NON déclarés" in texte
    assert "conforme" in texte, "un modèle non déclaré informe, il ne doit pas bloquer"


def test_refuse_de_conclure_sans_gateway():
    """Une commande de comparaison sans terme de comparaison ne prouve rien."""
    with patch("apps.llm_router.embeddings_gateway.gateway_embeddings_base_url",
               return_value=""):
        with pytest.raises(CommandError, match="aucune Gateway"):
            call_command(CMD, stdout=StringIO())
