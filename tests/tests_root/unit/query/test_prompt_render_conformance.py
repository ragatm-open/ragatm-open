"""Conformité du RENDU du prompt — flag `RAG_PROMPT_RENDER_CONFORMANCE_V1_ENABLED`.

Deux défauts objectifs, mesurés sur les 105 prompts DEV de référence (lots
PROMPT-BUDGET-CONFORMANCE-1A et EXCEL-RULES-BUDGET-1A) :

- C1 — le marqueur « … (extrait tronqué …) » d'un tableau était ajouté APRÈS avoir
  consommé la place allouée : un prompt de 8 350 caractères pour un plafond de 8 300,
  sans aucun signalement (`prompt_budget_unable_to_fit_minimum_source` restait faux) ;
- C2 — `chunk_repository._build_label` compose déjà « fichier (date) p.N », et le rendu
  ajoutait à nouveau « (date) p.N » : 786 en-têtes sur 922 répétaient date et page.

Et une amélioration d'observabilité (V4) : le coût des règles conditionnelles
(CLASSEURS / TABLEAUX) est compté dans le prompt mais invisible dans
`prompt_overhead_chars` ; il est désormais exposé, mesuré depuis le texte rendu.

Le flag est livré OFF : le rendu historique est reproduit à l'octet près (vérifié ici
sur fixtures, et sur les 105 prompts de référence dans le lot). Rien d'autre ne change :
ni règles, ni politique du plus long préfixe, ni protection de dernière source, ni budgets.
"""
import hashlib
import os
import time

import pytest
from django.conf import settings
from django.test import override_settings

from apps.rag_core.domain import retrieval as R
from apps.rag_core.services.query import prompt_builder as pb
from apps.rag_core.services.query.contracts import (
    PreparedQuery,
    QueryExecutionContext,
    QueryRequest,
)
from apps.rag_core.services.query.trace import RetrievalTraceBuilder

MARKER = pb.TABLE_TRUNCATION_MARKER
FLAG = "RAG_PROMPT_RENDER_CONFORMANCE_V1_ENABLED"
ON = {FLAG: True}
OFF = {FLAG: False}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def _table_text(rows=60, width=70):
    """Tableau Markdown : chaque ligne porte un nombre complet, une unité, un accent.

    Les lignes ont des longueurs VARIABLES (cellule élargie), pour que les frontières de
    ligne ne tombent jamais sur un multiple commode du plafond.
    """
    head = "| Désignation | Quantité | Unité | Prix unitaire (€) | Total (€) |"
    lines = [head, "|---|---|---|---|---|"]
    for i in range(rows):
        lines.append(f"| Élément n°{i:03d}{' ' * (i % 7)} | {1000 + i} | ml | 1 234,56 | 12 345,67 |")
    return "\n".join(lines)


def _excel(text=None, *, label="dpgf.xlsx", date=None, page=None):
    src = {
        "filename": "dpgf.xlsx", "label": label,
        "meta": {"kind": "excel_table", "sheet_name": "Synthèse", "cell_range": "B8:F17"},
    }
    if date:
        src["document_date"] = date
    if page is not None:
        src["page_number"] = page
    return {"chunk_id": "x1", "text": text if text is not None else _table_text(),
            "source": src, "page_number": page}


def _pdf(i=0, *, label=None, date="2024-03-12", page=7, text=None, filename=None):
    src = {"filename": filename or f"rapport{i}.pdf", "meta": {}}
    if label is not None:
        src["label"] = label
    if date:
        src["document_date"] = date
    if page is not None:
        src["page_number"] = page
    return {"chunk_id": f"p{i}", "text": text or ("Un paragraphe de prose numéro %d. " % i) * 30,
            "source": src, "page_number": page}


def _header(prompt, i=1):
    for line in prompt.splitlines():
        if line.startswith(f"[{i}] "):
            return line
    raise AssertionError(f"en-tête [{i}] absent")


def _legacy_truncate_table_text(s, max_chars):
    """Copie VERBATIM de l'implémentation historique (8da3eaa) : l'oracle du mode OFF."""
    t = ("" if s is None else str(s)).replace(" ", " ").strip()
    if max_chars <= 0 or len(t) <= max_chars:
        return t
    kept, used = [], 0
    for line in t.splitlines():
        if used + len(line) + 1 > max_chars:
            break
        kept.append(line)
        used += len(line) + 1
    kept.append("… (extrait tronqué : les lignes suivantes du tableau ne sont pas affichées)")
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# Flag : déclaré, OFF par défaut, sans effet hors périmètre
# ---------------------------------------------------------------------------
def test_flag_declare_et_off_par_defaut():
    assert hasattr(settings, FLAG)
    if os.getenv(FLAG):
        pytest.skip("valeur surchargée par l'environnement local")
    assert settings.RAG_PROMPT_RENDER_CONFORMANCE_V1_ENABLED is False
    assert pb._render_conformance_enabled() is False


def test_flag_au_registre_bool_defaut_false():
    from config.registry.domains.retrieval import DEFINITIONS

    d = {x.name: x for x in DEFINITIONS}[FLAG]
    assert d.value_type == "bool" and d.default is False
    assert "apps.rag_core.services.query.prompt_builder" in d.consumers


@override_settings(**ON)
def test_flag_on_active_le_chemin():
    assert pb._render_conformance_enabled() is True


@override_settings(**ON)
def test_flag_ne_touche_pas_les_extraits_textuels():
    """Hors tableau, `count_table_marker` n'a AUCUN effet : même texte, même coupe."""
    sel = [_pdf(0, text="Prose. " * 500)]
    a = pb._prepare_selected_for_prompt(sel, snippet_max_chars=800)
    b = pb._prepare_selected_for_prompt(sel, snippet_max_chars=800, count_table_marker=True)
    assert a == b and len(a[0]["text"]) <= 800


# ---------------------------------------------------------------------------
# Règles conditionnelles : INCHANGÉES (empreintes gelées) et activation inchangée
# ---------------------------------------------------------------------------
def test_regles_classeurs_et_tableaux_inchangees():
    """Empreintes mesurées sur 8da3eaa (lot EXCEL-RULES-BUDGET-1A, registre V0)."""
    assert len(R.EXCEL_RULES) == 2812
    assert hashlib.sha256(R.EXCEL_RULES.encode()).hexdigest() == (
        "9efb3825814f8114c87819f65a9f20b88ca53fbc716c8d2f15943226cf9f5263"
    )
    assert len(R.TABLE_RULES) == 435
    assert hashlib.sha256(R.TABLE_RULES.encode()).hexdigest() == (
        "5d57691c071bfe33712c1ab187016df6ff0c312b872d3e23a829a9afb35bf552"
    )


@pytest.mark.parametrize("dedup", [False, True])
def test_activation_des_regles_identique_sous_les_deux_modes(dedup):
    p_excel = R.build_prompt("Q ?", [_excel()], dedup_header_metadata=dedup)
    p_text = R.build_prompt("Q ?", [_pdf()], dedup_header_metadata=dedup)
    assert R.EXCEL_RULES in p_excel and R.TABLE_RULES not in p_excel
    assert R.EXCEL_RULES not in p_text and R.TABLE_RULES not in p_text


# ---------------------------------------------------------------------------
# C1 — marqueur de troncature compté
# ---------------------------------------------------------------------------
def test_c1_off_reproduit_l_historique_a_l_octet_pres():
    t = _table_text(rows=120)
    for n in (50, 200, 500, len(t) - 1, len(t), len(t) + 1, 4000):
        assert pb._truncate_table_text(t, n) == _legacy_truncate_table_text(t, n)


def test_c1_defaut_historique_depasse_le_plafond_du_marqueur():
    """Le défaut, reproduit : lignes à hauteur du plafond, PUIS marqueur."""
    t = _table_text()
    out = pb._truncate_table_text(t, 1000)
    assert out.endswith(MARKER) and len(out) > 1000


def test_c1_on_respecte_le_plafond_et_garde_des_lignes_entieres():
    t = _table_text(rows=120)
    assert len(t) > 4000
    lines = set(t.splitlines())
    for n in (300, 1000, 2500, 4000):
        out = pb._truncate_table_text(t, n, count_marker=True)
        assert len(out) <= n, n
        body = out.splitlines()
        assert body[-1] == MARKER and body.count(MARKER) == 1
        assert all(line in lines for line in body[:-1])  # jamais de demi-ligne
        assert body[:-1] == t.splitlines()[: len(body) - 1]  # préfixe strict, en ordre


@pytest.mark.parametrize("count", [False, True])
def test_c1_cas_limites_sans_troncature(count):
    t = _table_text(rows=5)
    assert pb._truncate_table_text(t, len(t), count_marker=count) == t  # exactement égal
    assert pb._truncate_table_text(t, len(t) + 1, count_marker=count) == t
    assert pb._truncate_table_text(t, 0, count_marker=count) == t  # plafond désactivé
    assert pb._truncate_table_text("", 10, count_marker=count) == ""


def test_c1_depassement_d_un_caractere_et_depassement_du_seul_marqueur():
    t = _table_text(rows=5)
    n = len(t) - 1  # dépasse d'UN caractère
    legacy = pb._truncate_table_text(t, n)
    fixed = pb._truncate_table_text(t, n, count_marker=True)
    assert len(legacy) > n  # historique : lignes + marqueur > plafond
    assert len(fixed) <= n and fixed.endswith(MARKER)
    # Dépassement dû UNIQUEMENT au marqueur : toutes les lignes tiennent, pas le marqueur.
    k = len("\n".join(t.splitlines()[:-1])) + 1 + 10
    out = pb._truncate_table_text(t, k, count_marker=True)
    assert len(out) <= k and out.endswith(MARKER)


def test_c1_premiere_ligne_trop_longue_ou_aucune_ligne_exploitable():
    longue = "| " + "x" * 5000 + " |\n| a | b |"
    out = pb._truncate_table_text(longue, 400, count_marker=True)
    assert out == MARKER  # aucune ligne ne tient : on le DIT, on ne coupe pas une ligne
    assert pb._truncate_table_text(longue, 400) == MARKER  # idem historiquement


def test_c1_valeurs_unites_accents_intacts():
    t = _table_text()
    out = pb._truncate_table_text(t, 1500, count_marker=True)
    for line in out.splitlines()[:-1]:
        assert line.rstrip().endswith("|")  # ligne Markdown complète
        assert "1 234,56" in line or "Désignation" in line or line.startswith("|---")


def test_c1_marqueur_deja_present_jamais_double_et_idempotence():
    t = _table_text()
    once = pb._truncate_table_text(t, 2000, count_marker=True)
    twice = pb._truncate_table_text(once, 2000, count_marker=True)
    assert twice == once  # idempotent à plafond égal
    smaller = pb._truncate_table_text(once, 1200, count_marker=True)
    assert smaller.count(MARKER) == 1 and smaller.endswith(MARKER) and len(smaller) <= 1200
    # Le texte prépar  é à 4 000 n'est plus jamais recoupé par le gabarit (plafond interne
    # 4 000 de `render_prompt`) : un seul marqueur dans le prompt.
    prepared = pb._prepare_selected_for_prompt(
        [_excel(_table_text(rows=400))], snippet_max_chars=800, table_max_chars=4000,
        count_table_marker=True,
    )
    assert len(prepared[0]["text"]) <= 4000
    prompt = R.build_prompt("Q ?", prepared, dedup_header_metadata=True)
    assert prompt.count("extrait tronqué") == 1


def test_c1_source_unique_tabulaire_plafond_respecte_sous_on_et_depasse_sous_off():
    """Le cas VIS-000074-Q02 sur fixture : une seule source, classeur, budget serré."""
    sel = [_excel(_table_text(rows=120))]
    common = dict(question_for_prompt="Q ?", selected=sel, template_content=None,
                  snippet_max_chars=800, table_max_chars=4000)
    overhead = len(R.build_prompt("Q ?", []))
    limit = overhead + len(R.EXCEL_RULES) + 1800  # place pour ~1 800 c de bloc source
    p_off, s_off, i_off = pb._build_prompt_with_budget(max_prompt_chars=limit, **common)
    p_on, s_on, i_on = pb._build_prompt_with_budget(
        max_prompt_chars=limit, render_conformance=True, **common
    )
    assert i_off["final_source_truncated"] and i_on["final_source_truncated"]
    assert not i_off["prompt_budget_unable_to_fit_minimum_source"]
    assert len(p_off) > limit  # le défaut : dépassement SILENCIEUX
    assert len(p_on) <= limit  # corrigé, et dit :
    assert i_on["prompt_limit_exceeded"] is False
    assert i_on["prompt_limit_exception_reason"] is None
    assert i_on["prompt_remaining_chars"] == limit - len(p_on) >= 0
    assert len(s_on) == 1 and s_on[0]["chunk_id"] == "x1"
    assert s_on[0]["text"].endswith(MARKER)
    assert "selected_source_chars" in i_off and "prompt_total_chars" not in i_off


def test_c1_protection_de_derniere_source_conservee_et_signalee():
    """Plafond mathématiquement intenable : la source reste, le dépassement est DIT."""
    sel = [_excel(_table_text(rows=120))]
    overhead = len(R.build_prompt("Q ?", []))
    limit = overhead + len(R.EXCEL_RULES) + 50  # moins que RAG_MIN_FINAL_SOURCE_CHARS
    p_on, s_on, i_on = pb._build_prompt_with_budget(
        question_for_prompt="Q ?", selected=sel, template_content=None,
        max_prompt_chars=limit, snippet_max_chars=800, table_max_chars=4000,
        render_conformance=True,
    )
    assert len(s_on) == 1  # jamais zéro source
    assert i_on["prompt_budget_unable_to_fit_minimum_source"] is True
    assert i_on["prompt_limit_exceeded"] is True
    assert i_on["prompt_limit_exception_reason"] == "minimum_source_protection"
    assert i_on["prompt_remaining_chars"] < 0
    assert len(s_on[0]["text"]) <= pb._min_final_source_chars()


def test_c1_tableau_structure_kind_table_aussi():
    ch = {"chunk_id": "t", "text": _table_text(rows=200),
          "source": {"filename": "calc.pdf", "meta": {"kind": "table", "table_type": "efforts"}}}
    out = pb._prepare_selected_for_prompt([ch], snippet_max_chars=800, table_max_chars=4000,
                                          count_table_marker=True)
    assert len(out[0]["text"]) <= 4000 and out[0]["text"].endswith(MARKER)
    p = R.build_prompt("Q ?", out)
    assert R.TABLE_RULES in p and "— TABLEAU efforts" in _header(p)


def test_c1_budgets_tres_faibles():
    t = _table_text(rows=3)
    for n in (1, 5, 50, len(MARKER), len(MARKER) + 1):
        out = pb._truncate_table_text(t, n, count_marker=True)
        assert out.endswith(MARKER) and out.count(MARKER) == 1
        assert all(line in t.splitlines() for line in out.splitlines()[:-1])


# ---------------------------------------------------------------------------
# C2 — en-tête sans date / page répétées
# ---------------------------------------------------------------------------
def test_c2_off_repete_date_et_page_on_ne_les_repete_pas():
    ch = _pdf(label="rapport.pdf (2024-03-12) p.7")
    off = _header(R.build_prompt("Q ?", [ch]))
    on = _header(R.build_prompt("Q ?", [ch], dedup_header_metadata=True))
    assert off == "[1] rapport.pdf (2024-03-12) p.7 (2024-03-12) p.7"
    assert on == "[1] rapport.pdf (2024-03-12) p.7"


@pytest.mark.parametrize(
    "label,date,page,attendu",
    [
        ("rapport.pdf (2024-03-12)", "2024-03-12", 7, "[1] rapport.pdf (2024-03-12) p.7"),  # date seule portée
        ("rapport.pdf p.7", "2024-03-12", 7, "[1] rapport.pdf p.7 (2024-03-12)"),  # page seule portée
        ("rapport.pdf", "2024-03-12", 7, "[1] rapport.pdf (2024-03-12) p.7"),  # déjà correct : inchangé
        ("rapport.pdf", None, 7, "[1] rapport.pdf p.7"),  # sans date
        ("rapport.pdf", "2024-03-12", None, "[1] rapport.pdf (2024-03-12)"),  # sans page
        ("rapport 2024-03-12.pdf", "2024-03-12", 7, "[1] rapport 2024-03-12.pdf (2024-03-12) p.7"),  # date DANS le titre : pas un suffixe
        ("p.7.pdf", "2024-03-12", 7, "[1] p.7.pdf (2024-03-12) p.7"),  # page dans le nom de fichier
        ("rapport.pdf (2024-03-12) p.7", "2024-03-12", "vii", "[1] rapport.pdf (2024-03-12) p.7 p.vii"),  # page non numérique ≠ p.7
        ("rapport.pdf (2024-03-11) p.7", "2024-03-12", 7, "[1] rapport.pdf (2024-03-11) p.7 (2024-03-12)"),  # date DIFFÉRENTE conservée
        ("rapport.pdf (2024-03-12) p.8", "2024-03-12", 7, "[1] rapport.pdf (2024-03-12) p.8 p.7"),  # page DISTINCTE conservée
        ("rapport_v2 (2024-03-12) p.7", "2024-03-12", 7, "[1] rapport_v2 (2024-03-12) p.7"),  # version dans le titre intacte
    ],
)
def test_c2_matrice(label, date, page, attendu):
    ch = _pdf(label=label, date=date, page=page)
    assert _header(R.build_prompt("Q ?", [ch], dedup_header_metadata=True)) == attendu


def test_c2_feuille_et_plage_excel_intactes():
    ch = _excel(label="dossier.xlsx (2024-03-12) p.1", date="2024-03-12", page=1)
    on = _header(R.build_prompt("Q ?", [ch], dedup_header_metadata=True))
    assert on == "[1] dossier.xlsx (2024-03-12) p.1 — CLASSEUR (feuille « Synthèse », cellules B8:F17)"
    ch2 = _excel(label="dossier.xlsx")
    assert _header(R.build_prompt("Q ?", [ch2], dedup_header_metadata=True)) == (
        "[1] dossier.xlsx — CLASSEUR (feuille « Synthèse », cellules B8:F17)"
    )


def test_c2_deux_documents_aux_libelles_similaires_numerotation_et_ids_inchanges():
    a = _pdf(0, label="rapport.pdf (2024-03-12) p.7")
    b = _pdf(1, label="rapport.pdf (2024-03-12) p.8", page=8)
    p, acc = R.render_prompt("Q ?", [a, b], dedup_header_metadata=True)
    assert _header(p, 1) == "[1] rapport.pdf (2024-03-12) p.7"
    assert _header(p, 2) == "[2] rapport.pdf (2024-03-12) p.8"
    assert acc["headers_deduplicated"] == 2 and acc["rendered_sources"] == 2
    assert a["chunk_id"] == "p0" and b["chunk_id"] == "p1"  # jamais muté
    assert a["source"]["label"] == "rapport.pdf (2024-03-12) p.7"


def test_c2_idempotence_sur_entree_deja_normalisee():
    assert R._drop_metadata_already_in_label("rapport.pdf (2024-03-12) p.7", "(2024-03-12)", "p.7") == ("", "")
    assert R._drop_metadata_already_in_label("rapport.pdf", "(2024-03-12)", "p.7") == ("(2024-03-12)", "p.7")
    assert R._drop_metadata_already_in_label("rapport.pdf", "", "") == ("", "")
    ch = _pdf(label="rapport.pdf (2024-03-12) p.7")
    p1 = R.build_prompt("Q ?", [ch], dedup_header_metadata=True)
    p2 = R.build_prompt("Q ?", [ch], dedup_header_metadata=True)
    assert p1 == p2


# ---------------------------------------------------------------------------
# V4 — compteurs de coût des règles conditionnelles
# ---------------------------------------------------------------------------
def _budget(sel, *, on=True, limit=20000, template=None):
    return pb._build_prompt_with_budget(
        question_for_prompt="Q ?", selected=sel, template_content=template,
        max_prompt_chars=limit, snippet_max_chars=800, table_max_chars=4000,
        render_conformance=on,
    )


@pytest.mark.parametrize(
    "sel,kind,cout",
    [
        ([_pdf(0)], "none", 0),
        ([_excel()], "excel", len(R.EXCEL_RULES)),
        ([{"chunk_id": "t", "text": _table_text(rows=5),
           "source": {"filename": "c.pdf", "meta": {"kind": "table"}}}], "table", len(R.TABLE_RULES)),
        ([_excel(), {"chunk_id": "t", "text": _table_text(rows=5),
                     "source": {"filename": "c.pdf", "meta": {"kind": "table"}}}],
         "excel+table", len(R.EXCEL_RULES) + len(R.TABLE_RULES)),
    ],
)
def test_v4_rules_kind_et_cout_reel(sel, kind, cout):
    prompt, kept, info = _budget(sel)
    assert info["rules_kind"] == kind
    assert info["conditional_rules_chars"] == cout
    # Identité algébrique, composantes DISJOINTES : coût fixe + règles + bloc SOURCES.
    assert info["prompt_total_chars"] == len(prompt) == (
        info["prompt_overhead_chars"] + info["conditional_rules_chars"] + info["source_context_chars"]
    )
    # Le compteur historique contient DÉJÀ les règles : il n'est pas redéfini.
    assert info["selected_source_chars"] == info["conditional_rules_chars"] + info["source_context_chars"]
    assert info["prompt_limit_chars"] == 20000
    assert info["prompt_remaining_chars"] == 20000 - len(prompt)
    assert info["prompt_limit_exceeded"] is False and info["prompt_limit_exception_reason"] is None


def test_v4_identite_avec_gabarit_personnalise():
    tpl = "Consigne maison.\n\nSOURCES:\n{context}\n\nQUESTION:\n{question}\n"
    prompt, kept, info = _budget([_excel()], template=tpl)
    assert info["rules_kind"] == "excel"
    assert info["conditional_rules_chars"] == len(R.EXCEL_RULES) + 1  # « \n » du garde-fou
    assert len(prompt) == info["prompt_overhead_chars"] + info["conditional_rules_chars"] + info["source_context_chars"]


def test_v4_compteurs_absents_sous_off_et_historiques_identiques():
    sel = [_pdf(0), _pdf(1)]
    p_off, k_off, i_off = _budget(sel, on=False)
    p_on, k_on, i_on = _budget(sel, on=True)
    for k in ("rules_kind", "conditional_rules_chars", "prompt_total_chars", "prompt_limit_chars",
              "prompt_remaining_chars", "prompt_limit_exceeded", "prompt_limit_exception_reason"):
        assert k not in i_off and k in i_on
    for k in ("prompt_overhead_chars", "source_budget_chars", "sources_before_budget",
              "sources_after_budget", "sources_dropped_by_budget", "prompt_budget_exhausted",
              "final_source_truncated", "min_sources_reserved"):
        assert i_off[k] == i_on[k], k


def test_v4_aucun_contenu_confidentiel_dans_les_compteurs():
    prompt, kept, info = _budget([_excel(), _pdf(0)])
    for k, v in info.items():
        assert isinstance(v, (int, float, bool)) or v is None or (
            k in ("rules_kind", "prompt_limit_exception_reason") and v in R.RULES_KINDS | {
                "minimum_source_protection", "unaccounted_overflow"}
        ), (k, v)


def test_v4_plafond_desactive():
    prompt, kept, info = _budget([_pdf(0)], limit=0)
    assert info["prompt_limit_chars"] is None and info["prompt_remaining_chars"] is None
    assert info["prompt_limit_exceeded"] is False


def test_v4_trace_filtre_les_chaines_libres_et_garde_le_vocabulaire_ferme():
    tb = RetrievalTraceBuilder(entrypoint="chat_sync", request_id=None)
    tb.record_prompt_budget(before=[], after=[], accounting={
        "prompt_overhead_chars": 1511, "rules_kind": "excel", "conditional_rules_chars": 2812,
        "prompt_limit_exception_reason": "minimum_source_protection",
        "prompt_limit_exceeded": True, "libre": "texte d'un prompt", "rules_kind2": "excel",
        "prompt_limit_exception_reason_x": "autre",
    })
    kept = tb.corpus.prompt_budget_accounting
    assert kept["rules_kind"] == "excel" and kept["conditional_rules_chars"] == 2812
    assert kept["prompt_limit_exception_reason"] == "minimum_source_protection"
    assert "libre" not in kept and "rules_kind2" not in kept and "prompt_limit_exception_reason_x" not in kept
    tb.record_prompt_budget(before=[], after=[], accounting={"rules_kind": "n'importe quoi"})
    assert "rules_kind" not in tb.corpus.prompt_budget_accounting
    summary = tb.corpus._prompt_budget_summary()
    assert "rules_kind" not in summary  # jamais une chaîne hors vocabulaire


# ---------------------------------------------------------------------------
# Chemin réel : build_prompt_stage, OFF → ON → OFF
# ---------------------------------------------------------------------------
def _ctx(question="Quel est le prix unitaire ?"):
    req = QueryRequest.from_legacy_args(
        rag_space_id="00000000-0000-0000-0000-000000000001", question=question, user=None,
        rag_space=None, request_id=None, conversation_context=None, entrypoint="chat_sync",
    )
    prep = PreparedQuery(original_question=question, retrieval_queries=(question,),
                         primary_retrieval_query=question, rewritten_query=None, inventory=False,
                         explanatory=False, news=False, has_temporal_intent=False, comparative=False)
    ctx = QueryExecutionContext.create(
        req, started_at=time.perf_counter(),
        trace=RetrievalTraceBuilder(entrypoint="chat_sync", request_id=None),
    )
    ctx.prepared = prep
    return ctx


def _stage(selected):
    return pb.build_prompt_stage(_ctx(), selected=selected, low_confidence=False, strict_refusal=False)


@override_settings(RAG_MAX_PROMPT_CHARS=8300, RAG_SNIPPET_MAX_CHARS=800,
                   RAG_SNIPPET_MAX_CHARS_TABLE=4000, RAG_MIN_FINAL_SOURCE_CHARS=400)
def test_off_on_off_sur_le_chemin_reel():
    sel = [_excel(_table_text(rows=120), label="dpgf.xlsx (2024-04-01) p.1", date="2024-04-01", page=1)] + [
        _pdf(i, label=f"rapport{i}.pdf (2024-03-12) p.{i+1}", page=i + 1) for i in range(6)
    ]
    with override_settings(**OFF):
        off1 = _stage(sel)
    with override_settings(**ON):
        on_a = _stage(sel)
        on_b = _stage(sel)
    with override_settings(**OFF):
        off2 = _stage(sel)
    assert off1.prompt == off2.prompt and off1.budget == off2.budget  # rollback exact
    assert on_a.prompt == on_b.prompt  # déterminisme
    assert on_a.prompt != off1.prompt
    assert "render_conformance_v1" not in off1.budget and on_a.budget["render_conformance_v1"] is True
    assert on_a.budget["rules_kind"] == "excel"
    assert on_a.budget["prompt_total_chars"] == len(on_a.prompt) <= 8300
    # Les en-têtes ON sont ceux de OFF sans la répétition, numérotation intacte.
    for i in range(1, len(off1.sources) + 1):
        h_off, h_on = _header(off1.prompt, i), _header(on_a.prompt, i)
        core_off, suf_off = h_off.split(" — ", 1) if " — " in h_off else (h_off, "")
        core_on, suf_on = h_on.split(" — ", 1) if " — " in h_on else (h_on, "")
        assert suf_off == suf_on  # suffixe CLASSEUR/TABLEAU intact
        assert core_off.split()[: len(core_on.split())] == core_on.split()
        extra = core_off.split()[len(core_on.split()):]
        assert extra == (["(2024-04-01)", "p.1"] if i == 1 else ["(2024-03-12)", f"p.{i-1}"])
    assert [s["chunk_id"] for s in on_a.sources][: len(off1.sources)] == [s["chunk_id"] for s in off1.sources]
    assert len(on_a.sources) >= len(off1.sources)  # un préfixe plus court ne perd jamais de source
    assert on_a.prompt.count("extrait tronqué") <= 1


def test_off_appelle_build_prompt_avec_la_signature_historique(monkeypatch):
    """Sous OFF, aucun argument nouveau n'atteint `build_prompt` : le rendu ne PEUT pas dévier."""
    seen = []

    def spy(question, chunks, *, template_content=None, answer_language=None):
        seen.append(True)
        return R.render_prompt(question, chunks, template_content=template_content,
                               answer_language=answer_language)[0]

    monkeypatch.setattr(pb, "build_prompt", spy)
    pb._build_prompt_with_budget(question_for_prompt="Q ?", selected=[_pdf()], template_content=None,
                                 max_prompt_chars=8300, snippet_max_chars=800)
    assert seen
