from __future__ import annotations

from django.db import transaction
from django.db.models import Max

from apps.chat.models import PromptTemplate
from apps.workspaces.models import Workspace

# ---------------------------------------------------------------------
# Built-in prompt templates (minimal change):
# - Keep your existing DB versioning/activation helpers unchanged.
# - Provide ready-to-use default template contents that Chat/RAG can use
#   (either to seed DB, or as references in code/tests).
# ---------------------------------------------------------------------

DEFAULT_PROMPT_TEMPLATE_NAME = "default"
EXPLAIN_PROMPT_TEMPLATE_NAME = "explain"

DEFAULT_PROMPT_TEMPLATE_CONTENT = """\
Tu es un assistant RAG. Réponds en français.

Règles:
- Utilise en priorité les informations contenues dans les SOURCES.
- N'invente pas de faits spécifiques (dates, chiffres, noms) si ce n'est pas explicitement présent dans les SOURCES.
- Si l'information manque, dis-le clairement.

Format:
- Réponse claire et concise.
- Quand c'est utile, structure en puces.
- Si l'utilisateur demande explicitement un tableau, utilise un tableau Markdown GFM valide (ligne de séparation après l'en-tête, une ligne physique par ligne, caractères "|" échappés avec "\\|") et respecte le format demandé.
"""

EXPLAIN_PROMPT_TEMPLATE_CONTENT = """\
Tu es un assistant RAG. Réponds en français avec un niveau de détail élevé.

Règles:
- Base-toi d'abord sur les SOURCES.
- Tu peux ajouter du contexte général uniquement si cela n'introduit pas de faits spécifiques non présents dans les SOURCES.
- Si les SOURCES sont partielles, réponds quand même et indique explicitement ce qui manque.

Structure attendue:
1) Contexte / objectif
2) Points clés (liste)
3) Impacts / acteurs concernés
4) Zones d'incertitude / informations manquantes
5) Synthèse en 3 lignes

Style:
- Précis, pédagogique, et structuré.
- Évite le blabla. Va droit au point, mais développe suffisamment.
"""


@transaction.atomic
def create_prompt_template_version(*, workspace: Workspace, name: str, content: str, activate: bool = False) -> PromptTemplate:
    last_ver = (
        PromptTemplate.objects.filter(workspace=workspace, name=name).aggregate(m=Max("version")).get("m")
    ) or 0

    tpl = PromptTemplate.objects.create(
        workspace=workspace,
        name=name,
        version=last_ver + 1,
        content=content,
        is_active=False,
    )

    if activate:
        activate_prompt_template(prompt_template=tpl)

    return tpl


@transaction.atomic
def activate_prompt_template(*, prompt_template: PromptTemplate) -> PromptTemplate:
    PromptTemplate.objects.filter(
        workspace=prompt_template.workspace,
        name=prompt_template.name,
        is_active=True,
    ).exclude(id=prompt_template.id).update(is_active=False)

    PromptTemplate.objects.filter(id=prompt_template.id).update(is_active=True)
    prompt_template.refresh_from_db()
    return prompt_template


# ---------------------------------------------------------------------
# Optional helper to seed built-in templates (minimal, safe to ignore).
# You can call this from a management command or bootstrap step.
# ---------------------------------------------------------------------
@transaction.atomic
def ensure_builtin_templates(*, workspace: Workspace) -> dict[str, PromptTemplate]:
    """
    Ensure built-in templates exist for a workspace.

    - Creates a 'default' and 'explain' template if missing.
    - Does NOT auto-activate anything unless there is no active 'default'.

    Returns a dict: {"default": tpl, "explain": tpl}
    """
    out: dict[str, PromptTemplate] = {}

    def _ensure(name: str, content: str) -> PromptTemplate:
        existing = (
            PromptTemplate.objects.filter(workspace=workspace, name=name).order_by("-version").first()
        )
        if existing:
            return existing
        return create_prompt_template_version(workspace=workspace, name=name, content=content, activate=False)

    out["default"] = _ensure(DEFAULT_PROMPT_TEMPLATE_NAME, DEFAULT_PROMPT_TEMPLATE_CONTENT)
    out["explain"] = _ensure(EXPLAIN_PROMPT_TEMPLATE_NAME, EXPLAIN_PROMPT_TEMPLATE_CONTENT)

    # If no active "default" exists yet, activate the created/last default.
    has_active_default = PromptTemplate.objects.filter(
        workspace=workspace, name=DEFAULT_PROMPT_TEMPLATE_NAME, is_active=True
    ).exists()
    if not has_active_default:
        activate_prompt_template(prompt_template=out["default"])

    return out
