"""Le chat parle à la façade Core, jamais à l'implémentation du retrieval.

Pourquoi un test de frontière plutôt qu'une convention écrite : l'appel direct à
``query_rag`` a été le chemin normal du chat pendant des mois. Il est plus court à
écrire, il fonctionne, et rien dans le code ne le refuse. Une frontière qu'aucun
test ne garde se refranchit sans que personne ne le décide — c'est exactement
ainsi que la bifurcation supprimée par ce lot était née.

Ce que le test autorise, il l'autorise NOMMÉMENT (``ALLOWLIST``), avec la raison
et la dette associée. Un module qui n'y figure pas et qui importe ``query_rag``
fait échouer la suite avec l'instruction à suivre.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[3]
BACKEND = REPO / "backend"

#: Modules encore autorisés à appeler l'implémentation directement, et pourquoi.
#: Toute entrée est une DETTE : elle décrit un appelant qui devrait passer par la
#: façade et ne le peut pas encore.
ALLOWLIST: dict[str, str] = {
    # La façade EST l'appelant légitime : c'est elle qui délègue.
    "apps/rag_core/core/facade.py": "la façade elle-même",
    # Ré-export du paquet : ce n'est pas un appelant, c'est la définition du module
    # où `query_rag` vit. L'exclure du test reviendrait à ne plus pouvoir l'importer
    # nulle part, y compris depuis la façade.
    "apps/rag_core/services/query/__init__.py": "ré-export du module d'origine",
    # Contrat historique exposé aux vues HTTP ; migrera avec le DTO.
    "apps/rag_core/services/query_facade.py": "façade HTTP historique",
    # Le replay d'incident a besoin de `trace_sink`, que le DTO n'expose pas encore.
    "apps/feedback/tasks.py": "replay d'incident : nécessite trace_sink",
    "apps/feedback/management/commands/replay_rag_incident.py": (
        "replay d'incident : nécessite trace_sink"
    ),
    # Bancs de mesure : ils appellent le pipeline de production À DESSEIN, et leur
    # comparabilité historique interdit d'en changer le point d'entrée à la légère.
    "apps/rag_core/tasks_benchmark.py": "banc de mesure",
    "apps/rag_core/benchmark/bi_source.py": "banc de mesure",
    "apps/rag_core/benchmark/bi_source_weights.py": "banc de mesure",
    # Curation : ce script appelle le pipeline de production À DESSEIN,
    # pour ÉTABLIR des labels de vérité terrain contre le retrieval réel, et il
    # lit `retrieval_trace.candidate_trace` — le pool de candidats.
    #
    # La façade rendrait exactement la même chose (`core.retrieve(...).raw` est le
    # payload verbatim de `query_rag`, et `entrypoint` n'est qu'une étiquette
    # d'observabilité — aucun code ne branche dessus). Le fichier n'est pourtant
    # PAS réécrit : il est SCELLÉ par sha256 dans `artifact-registry.json`
    # (catégorie `I_SOURCE_CODE`) et dans `checksums.sha256`. Le modifier casserait
    # le registre, que la campagne désigne comme sa seule source de vérité.
    #
    # Dette BORNÉE : la campagne est suspendue, et sa reprise est déclarée
    # `STRATEGY / EVALUATION ONLY` — l'outillage sera reconçu, pas rafistolé. Cette
    # entrée disparaît avec lui. Cf. docs/08-testing/lot12-suffisance-des-preuves.md.
}

MESSAGE = (
    "{module} importe `query_rag` directement.\n"
    "Le point d'entrée du retrieval est `apps.rag_core.core.retrieve` :\n"
    "    from apps.rag_core import core\n"
    "    payload = core.retrieve(...).raw\n"
    "Si ce module a un besoin que la façade ne couvre pas (par exemple `trace_sink`), "
    "ajoutez-le à ALLOWLIST dans ce fichier AVEC la raison — une dette nommée se "
    "rembourse, une dette tacite se répand."
)


def _python_files() -> list[pathlib.Path]:
    return [
        p
        for p in BACKEND.rglob("*.py")
        if "__pycache__" not in p.parts and "migrations" not in p.parts
    ]


def _imports_query_rag(path: pathlib.Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - fichier non Python valide
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "query_rag" for alias in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(alias.name.endswith("query_rag") for alias in node.names):
                return True
    return False


def test_no_module_outside_the_allowlist_imports_the_implementation():
    offenders = []
    for path in _python_files():
        rel = path.relative_to(BACKEND).as_posix()
        if rel in ALLOWLIST:
            continue
        if _imports_query_rag(path):
            offenders.append(rel)
    assert not offenders, "\n\n".join(MESSAGE.format(module=m) for m in offenders)


@pytest.mark.parametrize("module", sorted(ALLOWLIST))
def test_allowlisted_modules_still_exist(module):
    """Une allowlist qui pointe dans le vide masque une frontière déjà franchie."""
    assert (BACKEND / module).is_file(), (
        f"{module} est dans l'allowlist mais n'existe plus : retirez l'entrée, "
        "sinon elle finira par couvrir un module homonyme qu'on n'a pas examiné"
    )


def test_chat_services_never_import_the_implementation():
    """Garde spécifique au chat — la frontière que ce lot vient de rétablir."""
    chat = BACKEND / "apps" / "chat"
    offenders = [
        p.relative_to(BACKEND).as_posix()
        for p in chat.rglob("*.py")
        if "__pycache__" not in p.parts and _imports_query_rag(p)
    ]
    assert not offenders, "\n\n".join(MESSAGE.format(module=m) for m in offenders)
