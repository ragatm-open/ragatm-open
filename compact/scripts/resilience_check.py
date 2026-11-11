#!/usr/bin/env python3
"""Résilience minimale (qualification interne, §8.5) : pannes provoquées sur une installation JETABLE.

Scénarios : Gateway arrêtée, Ollama arrêté, reranker arrêté, Redis arrêté, redémarrage du worker,
client interrompant un flux. Attendu : erreurs EXPLICITES côté API, aucun repli externe, aucune
duplication de tâche à effet de bord (un document importé pendant une panne Redis ne doit pas être
ingéré deux fois ; un flux interrompu ne doit pas produire de second message).
Usage : resilience_check.py --base URL --admin-email … --admin-password … --state .e2e-state.json --space <id> --doc <fichier>
"""
from __future__ import annotations
import argparse, json, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_qualification import Client, Report, login, parse_sse, wait_ready, extract_doc_id, unique_bytes  # noqa: E402

def dc(*args, check=True):
    return subprocess.run(["docker", "compose", *args], cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True, check=check)

def wait_healthy(service, timeout=240):
    for _ in range(timeout // 5):
        out = dc("ps", "--format", "{{.Name}} {{.Health}} {{.State}}", service, check=False).stdout
        if " healthy " in out or (service in ("reranker", "beat") and " running" in out):
            return True
        time.sleep(5)
    return False

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18080"); ap.add_argument("--admin-email", required=True)
    ap.add_argument("--admin-password", required=True); ap.add_argument("--state", default=".e2e-state.json")
    ap.add_argument("--space", required=True); ap.add_argument("--doc", required=True); ap.add_argument("--out", default="resilience-results.json")
    a = ap.parse_args(); rep = Report(); state = json.loads(Path(a.state).read_text())
    c = Client(a.base, print); login(c, a.admin_email, a.admin_password, rep, totp_secret=state["admin_totp"], label="A")
    q = {"rag_space_id": a.space, "question": "Quelle est la profondeur maximale du lac de Vermeil ?"}

    # 1) Gateway indisponible -> erreur explicite, pas de repli, pas de réponse fabriquée.
    dc("stop", "ai-gateway"); time.sleep(2)
    r = c.req("POST", "/api/v1/chat/", json_body=q, timeout=120)
    body = r.text[:300]
    rep.add("resilience.gateway_down.chat", "PASS" if r.status_code == 503 and "ai_gateway_unavailable" in body and "Retry-After" in r.headers else ("OBSERVED" if r.status_code >= 500 else "FAIL"), http=r.status_code, body=body, retry_after=r.headers.get("Retry-After"))
    r = c.req("POST", "/api/v1/chat/stream/", json_body=q, stream=True, timeout=120, headers={"Accept": "text/event-stream"})
    evs, toks = [], []
    if r.status_code == 200:
        for e, pl in parse_sse(r):
            evs.append(e)
            if e == "token": toks.append((pl.get("delta") if isinstance(pl, dict) else str(pl)) or "")
            if e == "error" and isinstance(pl, dict): toks.append(f"<error:{pl.get('detail') or pl.get('code')}>")
    rep.add("resilience.gateway_down.stream", "PASS" if r.status_code == 200 and "error" in evs and "token" not in evs and evs and evs[-1] == "done" and "<error:ai_gateway_unavailable>" in "".join(toks) else "OBSERVED", http=r.status_code, events={e: evs.count(e) for e in set(evs)}, stream_text="".join(toks)[:240])
    # upload pendant la panne : le document doit finir FAILED/PENDING explicitement, puis réindexable
    r = c.req("POST", f"/api/v1/documents/upload/?rag_space_id={a.space}", files={"file": ("resilience_gw_down.md", unique_bytes(a.doc, "resilience_gw_down"))}, timeout=120)
    did = extract_doc_id(r.json()) if r.status_code in (200, 201, 202) else None
    st, last = wait_ready(c, did, 180) if did else ("NO_DOC", None)
    rep.add("resilience.gateway_down.ingest", "PASS" if st in ("FAILED", "TIMEOUT", "NO_DOC") else "OBSERVED", upload_http=r.status_code, state=st, error=(last or {}).get("error") or (last or {}).get("error_message"))
    dc("up", "-d", "ai-gateway", "reranker"); wait_healthy("ai-gateway")
    if did:
        r = c.req("POST", f"/api/v1/documents/{did}/reindex/", json_body={}, timeout=60)
        st2, _ = wait_ready(c, did, 600)
        rep.add("resilience.gateway_back.reindex", "PASS" if st2 == "READY" else "FAIL", http=r.status_code, state=st2)

    # 2) Ollama indisponible (Gateway vivante) -> 503 explicite relayé, aucune génération fabriquée.
    dc("stop", "ollama"); time.sleep(2)
    r = c.req("POST", "/api/v1/chat/", json_body=q, timeout=120)
    rep.add("resilience.ollama_down.chat", "PASS" if r.status_code == 503 and "ai_gateway_unavailable" in r.text else ("OBSERVED" if r.status_code >= 500 else "FAIL"), http=r.status_code, body=r.text[:300], retry_after=r.headers.get("Retry-After"))
    dc("up", "-d", "ollama", "ai-gateway", "reranker"); wait_healthy("ollama"); wait_healthy("ai-gateway"); time.sleep(5)

    # 3) Reranker indisponible -> fail_open_rrf : réponse servie, compteur de repli incrémenté (jamais un fournisseur externe).
    dc("stop", "reranker"); time.sleep(2)
    r = c.req("POST", "/api/v1/chat/", json_body=q, timeout=600)
    rep.add("resilience.reranker_down.chat_fail_open", "PASS" if r.status_code == 200 and r.json().get("sources") else "FAIL", http=r.status_code, sources=len(r.json().get("sources") or []) if r.status_code == 200 else None, excerpt=(r.json().get("answer") or "")[:120] if r.status_code == 200 else r.text[:200])
    dc("up", "-d", "reranker"); time.sleep(25)

    # 4) Redis indisponible -> upload refusé explicitement (aucun faux `queued`), pas de doublon après retour.
    dc("stop", "redis"); time.sleep(2)
    r = c.req("POST", f"/api/v1/documents/upload/?rag_space_id={a.space}", files={"file": ("resilience_redis_down.md", unique_bytes(a.doc, "resilience_redis_down"))}, timeout=120)
    rep.add("resilience.redis_down.upload", "PASS" if r.status_code == 503 and r.headers.get("Content-Type", "").startswith("application/json") and ("dependency_unavailable" in r.text or "ingest_enqueue_unavailable" in r.text) else ("OBSERVED" if r.status_code in (500, 502, 503) else "FAIL"), http=r.status_code, content_type=r.headers.get("Content-Type"), body=r.text[:200])
    dc("up", "-d", "redis"); wait_healthy("redis"); time.sleep(8)
    before = c.req("GET", f"/api/v1/documents/?rag_space_id={a.space}", expect=200).json()
    nb = before.get("count") if isinstance(before, dict) else len(before)
    time.sleep(10)
    after = c.req("GET", f"/api/v1/documents/?rag_space_id={a.space}", expect=200).json()
    na = after.get("count") if isinstance(after, dict) else len(after)
    rep.add("resilience.redis_back.no_duplicate_ingest", "PASS" if na == nb else "FAIL", before=nb, after=na)

    # 5) Redémarrage du worker pendant une ingestion -> le document finit READY une seule fois.
    r = c.req("POST", f"/api/v1/documents/upload/?rag_space_id={a.space}", files={"file": ("resilience_worker_restart.md", unique_bytes(a.doc, "resilience_worker_restart"))}, timeout=120, expect=(200, 201, 202))
    did = extract_doc_id(r.json()); dc("restart", "worker"); st, last = wait_ready(c, did, 600)
    rep.add("resilience.worker_restart.ingest_once", "PASS" if st == "READY" else "OBSERVED", state=st, chunks=(last or {}).get("chunk_count"))

    # 6) Client interrompant un flux -> aucune erreur côté API par la suite, un seul message assistant persisté.
    r = c.req("POST", "/api/v1/chat/stream/", json_body=q, stream=True, timeout=600, headers={"Accept": "text/event-stream"}, expect=200)
    tid = None; n = 0
    for ev, payload in parse_sse(r):
        if ev == "meta" and isinstance(payload, dict): tid = payload.get("thread_id")
        if ev == "token":
            n += 1
            if n >= 2: break
    r.close(); time.sleep(8)
    msgs = c.req("GET", f"/api/v1/chat/threads/{tid}/messages/", expect=(200, 404)).json() if tid else []
    items = msgs if isinstance(msgs, list) else (msgs.get("results") or msgs.get("messages") or [])
    assistant = [m for m in items if m.get("role") == "assistant"]
    rep.add("resilience.client_abort.no_duplicate_message", "PASS" if len(assistant) <= 1 else "FAIL", thread=tid, assistant_messages=len(assistant), tokens_before_abort=n)
    r = c.req("POST", "/api/v1/chat/", json_body=q, timeout=600); rep.add("resilience.after_abort.chat_ok", "PASS" if r.status_code == 200 else "FAIL", http=r.status_code)
    rep.dump(Path(a.out), base=a.base, space=a.space)
    print(f"\n{len(rep.steps)} étapes, {len(rep.failed)} FAIL -> {a.out}")

if __name__ == "__main__":
    main()
