"""Benchmark comparatif multi-modèles orienté usages RagATM (Priorité — outil).

Objectif : comparer plusieurs modèles de génération servis par l'AI Gateway
le serveur IA sur des tâches RAG réelles (fidélité aux sources, refus correct,
citations, étanchéité inter-workspace, extraction JSON, synthèse, rapport), SANS
toucher au modèle de production ni aux embeddings, et SANS base de données.

Conception :
  - le retrieval est FIXE (chunks synthétiques fournis par la fixture) : seul le
    modèle varie, ce qui isole la qualité de GÉNÉRATION et rend le run reproductible ;
  - le prompt est construit avec le VRAI ``build_prompt`` de RagATM (mêmes règles
    de citation / anti-hallucination qu'en production) ;
  - le scoring est HEURISTIQUE et déterministe (aucun juge LLM) : sous-chaînes
    attendues/interdites, marqueurs de refus, citations ``[n]`` valides, JSON
    parsable, sections de structure — donc rejouable hors-ligne sur un JSON de run.

Découplé de Django et du réseau : le runner reçoit un ``generate_fn`` injectable.
La commande ``manage.py benchmark_llm_models`` fournit l'implémentation Gateway.
"""
from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

# Signature du callable de génération (injectable => testable sans réseau).
#   (model, prompt, options, think) -> dict avec au minimum {text, error?, tokens?,
#    usage?, cost?, latency_ms?}
GenerateFn = Callable[[str, str, dict[str, Any], "bool | None"], dict[str, Any]]

# Signature du juge LLM (injectable) : (prompt_juge) -> texte brut du juge.
JudgeFn = Callable[[str], str]

# Signature de la mesure de cold-load (injectable) : (model) -> latence ms du
# premier appel (chargement du modèle), ou None si non mesuré.
ColdLoadFn = Callable[[str], "float | None"]


# ---------------------------------------------------------------------------
# Normalisation & helpers texte (insensibles casse + accents)
# ---------------------------------------------------------------------------

def normalize(s: Any) -> str:
    """Minuscule, sans accents, espaces normalisés — pour un matching robuste."""
    t = "" if s is None else str(s)
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def contains_all(text: str, needles: Sequence[str]) -> tuple[bool, list[str]]:
    """(tous présents ?, liste des manquants) — matching normalisé."""
    n = normalize(text)
    missing = [x for x in (needles or []) if normalize(x) not in n]
    return (len(missing) == 0, missing)


def contains_any_regex(text: str, patterns: Sequence[str]) -> list[str]:
    """Retourne les patterns qui MATCHENT (interdits trouvés)."""
    hits: list[str] = []
    for p in patterns or []:
        try:
            if re.search(p, text, flags=re.IGNORECASE):
                hits.append(p)
        except re.error:
            # pattern invalide => ignoré (ne fait pas échouer le run)
            continue
    return hits


REFUSAL_MARKERS = (
    "je ne sais pas",
    "n'est pas presente",
    "nest pas presente",
    "pas presente dans",
    "pas dans les documents",
    "pas dans les sources",
    "information non disponible",
    "non disponible",
    "pas disponible",
    "n'est pas disponible",
    "nest pas disponible",
    "aucune information",
    "aucune donnee",
    "aucune mention",
    "pas d'information",
    "pas dinformation",
    "ne figure pas",
    "n'apparait pas",
    "napparait pas",
    "aucun montant",
    "pas mentionne",
    "nest pas mentionne",
    "nest mentionne",
    "non mentionne",
    "ne mentionne",
    "pas precise",
    "non precise",
    "ne permettent pas de repondre",
    "ne contiennent pas",
    "non fournie",
    "n'est pas fournie",
    "nest pas fournie",
)


def looks_like_refusal(text: str) -> bool:
    n = normalize(text)
    return any(m in n for m in REFUSAL_MARKERS)


_CITATION_RE = re.compile(r"\[(\d{1,2})\]")


def cited_indices(text: str) -> list[int]:
    return [int(m) for m in _CITATION_RE.findall(text or "")]


def extract_json(text: str) -> Any:
    """Extrait le premier objet JSON d'une réponse (tolère les fences ```json).

    Retourne l'objet parsé, ou lève ``ValueError`` si rien de parsable.
    """
    if not text:
        raise ValueError("empty text")
    s = text.strip()
    # retire les fences markdown
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, flags=re.DOTALL | re.IGNORECASE)
    if fence:
        s = fence.group(1).strip()
    # tentative directe
    try:
        return json.loads(s)
    except Exception:
        pass
    # sinon, premier bloc {...} équilibré
    start = s.find("{")
    if start == -1:
        raise ValueError("no JSON object found")
    depth = 0
    for i in range(start, len(s)):
        c = s[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start : i + 1])
    raise ValueError("unbalanced JSON braces")


# ---------------------------------------------------------------------------
# Modèle de cas & scoring
# ---------------------------------------------------------------------------

DEFAULT_GEN = {"temperature": 0.1, "num_ctx": 8192, "num_predict": 1024}


@dataclass
class Case:
    id: str
    workspace: str
    question: str
    category: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    checks: dict[str, Any] = field(default_factory=dict)
    gen: dict[str, Any] = field(default_factory=dict)
    think: bool | None = None

    def gen_options(self, *, num_ctx: int | None = None, num_predict: int | None = None) -> dict[str, Any]:
        """Options Gateway effectives pour ce cas (overrides CLI prioritaires)."""
        o = dict(DEFAULT_GEN)
        o.update(self.gen or {})
        if num_ctx is not None:
            o["num_ctx"] = int(num_ctx)
        if num_predict is not None:
            o["num_predict"] = int(num_predict)
        return o


def load_cases(path: str | Path) -> list[Case]:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    cases_raw = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(cases_raw, list) or not cases_raw:
        raise ValueError(f"dataset invalide (aucun cas): {p}")
    out: list[Case] = []
    for c in cases_raw:
        out.append(
            Case(
                id=str(c["id"]),
                workspace=str(c.get("workspace", "")),
                question=str(c["question"]),
                category=str(c.get("category", "")),
                sources=list(c.get("sources", [])),
                checks=dict(c.get("checks", {})),
                gen=dict(c.get("gen", {})),
                think=c.get("think"),
            )
        )
    return out


def build_prompt_for_case(case: Case) -> str:
    """Construit le prompt via le VRAI build_prompt RagATM (chunks = sources fixes)."""
    from apps.rag_core.domain.retrieval import build_prompt

    chunks: list[dict[str, Any]] = []
    for i, src in enumerate(case.sources, start=1):
        filename = src.get("filename") or src.get("label") or f"source-{i}.txt"
        chunks.append(
            {
                "text": src.get("text", ""),
                "source": {"filename": filename, "label": filename},
            }
        )
    return build_prompt(case.question, chunks)


def score_response(case: Case, text: str) -> dict[str, Any]:
    """Évalue une réponse. Retourne des scores 0..1 par dimension + drapeaux.

    Dimensions (None = non applicable à ce cas) :
      fidelity, hallucination, refusal, citations, json, structure.
    """
    checks = case.checks or {}
    text = text or ""
    n_sources = len(case.sources)
    result: dict[str, Any] = {"flags": [], "notes": []}

    # --- Fidélité : sous-chaînes attendues présentes -----------------------
    # ``must_contain``     : chaque item DOIT apparaître (matching littéral robuste).
    # ``must_contain_any`` : liste de GROUPES ; un groupe est satisfait si AU MOINS
    #   une variante apparaît (évite les faux négatifs de simple reformulation, ex.
    #   « aucun impact financier » vs « aucune estimation financière »).
    must = checks.get("must_contain")
    any_groups = checks.get("must_contain_any")
    if must is not None or any_groups is not None:
        total = 0
        satisfied = 0
        missing: list[str] = []
        n = normalize(text)
        for item in must or []:
            total += 1
            if normalize(item) in n:
                satisfied += 1
            else:
                missing.append(item)
        for group in any_groups or []:
            total += 1
            if any(normalize(v) in n for v in group):
                satisfied += 1
            else:
                missing.append(f"aucune variante de {group}")
        result["fidelity"] = round(satisfied / max(1, total), 4)
        if missing:
            result["notes"].append(f"manquants: {missing}")
    else:
        result["fidelity"] = None

    # --- Hallucination : aucune sous-chaîne / regex interdite --------------
    forbidden = list(checks.get("must_not_contain", []) or [])
    forbidden_rx = list(checks.get("must_not_contain_regex", []) or [])
    hall_hits: list[str] = []
    if forbidden:
        _, present = contains_all(text, forbidden)  # present = ceux NON trouvés
        found = [x for x in forbidden if x not in present]
        hall_hits.extend(found)
    if forbidden_rx:
        hall_hits.extend(contains_any_regex(text, forbidden_rx))
    if forbidden or forbidden_rx:
        result["hallucination"] = 1.0 if not hall_hits else 0.0
        if hall_hits:
            result["flags"].append("hallucination")
            result["notes"].append(f"interdits trouvés: {hall_hits}")
    else:
        result["hallucination"] = None

    # --- Refus correct (info absente / étanchéité) -------------------------
    if checks.get("expect_refusal"):
        refused = looks_like_refusal(text)
        # un refus n'est valable que si aucune donnée interdite n'a fuité
        result["refusal"] = 1.0 if (refused and not hall_hits) else 0.0
        if not refused:
            result["flags"].append("no_refusal")
    else:
        result["refusal"] = None

    # --- Citations : au moins une, toutes dans l'intervalle valide ---------
    if checks.get("expect_citation"):
        cites = cited_indices(text)
        if not cites:
            result["citations"] = 0.0
            result["flags"].append("no_citation")
        else:
            out_of_range = [c for c in cites if c < 1 or c > n_sources]
            result["citations"] = 1.0 if not out_of_range else 0.0
            if out_of_range:
                result["flags"].append("citation_out_of_range")
                result["notes"].append(f"citations hors bornes: {out_of_range}/{n_sources}")
    else:
        result["citations"] = None

    # --- JSON valide + clés requises ---------------------------------------
    json_keys = checks.get("json_required_keys")
    if json_keys is not None:
        try:
            obj = extract_json(text)
            if not isinstance(obj, dict):
                raise ValueError("not an object")
            missing_keys = [k for k in json_keys if k not in obj]
            result["json"] = 1.0 if not missing_keys else 0.0
            if missing_keys:
                result["flags"].append("json_missing_keys")
                result["notes"].append(f"clés JSON manquantes: {missing_keys}")
        except Exception as exc:
            result["json"] = 0.0
            result["flags"].append("json_invalid")
            result["notes"].append(f"JSON illisible: {type(exc).__name__}")
    else:
        result["json"] = None

    # --- Structure : sections attendues présentes --------------------------
    sections = checks.get("structure_sections")
    if sections is not None:
        ok, missing = contains_all(text, sections)
        result["structure"] = round(1.0 - (len(missing) / max(1, len(sections))), 4)
        if missing:
            result["notes"].append(f"sections manquantes: {missing}")
    else:
        result["structure"] = None

    return result


# ---------------------------------------------------------------------------
# Juge LLM (optionnel) — qualité rédactionnelle des rapports/synthèses
# ---------------------------------------------------------------------------

def build_judge_prompt(case: "Case", answer: str) -> str:
    """Construit le prompt du juge : évalue fidélité aux sources + structure.

    Le juge ne voit QUE les sources fournies (mêmes chunks que le candidat) et doit
    répondre par un JSON strict ``{"score": 0..10, "rationale": "..."}``.
    """
    sources = "\n\n".join(
        f"[{i}] {s.get('filename') or s.get('label') or ('source-%d' % i)}\n{s.get('text', '')}"
        for i, s in enumerate(case.sources or [], start=1)
    )
    return (
        "Tu es un évaluateur STRICT et impartial de réponses RAG. Tu notes UNIQUEMENT "
        "à partir des SOURCES fournies.\n"
        "Critères (0 à 10) : fidélité aux sources (aucune information inventée), "
        "complétude vis-à-vis de la question, clarté et structure. Une réponse qui "
        "invente une information ou contredit les sources doit recevoir un score bas.\n\n"
        "Réponds UNIQUEMENT par un JSON valide de la forme "
        '{"score": <entier 0-10>, "rationale": "<courte justification>"}.\n\n'
        f"QUESTION:\n{case.question}\n\n"
        f"SOURCES:\n{sources}\n\n"
        f"RÉPONSE À ÉVALUER:\n{answer}\n"
    )


def parse_judge_score(text: str) -> float | None:
    """Extrait un score 0..1 depuis la sortie JSON du juge (``score`` sur 10)."""
    try:
        obj = extract_json(text)
    except Exception:
        return None
    if not isinstance(obj, dict) or "score" not in obj:
        return None
    try:
        raw = float(obj["score"])
    except (TypeError, ValueError):
        return None
    # tolère un score déjà normalisé 0..1 ou un score 0..10.
    if raw <= 1.0:
        return round(max(0.0, min(1.0, raw)), 4)
    return round(max(0.0, min(1.0, raw / 10.0)), 4)


def _judge_applicable(case: "Case") -> bool:
    return bool((case.checks or {}).get("judge"))


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

# Ordre de priorité de notation (Phase 9) : étanchéité/refus > fidélité >
# hallucination > citations > format > structure > qualité rédactionnelle (juge).
_WEIGHTS = {
    "refusal": 3.0,
    "fidelity": 2.5,
    "hallucination": 2.0,
    "citations": 1.5,
    "json": 1.2,
    "structure": 1.0,
    "judge": 1.0,
}


def weighted_case_score(scores: dict[str, Any]) -> float | None:
    num = 0.0
    den = 0.0
    for k, w in _WEIGHTS.items():
        v = scores.get(k)
        if v is None:
            continue
        num += w * float(v)
        den += w
    return None if den == 0 else round(num / den, 4)


@dataclass
class CaseRun:
    case_id: str
    workspace: str
    category: str
    empty: bool
    error: str | None
    latency_ms: float
    tokens: int
    cost: float | None
    scores: dict[str, Any]
    overall: float | None
    text_preview: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "workspace": self.workspace,
            "category": self.category,
            "empty": self.empty,
            "error": self.error,
            "latency_ms": round(self.latency_ms, 1),
            "tokens": self.tokens,
            "cost": self.cost,
            "scores": self.scores,
            "overall": self.overall,
            "text_preview": self.text_preview,
        }


def run_case(
    case: Case,
    *,
    model: str,
    generate_fn: GenerateFn,
    num_ctx: int | None = None,
    num_predict: int | None = None,
    preview_chars: int = 400,
    judge_fn: "JudgeFn | None" = None,
) -> CaseRun:
    prompt = build_prompt_for_case(case)
    options = case.gen_options(num_ctx=num_ctx, num_predict=num_predict)
    think = case.think

    start = time.perf_counter()
    try:
        out = generate_fn(model, prompt, options, think)
    except Exception as exc:  # robustesse : un modèle KO ne casse pas le run
        latency = (time.perf_counter() - start) * 1000.0
        return CaseRun(
            case_id=case.id, workspace=case.workspace, category=case.category,
            empty=True, error=f"{type(exc).__name__}: {exc}", latency_ms=latency,
            tokens=0, cost=None, scores={}, overall=None, text_preview="",
        )
    latency = out.get("latency_ms")
    if latency is None:
        latency = (time.perf_counter() - start) * 1000.0

    err = out.get("error")
    text = (out.get("text") or "").strip()
    empty = not bool(text)

    scores = {} if (err or empty) else score_response(case, text)
    # Juge LLM (optionnel) : qualité rédactionnelle sur les cas marqués ``judge``.
    if scores and judge_fn is not None and _judge_applicable(case):
        try:
            js = parse_judge_score(judge_fn(build_judge_prompt(case, text)))
        except Exception:
            js = None
        if js is not None:
            scores["judge"] = js
    overall = None if (err or empty) else weighted_case_score(scores)

    return CaseRun(
        case_id=case.id,
        workspace=case.workspace,
        category=case.category,
        empty=empty,
        error=err,
        latency_ms=float(latency),
        tokens=int(out.get("tokens") or 0),
        cost=out.get("cost"),
        scores=scores,
        overall=overall,
        text_preview=text[:preview_chars],
    )


def _mean(vals: Sequence[float]) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def aggregate_model(runs: Sequence[CaseRun]) -> dict[str, Any]:
    dims = ["fidelity", "hallucination", "refusal", "citations", "json", "structure", "judge"]
    per_dim = {
        d: _mean([r.scores.get(d) for r in runs if r.scores.get(d) is not None])
        for d in dims
    }
    overalls = [r.overall for r in runs if r.overall is not None]
    latencies = [r.latency_ms for r in runs if not r.error]
    tokens = [r.tokens for r in runs if not r.error]
    empties = [r for r in runs if r.empty and not r.error]
    errors = [r for r in runs if r.error]
    return {
        "n_cases": len(runs),
        "n_empty": len(empties),
        "n_errors": len(errors),
        "dimensions": per_dim,
        "overall": _mean(overalls),
        "latency_ms": {
            "mean": _mean(latencies),
            "p50": _percentile(latencies, 50),
            "max": round(max(latencies), 1) if latencies else None,
        },
        "tokens_total": sum(tokens),
        "flags": _collect_flags(runs),
    }


def _collect_flags(runs: Sequence[CaseRun]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in runs:
        for f in r.scores.get("flags", []) if r.scores else []:
            counts[f] = counts.get(f, 0) + 1
        if r.empty and not r.error:
            counts["empty_response"] = counts.get("empty_response", 0) + 1
    return counts


def _percentile(vals: Sequence[float], pct: int) -> float | None:
    xs = sorted(v for v in vals if v is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    frac = k - lo
    return round(xs[lo] * (1 - frac) + xs[hi] * frac, 1)


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.2f}"
    return str(v)


def render_markdown(report: dict[str, Any]) -> str:
    """Tableau comparatif Markdown (une ligne par modèle)."""
    meta = report.get("meta", {})
    lines: list[str] = []
    lines.append("# Benchmark RagATM multi-modèles\n")
    for k in ("generated_at", "gateway", "dataset", "n_cases", "num_ctx", "num_predict", "embeddings"):
        if k in meta:
            lines.append(f"- **{k}** : {meta[k]}")
    lines.append("")
    lines.append(
        "| Modèle | Fidélité | Hallucination | Refus | Citations | JSON | Structure | Juge | Overall | Latence moy. (ms) | Cold-load (ms) | Vides | Erreurs |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for model, blob in report.get("models", {}).items():
        s = blob.get("summary", {})
        d = s.get("dimensions", {})
        lat = s.get("latency_ms", {})
        lines.append(
            "| `{m}` | {fi} | {ha} | {re} | {ci} | {js} | {st} | {ju} | {ov} | {lat} | {cold} | {em} | {er} |".format(
                m=model,
                fi=_fmt(d.get("fidelity")), ha=_fmt(d.get("hallucination")),
                re=_fmt(d.get("refusal")), ci=_fmt(d.get("citations")),
                js=_fmt(d.get("json")), st=_fmt(d.get("structure")),
                ju=_fmt(d.get("judge")),
                ov=_fmt(s.get("overall")), lat=_fmt(lat.get("mean")),
                cold=_fmt(s.get("cold_load_ms")),
                em=_fmt(s.get("n_empty")), er=_fmt(s.get("n_errors")),
            )
        )
    lines.append("")
    # Détail des drapeaux (hallucinations, refus manqués, citations…) par modèle.
    for model, blob in report.get("models", {}).items():
        flags = blob.get("summary", {}).get("flags", {})
        if flags:
            lines.append(f"- **{model}** drapeaux : " + ", ".join(f"{k}×{v}" for k, v in flags.items()))
    return "\n".join(lines) + "\n"


def run_benchmark(
    *,
    models: Sequence[str],
    cases: Sequence[Case],
    generate_fn: GenerateFn,
    num_ctx: int | None = None,
    num_predict: int | None = None,
    meta: dict[str, Any] | None = None,
    progress: Callable[[str], None] | None = None,
    judge_fn: "JudgeFn | None" = None,
    cold_load_fn: "ColdLoadFn | None" = None,
) -> dict[str, Any]:
    report: dict[str, Any] = {"meta": dict(meta or {}), "models": {}}
    for model in models:
        # Cold-load : mesure isolée du premier chargement du modèle (probe minimal),
        # séparée des latences de génération « à chaud » des cas.
        cold_load_ms = None
        if cold_load_fn is not None:
            if progress:
                progress(f"  · {model} / cold-load probe")
            try:
                cold_load_ms = cold_load_fn(model)
            except Exception:
                cold_load_ms = None

        runs: list[CaseRun] = []
        for case in cases:
            eff_np = num_predict
            # qwen3:14b : budget de tokens minimal impératif (raisonnement interne).
            if "qwen3:14b" in model and (eff_np or case.gen_options().get("num_predict", 0)) < 768:
                eff_np = max(eff_np or 0, 1024)
            if progress:
                progress(f"  · {model} / {case.id}")
            runs.append(
                run_case(
                    case, model=model, generate_fn=generate_fn,
                    num_ctx=num_ctx, num_predict=eff_np, judge_fn=judge_fn,
                )
            )
        summary = aggregate_model(runs)
        summary["cold_load_ms"] = None if cold_load_ms is None else round(float(cold_load_ms), 1)
        report["models"][model] = {
            "summary": summary,
            "cases": [r.to_dict() for r in runs],
        }
    return report
