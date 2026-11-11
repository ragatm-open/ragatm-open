"""Comparaison baseline/candidat de deux rapports de benchmark (P1-A).

Fonctions *pures* (sans Django ni I/O) qui prennent deux rapports JSON déjà
chargés (dicts produits par ``report.to_json``) et produisent :

- les deltas agrégés (Recall@1/3/5, MRR, latence p95, n_errors) ;
- la classification par cas (régressions, améliorations, toujours réussis/échoués,
  erreurs introduites/résolues) ALIGNÉE PAR ``case_id`` (jamais par position) ;
- un verdict de compatibilité qui REFUSE de comparer deux benchmarks distincts ou
  invalides plutôt que de calculer silencieusement des deltas globaux.

Convention des deltas (documentée, testée) :
    delta = candidate - baseline
Pour la latence, un delta POSITIF = candidat plus lent.
Pour les rangs :
    rank_delta = baseline_rank - candidate_rank   (positif = meilleur rang candidat)

Le passage attendu absent du top-k donne un rang ``None`` (jamais 9999).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

DEFAULT_DECISION_K = 5


class ComparisonError(ValueError):
    """Entrée invalide ou rapports incompatibles (code de sortie CLI = 2)."""


# ---------------------------------------------------------------------------
# Chargement / extraction
# ---------------------------------------------------------------------------


def load_report(raw: str | dict[str, Any]) -> dict[str, Any]:
    """Parse un rapport (str JSON ou dict) et vérifie sa structure minimale."""
    if isinstance(raw, dict):
        report = raw
    else:
        try:
            report = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ComparisonError(f"JSON invalide: {exc}") from exc
    if not isinstance(report, dict):
        raise ComparisonError("le rapport doit être un objet JSON")
    if "summary" not in report or "cases" not in report:
        raise ComparisonError("rapport incompatible: champs 'summary'/'cases' requis")
    if not isinstance(report.get("cases"), list):
        raise ComparisonError("rapport incompatible: 'cases' doit être une liste")
    return report


def _benchmark_identity(report: dict[str, Any]) -> str:
    """Identité stable d'un benchmark pour comparer des runs comparables.

    Base : meta.benchmark > basename(meta.dataset) > meta.rag_spaces.
    Le ``profile`` (ex. 'stub' vs 'nomic') est intégré à l'identité : deux profils
    différents ne sont JAMAIS comparables (un run stub/FTS et un run sémantique
    mesurent des choses distinctes).
    """
    meta = report.get("meta") or {}
    base = ""
    bench = meta.get("benchmark")
    if isinstance(bench, str) and bench.strip():
        base = bench.strip()
    else:
        dataset = meta.get("dataset")
        if isinstance(dataset, str) and dataset.strip():
            stem = dataset.replace("\\", "/").rsplit("/", 1)[-1]
            base = stem.rsplit(".", 1)[0] if "." in stem else stem
        else:
            spaces = meta.get("rag_spaces")
            base = spaces.strip() if isinstance(spaces, str) and spaces.strip() else ""

    profile = meta.get("profile")
    if isinstance(profile, str) and profile.strip():
        base = f"{base}::{profile.strip()}"
    # Un run AVEC answerability n'est jamais comparable à un run sans (ou d'une autre
    # stratégie) : la décision répondre/refuser change.
    strat = meta.get("answerability_strategy")
    if meta.get("answerability_enabled") and isinstance(strat, str) and strat not in ("", "none"):
        base = f"{base}::ans-{strat}"
    # Le seuil lexical fait partie de l'identité (P1.9-f) : il gouverne la décision
    # répondre/refuser dès qu'un signal FTS existe. Deux runs à seuils différents mesurent
    # deux politiques, pas deux versions d'une même politique. Absent ou nul = identité
    # historique inchangée (aucune baseline existante n'est invalidée).
    try:
        mtr = float(meta.get("min_text_rank") or 0.0)
    except (TypeError, ValueError):
        mtr = 0.0
    if mtr > 0.0:
        base = f"{base}::tr{mtr:g}"
    return base


def _index_cases(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Indexe les cas par ``case_id``. Les doublons sont BLOQUANTS."""
    out: dict[str, dict[str, Any]] = {}
    for c in report.get("cases") or []:
        if not isinstance(c, dict):
            raise ComparisonError("cas invalide (objet attendu)")
        cid = c.get("case_id")
        if not isinstance(cid, str) or not cid.strip():
            raise ComparisonError(f"cas sans 'case_id' exploitable: {cid!r}")
        if cid in out:
            raise ComparisonError(f"case_id dupliqué: {cid!r}")
        out[cid] = c
    return out


# ---------------------------------------------------------------------------
# Décision succès / rang par cas
# ---------------------------------------------------------------------------


def _case_error(case: dict[str, Any]) -> str | None:
    err = case.get("error")
    if isinstance(err, str) and err.strip():
        return err.strip()
    # repli : un status 'error' sans message reste une erreur
    if str(case.get("status") or "").strip().lower() == "error":
        return "error"
    return None


def _first_rank(case: dict[str, Any]) -> int | None:
    r = case.get("first_relevant_rank")
    if isinstance(r, int) and r >= 1:
        return r
    return None


def _recall_at(case: dict[str, Any], k: int) -> float | None:
    rec = case.get("recall")
    if not isinstance(rec, dict):
        return None
    v = rec.get(str(k))
    return float(v) if isinstance(v, (int, float)) else None


def _is_success(case: dict[str, Any], decision_k: int) -> bool:
    """Succès = chunk attendu présent dans le top-k ET pas d'erreur.

    Priorité au rang du premier document pertinent (borné à decision_k). Repli sur
    ``recall@decision_k > 0`` si le rang n'est pas exposé.
    """
    if _case_error(case) is not None:
        return False
    rank = _first_rank(case)
    if rank is not None:
        return rank <= decision_k
    rec = _recall_at(case, decision_k)
    return rec is not None and rec > 0.0


# ---------------------------------------------------------------------------
# Résultat de comparaison
# ---------------------------------------------------------------------------


@dataclass
class CaseDelta:
    case_id: str
    question: str
    category: str  # regression|improvement|still_passing|still_failing
    baseline_rank: int | None
    candidate_rank: int | None
    rank_delta: int | None
    baseline_recall_at_k: float | None
    candidate_recall_at_k: float | None
    baseline_error: str | None
    candidate_error: str | None
    error_introduced: bool
    error_resolved: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "question": self.question,
            "category": self.category,
            "baseline_rank": self.baseline_rank,
            "candidate_rank": self.candidate_rank,
            "rank_delta": self.rank_delta,
            "baseline_recall_at_k": self.baseline_recall_at_k,
            "candidate_recall_at_k": self.candidate_recall_at_k,
            "baseline_error": self.baseline_error,
            "candidate_error": self.candidate_error,
            "error_introduced": self.error_introduced,
            "error_resolved": self.error_resolved,
        }


@dataclass
class ComparisonResult:
    status: str  # completed | incompatible
    compatible: bool
    decision_k: int
    reason: str
    baseline_info: dict[str, Any]
    candidate_info: dict[str, Any]
    metrics: dict[str, Any] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)
    regressions: list[CaseDelta] = field(default_factory=list)
    improvements: list[CaseDelta] = field(default_factory=list)
    still_passing: list[CaseDelta] = field(default_factory=list)
    still_failing: list[CaseDelta] = field(default_factory=list)
    missing_in_candidate: list[str] = field(default_factory=list)
    new_in_candidate: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "compatible": self.compatible,
            "decision_k": self.decision_k,
            "reason": self.reason,
            "baseline": self.baseline_info,
            "candidate": self.candidate_info,
            "metrics": self.metrics,
            "summary": self.summary,
            "regressions": [c.to_dict() for c in self.regressions],
            "improvements": [c.to_dict() for c in self.improvements],
            "still_failing": [c.to_dict() for c in self.still_failing],
            "missing_in_candidate": list(self.missing_in_candidate),
            "new_in_candidate": list(self.new_in_candidate),
        }


# ---------------------------------------------------------------------------
# Cœur de comparaison
# ---------------------------------------------------------------------------


def _summary_val(report: dict[str, Any], path: str) -> float | None:
    s = report.get("summary") or {}
    if path.startswith("recall_at_"):
        k = path.rsplit("_", 1)[-1]
        v = (s.get("recall_at_k") or {}).get(k)
    elif path == "latency_p95":
        v = (s.get("latency_ms") or {}).get("p95")
    else:
        v = s.get(path)
    return float(v) if isinstance(v, (int, float)) else None


def _delta(baseline: float | None, candidate: float | None) -> float | None:
    if baseline is None or candidate is None:
        return None
    return round(candidate - baseline, 6)


def _n_errors(report: dict[str, Any]) -> int:
    s = report.get("summary") or {}
    return int(s.get("n_errors") or 0)


def _obsolete_count(report: dict[str, Any]) -> int:
    s = report.get("summary") or {}
    return int(s.get("n_obsolete_references") or 0) + int(s.get("n_invalid_cases") or 0)


def _report_info(report: dict[str, Any], path: str | None) -> dict[str, Any]:
    s = report.get("summary") or {}
    return {
        "path": path,
        "benchmark": _benchmark_identity(report),
        "n_cases": int(s.get("n_cases") or 0),
        "n_errors": _n_errors(report),
        "n_obsolete_references": int(s.get("n_obsolete_references") or 0),
        "run_status": s.get("run_status"),
        "chunking_version": (report.get("meta") or {}).get("chunking_version"),
    }


def compare_reports(
    baseline: dict[str, Any] | str,
    candidate: dict[str, Any] | str,
    *,
    decision_k: int = DEFAULT_DECISION_K,
    allow_case_set_difference: bool = False,
    baseline_path: str | None = None,
    candidate_path: str | None = None,
) -> ComparisonResult:
    """Compare deux rapports. Ne compare JAMAIS deux benchmarks distincts en silence."""
    b = load_report(baseline)
    c = load_report(candidate)

    b_info = _report_info(b, baseline_path)
    c_info = _report_info(c, candidate_path)

    def _incompatible(reason: str) -> ComparisonResult:
        return ComparisonResult(
            status="incompatible",
            compatible=False,
            decision_k=decision_k,
            reason=reason,
            baseline_info=b_info,
            candidate_info=c_info,
        )

    b_cases = _index_cases(b)
    c_cases = _index_cases(c)

    if not b_cases or not c_cases:
        return _incompatible("aucun cas exploitable dans l'un des rapports")

    # Compatibilité benchmark (nom/dataset).
    b_id, c_id = _benchmark_identity(b), _benchmark_identity(c)
    if b_id and c_id and b_id != c_id:
        return _incompatible(f"benchmarks distincts: {b_id!r} vs {c_id!r}")

    # Compatibilité chunking_version si présente DES DEUX CÔTÉS.
    b_cv = (b.get("meta") or {}).get("chunking_version")
    c_cv = (c.get("meta") or {}).get("chunking_version")
    if b_cv and c_cv and b_cv != c_cv:
        return _incompatible(f"chunking_version incompatible: {b_cv!r} vs {c_cv!r}")

    # decision_k doit être calculable des deux côtés (rangs ou recall@k présents).
    if not _decision_k_available(b) or not _decision_k_available(c):
        return _incompatible(
            f"decision-k={decision_k} non exploitable: rangs/recall@{decision_k} absents"
        )

    # Un run invalide (références obsolètes / cas invalides) interdit la comparaison.
    if _obsolete_count(b) > 0 or _obsolete_count(c) > 0:
        return _incompatible(
            "références obsolètes/cas invalides présents : régénérer le benchmark "
            f"(baseline={_obsolete_count(b)}, candidat={_obsolete_count(c)})"
        )

    # Alignement des cas par case_id.
    common = sorted(set(b_cases) & set(c_cases))
    missing_in_candidate = sorted(set(b_cases) - set(c_cases))
    new_in_candidate = sorted(set(c_cases) - set(b_cases))

    if (missing_in_candidate or new_in_candidate) and not allow_case_set_difference:
        return _incompatible(
            "jeux de cas différents "
            f"(manquants candidat: {len(missing_in_candidate)}, "
            f"nouveaux candidat: {len(new_in_candidate)}) — "
            "utiliser --allow-case-set-difference pour comparer les cas communs"
        )

    regressions: list[CaseDelta] = []
    improvements: list[CaseDelta] = []
    still_passing: list[CaseDelta] = []
    still_failing: list[CaseDelta] = []
    errors_introduced = 0
    errors_resolved = 0

    for cid in common:
        bc, cc = b_cases[cid], c_cases[cid]
        b_ok = _is_success(bc, decision_k)
        c_ok = _is_success(cc, decision_k)
        b_err = _case_error(bc)
        c_err = _case_error(cc)
        err_intro = b_err is None and c_err is not None
        err_res = b_err is not None and c_err is None
        errors_introduced += 1 if err_intro else 0
        errors_resolved += 1 if err_res else 0

        b_rank = _first_rank(bc)
        c_rank = _first_rank(cc)
        rank_delta = (b_rank - c_rank) if (b_rank is not None and c_rank is not None) else None

        cd = CaseDelta(
            case_id=cid,
            question=str(cc.get("question") or bc.get("question") or ""),
            category="",
            baseline_rank=b_rank,
            candidate_rank=c_rank,
            rank_delta=rank_delta,
            baseline_recall_at_k=_recall_at(bc, decision_k),
            candidate_recall_at_k=_recall_at(cc, decision_k),
            baseline_error=b_err,
            candidate_error=c_err,
            error_introduced=err_intro,
            error_resolved=err_res,
        )

        if b_ok and not c_ok:
            cd.category = "regression"
            regressions.append(cd)
        elif not b_ok and c_ok:
            cd.category = "improvement"
            improvements.append(cd)
        elif b_ok and c_ok:
            cd.category = "still_passing"
            still_passing.append(cd)
        else:
            cd.category = "still_failing"
            still_failing.append(cd)

    metrics = {
        "recall_at_1": _metric_block(b, c, "recall_at_1"),
        "recall_at_3": _metric_block(b, c, "recall_at_3"),
        "recall_at_5": _metric_block(b, c, "recall_at_5"),
        "mrr": _metric_block(b, c, "mrr"),
        "latency_p95_ms": _latency_block(b, c),
        "n_errors": {
            "baseline": _n_errors(b),
            "candidate": _n_errors(c),
            "delta": _n_errors(c) - _n_errors(b),
        },
    }

    summary = {
        "regressions": len(regressions),
        "improvements": len(improvements),
        "still_passing": len(still_passing),
        "still_failing": len(still_failing),
        "errors_introduced": errors_introduced,
        "errors_resolved": errors_resolved,
        "missing_in_candidate": len(missing_in_candidate),
        "new_in_candidate": len(new_in_candidate),
    }

    return ComparisonResult(
        status="completed",
        compatible=True,
        decision_k=decision_k,
        reason="",
        baseline_info=b_info,
        candidate_info=c_info,
        metrics=metrics,
        summary=summary,
        regressions=regressions,
        improvements=improvements,
        still_passing=still_passing,
        still_failing=still_failing,
        missing_in_candidate=missing_in_candidate,
        new_in_candidate=new_in_candidate,
    )


def _decision_k_available(report: dict[str, Any]) -> bool:
    for c in report.get("cases") or []:
        if not isinstance(c, dict):
            continue
        if isinstance(c.get("first_relevant_rank"), int):
            return True
        if isinstance(c.get("recall"), dict) and c["recall"]:
            return True
    # rapport sans cas positifs exploitables : on autorise (deltas agrégés only)
    return True


def _metric_block(b: dict[str, Any], c: dict[str, Any], key: str) -> dict[str, Any]:
    bv, cv = _summary_val(b, key), _summary_val(c, key)
    return {"baseline": bv, "candidate": cv, "delta": _delta(bv, cv)}


def _latency_block(b: dict[str, Any], c: dict[str, Any]) -> dict[str, Any]:
    bv = _summary_val(b, "latency_p95")
    cv = _summary_val(c, "latency_p95")
    delta = _delta(bv, cv)
    delta_pct: float | None = None
    if bv not in (None, 0) and delta is not None:
        delta_pct = round((delta / bv) * 100.0, 2)
    return {"baseline": bv, "candidate": cv, "delta": delta, "delta_percent": delta_pct}


# ---------------------------------------------------------------------------
# Décision CI
# ---------------------------------------------------------------------------


def blocking_reasons(result: ComparisonResult) -> list[str]:
    """Raisons bloquantes pour la CI (liste vide = promouvable)."""
    if not result.compatible:
        return [f"incompatible: {result.reason}"]
    reasons: list[str] = []
    s = result.summary
    if s.get("regressions"):
        reasons.append(f"{s['regressions']} régression(s)")
    if s.get("errors_introduced"):
        reasons.append(f"{s['errors_introduced']} erreur(s) introduite(s)")
    if s.get("missing_in_candidate"):
        reasons.append(f"{s['missing_in_candidate']} cas manquant(s) dans le candidat")
    b_err = result.baseline_info.get("n_errors", 0)
    c_err = result.candidate_info.get("n_errors", 0)
    if c_err > b_err:
        reasons.append(f"n_errors candidat ({c_err}) > baseline ({b_err})")
    return reasons


def exit_code(result: ComparisonResult) -> int:
    """0 = OK ; 1 = régression bloquante ; 2 = entrée invalide/incompatible."""
    if not result.compatible:
        return 2
    return 1 if blocking_reasons(result) else 0


# ---------------------------------------------------------------------------
# Rendu
# ---------------------------------------------------------------------------


def to_json(result: ComparisonResult, *, indent: int = 2) -> str:
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=indent)


def _fmt(v: Any) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def _signed(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.3f}"


def to_markdown(result: ComparisonResult) -> str:
    r = result
    lines: list[str] = ["# Comparaison Benchmark RAG — baseline vs candidat", ""]

    if not r.compatible:
        lines.append(f"## ❌ Incompatible — {r.reason}")
        lines.append("")
        lines.append(f"- baseline : `{r.baseline_info.get('path')}` "
                     f"(benchmark `{r.baseline_info.get('benchmark')}`)")
        lines.append(f"- candidat : `{r.candidate_info.get('path')}` "
                     f"(benchmark `{r.candidate_info.get('benchmark')}`)")
        return "\n".join(lines)

    blk = blocking_reasons(r)
    verdict = "✅ Promouvable" if not blk else "❌ Bloqué"
    lines.append(f"## Verdict : {verdict}")
    if blk:
        lines.append("")
        for b in blk:
            lines.append(f"- ⛔ {b}")
    lines.append("")
    lines.append(f"- decision-k = **{r.decision_k}** (succès = 1er document attendu dans le top-{r.decision_k})")
    lines.append(f"- baseline : `{r.baseline_info.get('path')}` "
                 f"({r.baseline_info.get('n_cases')} cas, {r.baseline_info.get('n_errors')} erreurs)")
    lines.append(f"- candidat : `{r.candidate_info.get('path')}` "
                 f"({r.candidate_info.get('n_cases')} cas, {r.candidate_info.get('n_errors')} erreurs)")
    lines.append("")

    lines.append("## Deltas agrégés  (delta = candidat − baseline)")
    lines.append("")
    lines.append("| Métrique | Baseline | Candidat | Delta |")
    lines.append("| --- | ---: | ---: | ---: |")
    for label, key in (
        ("Recall@1", "recall_at_1"),
        ("Recall@3", "recall_at_3"),
        ("Recall@5", "recall_at_5"),
        ("MRR", "mrr"),
    ):
        m = r.metrics.get(key) or {}
        lines.append(f"| {label} | {_fmt(m.get('baseline'))} | {_fmt(m.get('candidate'))} | {_signed(m.get('delta'))} |")
    lat = r.metrics.get("latency_p95_ms") or {}
    pct = lat.get("delta_percent")
    pct_str = "" if pct is None else f" ({pct:+.1f}%)"
    lines.append(f"| Latence p95 (ms) | {_fmt(lat.get('baseline'))} | {_fmt(lat.get('candidate'))} | {_signed(lat.get('delta'))}{pct_str} |")
    ne = r.metrics.get("n_errors") or {}
    lines.append(f"| n_errors | {ne.get('baseline')} | {ne.get('candidate')} | {ne.get('delta'):+d} |")
    lines.append("")

    s = r.summary
    lines.append("## Résumé des cas")
    lines.append("")
    lines.append("| Catégorie | Nombre |")
    lines.append("| --- | ---: |")
    lines.append(f"| Régressions | {s.get('regressions')} |")
    lines.append(f"| Améliorations | {s.get('improvements')} |")
    lines.append(f"| Toujours réussis | {s.get('still_passing')} |")
    lines.append(f"| Toujours en échec | {s.get('still_failing')} |")
    lines.append(f"| Erreurs introduites | {s.get('errors_introduced')} |")
    lines.append(f"| Erreurs résolues | {s.get('errors_resolved')} |")
    lines.append(f"| Cas manquants dans le candidat | {s.get('missing_in_candidate')} |")
    lines.append(f"| Cas nouveaux dans le candidat | {s.get('new_in_candidate')} |")
    lines.append("")

    lines += _case_table("Régressions", r.regressions)
    lines += _case_table("Améliorations", r.improvements)
    lines += _case_table("Cas toujours en échec", r.still_failing)

    if r.missing_in_candidate:
        lines.append("## Cas manquants dans le candidat")
        lines.append("")
        lines.append(", ".join(f"`{c}`" for c in r.missing_in_candidate))
        lines.append("")
    if r.new_in_candidate:
        lines.append("## Cas nouveaux dans le candidat")
        lines.append("")
        lines.append(", ".join(f"`{c}`" for c in r.new_in_candidate))
        lines.append("")

    return "\n".join(lines)


def _case_table(title: str, cases: list[CaseDelta]) -> list[str]:
    if not cases:
        return []
    out = [f"## {title}", ""]
    out.append("| Case ID | Question | Rang baseline | Rang candidat | Erreur candidat |")
    out.append("| --- | --- | ---: | ---: | --- |")
    for c in cases:
        q = (c.question or "").replace("|", "/")[:60]
        br = c.baseline_rank if c.baseline_rank is not None else "—"
        cr = c.candidate_rank if c.candidate_rank is not None else "—"
        err = (c.candidate_error or "aucune").replace("|", "/")[:40]
        out.append(f"| {c.case_id} | {q} | {br} | {cr} | {err} |")
    out.append("")
    return out
