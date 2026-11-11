"""Collecteur d'observation owner_scope (agrégation OFFLINE de logs staging).

Lit des logs JSONL structurés (événements `owner_scope.audit` : `ragspace_decision`,
`ragspace_list_decision`, `task_revalidation`) et produit un rapport JSON stable.
**Lecture seule, aucun accès base**, déterministe, idempotent. **Aucune donnée
métier** n'est lue ni réémise : seuls des identifiants, rôles, décisions et raisons
(liste blanche de champs) sont agrégés.

Extraction staging (à adapter au format réel du logger) :
    docker compose logs --no-color backend celery_worker \\
      | grep 'owner_scope_audit' > owner_scope_staging.jsonl

Usage :
    python manage.py owner_scope_observation --input staging.jsonl --output rapport.json \\
      --fail-on-would-allow --fail-on-context-missing --min-request-id-coverage 0.95
"""
from __future__ import annotations

import json
import sys

from django.core.management.base import BaseCommand, CommandError

# Champs autorisés par type d'événement (liste blanche anti-fuite : tout autre
# champ du log est IGNORÉ et jamais réémis).
_WHITELIST = {
    "ragspace_decision": {
        "event", "endpoint", "request_id", "user_id", "workspace_id", "owner_scope_id",
        "rag_space_id", "permission", "workspace_role", "space_role",
        "legacy_decision", "target_decision", "audit", "reason", "timestamp",
    },
    "ragspace_list_decision": {
        "event", "endpoint", "request_id", "user_id", "workspace_id",
        "legacy_count", "target_count", "legacy_only_count", "target_only_count",
        "would_hide_count", "timestamp",
    },
    "task_revalidation": {
        "event", "job_label", "request_id", "user_id", "rag_space_id",
        "required_permission", "decision", "reason", "timestamp",
    },
}

# Surfaces attendues (sinon « silencieuses »).
EXPECTED_ENDPOINTS = {
    "ragspaces.list", "ragspaces.stats", "ragspaces.patch", "ragspaces.delete",
    "documents.access", "documents.list", "documents.search", "documents.ingest",
    "documents.upload", "chat.sync", "chat.stream", "chat.threads.list",
    "chat.threads.create", "core.retrieve", "core.document_status",
}
EXPECTED_TASKS = {
    "document_summary", "ragspace_reindex", "corpus_analysis",
    "rephrasing_suggestions", "ingest_document_task", "reindex_document_task",
}

# would_deny : distinguer attendu (avant backfill ACL) d'anormal (à investiguer).
WOULD_DENY_EXPECTED = {"owner_scope_mismatch", "access_missing", "not_member"}
WOULD_DENY_ANOMALOUS = {
    "membership_missing", "ragspace_without_scope", "workspace_disabled",
    "owner_scope_transferred", "access_revoked", "ragspace_deleted", "actor_inactive",
}


class Command(BaseCommand):
    help = "Agrège des logs owner_scope.audit (offline) en un rapport JSON."

    def add_arguments(self, parser):
        parser.add_argument("--input", required=True, help="Fichier JSONL de logs.")
        parser.add_argument("--output", default=None, help="Fichier JSON de sortie (défaut: stdout).")
        parser.add_argument("--since", default=None, help="ISO min (inclus) si les events ont un timestamp.")
        parser.add_argument("--until", default=None, help="ISO max (exclus) si les events ont un timestamp.")
        parser.add_argument("--fail-on-would-allow", action="store_true")
        parser.add_argument("--fail-on-context-missing", action="store_true")
        parser.add_argument("--fail-on-silent-surface", action="store_true")
        parser.add_argument("--max-would-deny-rate", type=float, default=None)
        parser.add_argument("--min-request-id-coverage", type=float, default=None)

    def handle(self, *args, **opts):
        try:
            with open(opts["input"], "r") as fh:
                lines = fh.read().splitlines()
        except OSError as e:
            raise CommandError(f"lecture impossible: {e}")

        report = aggregate(
            lines, since=opts["since"], until=opts["until"],
        )
        payload = json.dumps(report, indent=2, sort_keys=True, default=str)
        if opts["output"]:
            with open(opts["output"], "w") as fh:
                fh.write(payload)
            self.stdout.write(f"Rapport écrit : {opts['output']}")
        else:
            self.stdout.write(payload)

        if _gate_failed(report, opts):
            sys.exit(1)


# --- Extraction / parsing ----------------------------------------------------

def _extract_payload(line: str) -> dict | None:
    """Renvoie le payload d'événement d'une ligne, ou None si non pertinent/invalide.

    Tolère : (a) ligne brute `... owner_scope_audit {json}` ; (b) JSON structuré
    avec un champ `message` contenant le marqueur ; (c) payload JSON direct.
    """
    line = (line or "").strip()
    if not line:
        return None
    marker = "owner_scope_audit "
    # 1) JSON structuré d'abord (évite de mal découper un marqueur présent DANS
    #    une chaîne "message" échappée).
    try:
        obj = json.loads(line)
        if isinstance(obj, dict):
            if "event" in obj:
                return obj
            msg = obj.get("message")
            if isinstance(msg, str) and marker in msg:
                return _extract_payload(msg)
        return None
    except json.JSONDecodeError:
        pass
    # 2) Ligne brute logfmt : `... owner_scope_audit {json}`.
    if marker in line:
        frag = line[line.rindex(marker) + len(marker):]
        try:
            obj = json.loads(frag)
            return obj if isinstance(obj, dict) and "event" in obj else None
        except json.JSONDecodeError:
            return None
    return None


def _sanitize(payload: dict) -> dict | None:
    ev = payload.get("event")
    wl = _WHITELIST.get(ev)
    if wl is None:
        return None  # événement inconnu : ignoré
    return {k: payload.get(k) for k in wl if k in payload}


def _in_period(payload: dict, since, until) -> bool:
    if not since and not until:
        return True
    ts = payload.get("timestamp")
    if ts is None:
        return True  # pas de timestamp -> jamais filtré (tolérant)
    s = str(ts)
    if since and s < str(since):
        return False
    if until and s >= str(until):
        return False
    return True


# --- Agrégation --------------------------------------------------------------

def aggregate(lines, *, since=None, until=None) -> dict:
    def bump(d, k):
        d[k] = d.get(k, 0) + 1

    totals = {}
    by_endpoint, by_reason, by_role, by_scope, by_task = {}, {}, {}, {}, {}
    http_vs_task = {"http": 0, "task": 0, "list": 0}
    would_deny_breakdown = {"expected": {}, "anomalous": {}, "other": {}}
    context_missing, would_allow_samples, would_deny_samples = [], [], []
    rid_with = rid_without = 0
    parse_errors = 0
    observed_endpoints, observed_tasks = set(), set()
    seen = set()  # dédup (request_id, event, endpoint/job_label)
    dedup_removed = 0
    period_kept = 0

    for raw in lines:
        payload = _extract_payload(raw)
        if payload is None:
            if (raw or "").strip():
                parse_errors += 1
            continue
        clean = _sanitize(payload)
        if clean is None:
            continue
        if not _in_period(clean, since, until):
            continue
        period_kept += 1

        ev = clean["event"]
        surface = clean.get("endpoint") or clean.get("job_label")
        rid = clean.get("request_id")

        # Déduplication déterministe quand request_id présent.
        if rid:
            key = (rid, ev, surface)
            if key in seen:
                dedup_removed += 1
                continue
            seen.add(key)

        # Couverture request_id.
        if rid:
            rid_with += 1
        else:
            rid_without += 1

        if ev == "ragspace_decision":
            http_vs_task["http"] += 1
            observed_endpoints.add(surface)
            klass = clean.get("audit") or "unknown"
            bump(totals, klass)
            by_endpoint.setdefault(surface, {}); bump(by_endpoint[surface], klass)
            role = clean.get("workspace_role") or "unknown"
            by_role.setdefault(role, {}); bump(by_role[role], klass)
            scope = str(clean.get("owner_scope_id"))
            by_scope.setdefault(scope, {}); bump(by_scope[scope], klass)
            reason = clean.get("reason")
            if klass in ("would_deny", "would_allow") and reason:
                bump(by_reason, reason)
            if klass == "would_deny" and reason:
                bucket = ("expected" if reason in WOULD_DENY_EXPECTED
                          else "anomalous" if reason in WOULD_DENY_ANOMALOUS else "other")
                bump(would_deny_breakdown[bucket], reason)
                if len(would_deny_samples) < 200:
                    would_deny_samples.append(_sample(clean, ("endpoint", "workspace_role", "space_role",
                                                              "permission", "owner_scope_id", "rag_space_id", "reason")))
            if klass == "would_allow":
                would_allow_samples.append(_sample(clean, ("endpoint", "workspace_role", "space_role",
                                                            "permission", "owner_scope_id", "rag_space_id", "reason")))
        elif ev == "ragspace_list_decision":
            http_vs_task["list"] += 1
            observed_endpoints.add(surface)
            bump(totals, "list_event")
            by_endpoint.setdefault(surface, {}); bump(by_endpoint[surface], "list_event")
        elif ev == "task_revalidation":
            http_vs_task["task"] += 1
            observed_tasks.add(surface)
            decision = "task_" + (clean.get("decision") or "unknown")
            bump(totals, decision)
            by_task.setdefault(surface, {}); bump(by_task[surface], clean.get("decision") or "unknown")
            reason = clean.get("reason")
            if clean.get("decision") == "deny" and reason:
                bump(by_reason, reason)
            if reason == "context_missing":
                context_missing.append(_sample(clean, ("job_label", "required_permission",
                                                        "rag_space_id"), rid_present=bool(rid)))

    total_events = rid_with + rid_without
    rid_rate = (rid_with / total_events) if total_events else 1.0
    decisions = sum(totals.get(k, 0) for k in ("matched_allow", "matched_deny", "would_allow", "would_deny"))
    would_deny_rate = (totals.get("would_deny", 0) / decisions) if decisions else 0.0

    silent = sorted((EXPECTED_ENDPOINTS - observed_endpoints) | (EXPECTED_TASKS - observed_tasks))

    findings = _build_findings(totals, would_allow_samples, context_missing,
                               would_deny_breakdown, silent, rid_rate)

    return {
        "period": {"since": since, "until": until, "events_in_period": period_kept} if (since or until) else {},
        "totals": totals,
        "http_vs_task": http_vs_task,
        "by_endpoint": by_endpoint,
        "by_reason": by_reason,
        "by_role": by_role,
        "by_owner_scope": by_scope,
        "by_task": by_task,
        "would_deny_breakdown": would_deny_breakdown,
        "would_deny": would_deny_samples,
        "would_allow": would_allow_samples,
        "context_missing": context_missing,
        "silent_surfaces": silent,
        "request_id_coverage": {"with": rid_with, "without": rid_without, "rate": round(rid_rate, 4)},
        "would_deny_rate": round(would_deny_rate, 4),
        "parse_errors": parse_errors,
        "dedup_removed": dedup_removed,
        "findings": findings,
    }


def _sample(clean: dict, keys, **extra) -> dict:
    out = {k: clean.get(k) for k in keys}
    out.update(extra)
    return out


def _build_findings(totals, would_allow, context_missing, wdb, silent, rid_rate) -> list:
    f = []
    if totals.get("would_allow"):
        f.append({"severity": "high", "kind": "would_allow",
                  "detail": f"{totals['would_allow']} accès où la cible est PLUS permissive que le legacy "
                            "(erreur d'ACL/rôle possible)."})
    if context_missing:
        f.append({"severity": "high", "kind": "context_missing",
                  "detail": f"{len(context_missing)} tâches sans acteur identifiable (refusées si ENABLED)."})
    if wdb["anomalous"]:
        f.append({"severity": "medium", "kind": "would_deny_anomalous",
                  "detail": f"would_deny anormaux (hors attendu pré-backfill): {wdb['anomalous']}"})
    if silent:
        f.append({"severity": "medium", "kind": "silent_surface",
                  "detail": f"surfaces attendues jamais observées: {silent}"})
    if rid_rate < 1.0:
        f.append({"severity": "low", "kind": "request_id_coverage",
                  "detail": f"couverture request_id = {round(rid_rate, 4)}"})
    return f


def _gate_failed(report, opts) -> bool:
    fails = []
    if opts["fail_on_would_allow"] and report["totals"].get("would_allow"):
        fails.append("would_allow")
    if opts["fail_on_context_missing"] and report["context_missing"]:
        fails.append("context_missing")
    if opts["fail_on_silent_surface"] and report["silent_surfaces"]:
        fails.append("silent_surface")
    if opts["max_would_deny_rate"] is not None and report["would_deny_rate"] > opts["max_would_deny_rate"]:
        fails.append("would_deny_rate")
    if opts["min_request_id_coverage"] is not None and report["request_id_coverage"]["rate"] < opts["min_request_id_coverage"]:
        fails.append("request_id_coverage")
    return bool(fails)
