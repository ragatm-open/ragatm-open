#!/usr/bin/env python3
"""Qualification de bout en bout RAGatm Open Compact — parcours RÉEL via le frontend.

Parcours : authentification (mot de passe + TOTP enrôlé) → bootstrap → création d'espace →
import des fixtures synthétiques → suivi d'ingestion (worker Celery, embeddings réels) →
catalogue de modèles → questions (réponse agrégée ET flux SSE) → citations → suppression /
réimport → isolation inter-espaces et inter-utilisateurs.

Tout passe par le proxy Next (http://127.0.0.1:<port>/api/…), exactement comme le navigateur.
Les cookies sont gérés à la main : le backend pose des cookies `Secure` (profil prod) que le
navigateur accepte sur localhost mais que `http.cookiejar` refuserait sur http.

Sortie : JSON de résultats (--out) + journal lisible. Aucun seuil de qualité inventé : les
observations (termes attendus présents / absents, documents cités, pages) sont consignées.
"""
from __future__ import annotations

import argparse, base64, hashlib, hmac, json, re, struct, sys, time, uuid
from pathlib import Path
import requests

# ----------------------------------------------------------------------------- utilitaires
def totp(secret_b32: str, t: float | None = None, step: int = 30, digits: int = 6) -> str:
    key = base64.b32decode(secret_b32.upper() + "=" * (-len(secret_b32) % 8))
    counter = int((t if t is not None else time.time()) // step)
    mac = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


class Client:
    """Session HTTP minimaliste avec cookies manuels (Secure ignoré) et CSRF double-submit."""

    def __init__(self, base: str, log):
        self.base = base.rstrip("/"); self.cookies: dict[str, str] = {}; self.log = log
        self.s = requests.Session(); self.s.trust_env = False

    def _capture(self, resp: requests.Response) -> None:
        for raw in resp.raw.headers.getlist("Set-Cookie") if hasattr(resp.raw, "headers") else []:
            kv = raw.split(";", 1)[0]
            if "=" in kv:
                k, v = kv.split("=", 1)
                if v == "" or "Max-Age=0" in raw or "expires=Thu, 01 Jan 1970" in raw:
                    self.cookies.pop(k.strip(), None)
                else:
                    self.cookies[k.strip()] = v

    def req(self, method: str, path: str, *, json_body=None, files=None, data=None, stream=False,
            headers=None, timeout=120, expect=None):
        h = {"Accept": "application/json"}
        if headers: h.update(headers)
        if self.cookies: h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        if method not in ("GET", "HEAD", "OPTIONS") and "csrftoken" in self.cookies:
            h["X-CSRFToken"] = self.cookies["csrftoken"]
        h.setdefault("X-Request-ID", f"open4-{uuid.uuid4().hex[:12]}")
        r = self.s.request(method, self.base + path, json=json_body, files=files, data=data,
                           headers=h, stream=stream, timeout=timeout, allow_redirects=False)
        self._capture(r)
        if expect is not None and r.status_code not in (expect if isinstance(expect, (tuple, list)) else (expect,)):
            body = r.text[:400] if not stream else "<stream>"
            raise RuntimeError(f"{method} {path} -> HTTP {r.status_code} (attendu {expect}) : {body}")
        return r


class Report:
    def __init__(self):
        self.steps: list[dict] = []; self.t0 = time.time()

    def add(self, step: str, status: str, **detail):
        entry = {"step": step, "status": status, "t": round(time.time() - self.t0, 1), **detail}
        self.steps.append(entry)
        flag = {"PASS": "✓", "FAIL": "✗", "INFO": "·", "OBSERVED": "◦", "NOT_EXECUTED": "–"}.get(status, "?")
        extra = " ".join(f"{k}={json.dumps(v, ensure_ascii=False)[:160]}" for k, v in detail.items() if k != "raw")
        print(f"[{entry['t']:7.1f}s] {flag} {status:12s} {step}  {extra}", flush=True)

    def dump(self, path: Path, **meta):
        path.write_text(json.dumps({"meta": meta, "steps": self.steps}, ensure_ascii=False, indent=2), encoding="utf-8")

    @property
    def failed(self):
        return [s for s in self.steps if s["status"] == "FAIL"]


# ----------------------------------------------------------------------------- parcours
def login(c: Client, email: str, password: str, rep: Report, *, totp_secret: str | None, label: str):
    """Connexion complète. Enrôle le TOTP si le compte n'en a pas (retourne le secret)."""
    r = c.req("POST", "/api/v1/auth/login/", json_body={"email": email, "password": password}, expect=200)
    body = r.json(); assert body.get("mfa_required") is True, body
    nxt = body.get("next")
    rep.add(f"auth.login[{label}]", "PASS", next=nxt, cookies=sorted(c.cookies))
    assert "ragatm_auth_token" not in c.cookies, "le premier facteur ne doit poser AUCUN cookie d'auth"
    if nxt == "mfa_setup":
        r = c.req("POST", "/api/v1/auth/mfa/setup/", json_body={}, expect=201)
        totp_secret = r.json()["secret"]
        r = c.req("POST", "/api/v1/auth/mfa/setup/confirm/", json_body={"token": totp(totp_secret)}, expect=200)
        rep.add(f"auth.mfa_setup_confirm[{label}]", "PASS", method="totp")
    else:
        assert totp_secret, "secret TOTP requis pour un compte déjà enrôlé"
        r = c.req("POST", "/api/v1/auth/mfa/verify/", json_body={"token": totp(totp_secret)}, expect=200)
        rep.add(f"auth.mfa_verify[{label}]", "PASS")
    assert "ragatm_auth_token" in c.cookies, "cookie d'auth HttpOnly attendu après le second facteur"
    me = c.req("GET", "/api/v1/auth/me/", expect=200).json()
    rep.add(f"auth.me[{label}]", "PASS", email=me.get("email"), superuser=me.get("is_superuser"))
    return totp_secret


def extract_doc_id(body) -> str | None:
    """Identifiant du document d'une réponse d'upload — forme directe ou dédupliquée (`document.id`)."""
    if not isinstance(body, dict):
        return None
    return body.get("document_id") or body.get("id") or ((body.get("document") or {}).get("id") if isinstance(body.get("document"), dict) else None)


def unique_bytes(path, label: str) -> bytes:
    """Contenu distinct à chaque appel : l'upload RAGatm déduplique par sha256 dans un espace."""
    return Path(path).read_bytes() + f"\n\n<!-- compact e2e test nonce {label} {uuid.uuid4().hex} -->\n".encode()


def wait_ready(c: Client, doc_id: str, timeout_s: int, poll: float = 3.0):
    if not doc_id:
        return "NO_DOC", None
    deadline = time.time() + timeout_s; last = None
    while time.time() < deadline:
        r = c.req("GET", f"/api/v1/documents/{doc_id}/", expect=(200, 404))
        if r.status_code == 200:
            last = r.json(); st = str(last.get("status") or "").upper()
            if st in ("READY", "FAILED"):
                return st, last
        time.sleep(poll)
    return "TIMEOUT", last


def parse_sse(resp: requests.Response):
    """Événements SSE (`event:` / `data:`) du flux applicatif RAGatm."""
    event, data = None, []
    for raw in resp.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        if raw == "":
            if event is not None:
                payload = "\n".join(data)
                try: payload = json.loads(payload)
                except Exception: pass
                yield event, payload
            event, data = None, []
            continue
        if raw.startswith("event:"): event = raw[6:].strip()
        elif raw.startswith("data:"): data.append(raw[5:].lstrip())


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).replace(" ", " ").replace(" ", " ").lower()


def ask(c: Client, space_id: str, q: dict, rep: Report, timeout: int):
    """Réponse agrégée + flux SSE ; consigne les observations (aucun seuil inventé)."""
    t = time.time()
    r = c.req("POST", "/api/v1/chat/", json_body={"rag_space_id": space_id, "question": q["question"]},
              timeout=timeout, expect=(200, 500, 502, 503, 504))
    agg_ms = int((time.time() - t) * 1000)
    if r.status_code != 200:
        rep.add(f"chat.answer[{q['id']}]", "FAIL", http=r.status_code, body=r.text[:300]); return None
    body = r.json(); answer = body.get("answer") or body.get("text") or ""
    sources = body.get("sources") or []
    src_names = sorted({str(s.get("filename") or s.get("document_name") or s.get("title") or s.get("document_id")) for s in sources})
    a = norm(answer)
    terms_ok = all(norm(x) in a for x in q.get("expected_answer_terms", []))
    if not terms_ok and q.get("expected_answer_terms_alt"):
        terms_ok = all(norm(x) in a for x in q["expected_answer_terms_alt"])
    docs_ok = all(any(d in n for n in src_names) for d in q.get("expected_docs", []))
    obs = {"latency_ms": agg_ms, "answer_chars": len(answer), "sources": len(sources), "cited_docs": src_names,
           "expected_terms_present": terms_ok if q.get("expected_answer_terms") else None,
           "expected_docs_cited": docs_ok if q.get("expected_docs") else None,
           "answer_excerpt": answer[:240], "model": body.get("model") or body.get("llm_model"),
           "abstained": bool(body.get("abstained") or body.get("no_answer") or body.get("answerability", {}).get("abstain") if isinstance(body.get("answerability"), dict) else body.get("abstained")),
           "raw_keys": sorted(body.keys())}
    pages = {}
    for s in sources:
        n = str(s.get("filename") or s.get("document_name") or ""); p = s.get("page") or s.get("page_number")
        if p is not None: pages.setdefault(n, []).append(p)
    if pages: obs["cited_pages"] = pages
    if q.get("expected_pages"):
        obs["expected_pages_cited"] = all(any(p == exp for p in pages.get(k, [])) for k, exp in q["expected_pages"].items() if any(k in n for n in src_names))
    rep.add(f"chat.answer[{q['id']}]", "OBSERVED", **obs)

    # Flux SSE : meta → sources → token* → done ; exactement un done, pas de duplication.
    t = time.time()
    r = c.req("POST", "/api/v1/chat/stream/", json_body={"rag_space_id": space_id, "question": q["question"]},
              stream=True, timeout=timeout, headers={"Accept": "text/event-stream"}, expect=(200, 500, 502, 503))
    if r.status_code != 200:
        rep.add(f"chat.stream[{q['id']}]", "FAIL", http=r.status_code); return body
    events = []; first_token_ms = None; text = ""
    for ev, payload in parse_sse(r):
        events.append(ev)
        if ev == "token":
            if first_token_ms is None: first_token_ms = int((time.time() - t) * 1000)
            text += (payload.get("delta") if isinstance(payload, dict) else str(payload)) or ""
        if ev in ("done", "error"): break
    total_ms = int((time.time() - t) * 1000)
    counts = {e: events.count(e) for e in sorted(set(events))}
    ok = counts.get("done", 0) == 1 and counts.get("error", 0) == 0 and counts.get("token", 0) >= 1 and events[-1] == "done"
    rep.add(f"chat.stream[{q['id']}]", "PASS" if ok else "FAIL", events=counts, first_token_ms=first_token_ms,
            total_ms=total_ms, streamed_chars=len(text), content_type=r.headers.get("Content-Type"))
    return body


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:18080")
    ap.add_argument("--admin-email", required=True); ap.add_argument("--admin-password", required=True)
    ap.add_argument("--user-b-email"); ap.add_argument("--user-b-password")
    ap.add_argument("--corpus", default=str(Path(__file__).resolve().parent.parent / "fixtures" / "corpus"))
    ap.add_argument("--out", default="e2e-results.json")
    ap.add_argument("--ingest-timeout", type=int, default=900); ap.add_argument("--chat-timeout", type=int, default=600)
    ap.add_argument("--state", default=".e2e-state.json", help="secrets TOTP de la campagne (fichier 0600, jetable)")
    args = ap.parse_args()

    rep = Report(); corpus = Path(args.corpus); manifest = json.loads((corpus / "manifest.json").read_text())
    state_p = Path(args.state); state = json.loads(state_p.read_text()) if state_p.exists() else {}
    def save_state():
        state_p.write_text(json.dumps(state)); state_p.chmod(0o600)

    c = Client(args.base, print)
    # 0) Non authentifié : refus explicite.
    r = c.req("GET", "/api/v1/rag-spaces/"); rep.add("security.unauthenticated_list", "PASS" if r.status_code in (401, 403) else "FAIL", http=r.status_code)
    r = c.req("POST", "/api/v1/chat/", json_body={"rag_space_id": str(uuid.uuid4()), "question": "x"}); rep.add("security.unauthenticated_chat", "PASS" if r.status_code in (401, 403) else "FAIL", http=r.status_code)

    # 1) Authentification A (admin) avec TOTP.
    state["admin_totp"] = login(c, args.admin_email, args.admin_password, rep, totp_secret=state.get("admin_totp"), label="A"); save_state()

    # 2) Bootstrap + espace de qualification.
    b = c.req("POST", "/api/v1/bootstrap/", json_body={}, expect=(200, 201)).json()
    ws_id = b.get("workspace", {}).get("id") if isinstance(b.get("workspace"), dict) else b.get("workspace_id")
    rep.add("bootstrap", "PASS", workspace_id=ws_id, keys=sorted(b.keys()))
    space_name = f"Qualif Compact {time.strftime('%H%M%S')}"
    r = c.req("POST", f"/api/v1/workspaces/{ws_id}/ragspaces/", json_body={"name": space_name}, expect=(200, 201))
    space = r.json(); space_id = space.get("id") or space.get("rag_space", {}).get("id")
    rep.add("ragspace.create", "PASS", id=space_id, llm_model=space.get("llm_model"), embedding_model=space.get("embedding_model"), provider=space.get("embedding_provider"))
    llm = c.req("GET", f"/api/v1/rag-spaces/{space_id}/llm-settings/", expect=200).json()
    rep.add("ragspace.llm_settings", "PASS" if llm.get("llm_model") == "compact-generation-8" and llm.get("embedding_model") == "fr-en-retrieval" else "FAIL", **{k: llm.get(k) for k in ("llm_provider", "llm_model", "embedding_provider", "embedding_model")})

    # 3) Catalogue de modèles (projection du registre Gateway).
    cat = c.req("GET", "/api/v1/ai/model-catalog/", expect=200).json()
    chat_ids = {e["id"]: e for e in cat.get("chat", [])}; emb_ids = {e["id"]: e for e in cat.get("embeddings", [])}
    rep.add("model_catalog", "PASS" if cat.get("default_chat_status") == "available" and emb_ids.get("fr-en-retrieval", {}).get("allowed") else "FAIL",
            default_chat_model=cat.get("default_chat_model"), default_chat_status=cat.get("default_chat_status"),
            chat=sorted(chat_ids), embeddings={k: v.get("allowed") for k, v in emb_ids.items()}, generation_registry=cat.get("generation_registry_available"))
    # Sélection d'un modèle hors registre / profil 12 Go non installé : refus explicite, pas de repli.
    r = c.req("PATCH", f"/api/v1/rag-spaces/{space_id}/", json_body={"llm_model": "llama3.1:8b"}); rep.add("model_selection.unregistered_refused", "PASS" if r.status_code in (400, 409, 422) else "FAIL", http=r.status_code, body=r.text[:200])
    r = c.req("PATCH", f"/api/v1/rag-spaces/{space_id}/", json_body={"llm_model": "compact-generation"}); rep.add("model_selection.not_installed_12gb_refused", "PASS" if r.status_code in (400, 409, 422) else "FAIL", http=r.status_code, body=r.text[:200])
    r = c.req("PATCH", f"/api/v1/rag-spaces/{space_id}/", json_body={"embedding_model": "legacy-768"}); rep.add("embedding_identity.other_profile_refused_or_gated", "PASS" if r.status_code in (400, 409, 422) else "FAIL", http=r.status_code, body=r.text[:200])

    # 4) Import documentaire (asynchrone, worker) + suivi.
    docs = {}
    for d in manifest["documents"]:
        p = corpus / d["name"]
        with p.open("rb") as fh:
            r = c.req("POST", f"/api/v1/documents/upload/?rag_space_id={space_id}", files={"file": (p.name, fh)}, data={"rag_space_id": space_id}, timeout=300, expect=(200, 201, 202))
        body = r.json(); doc_id = body.get("document_id") or body.get("id") or (body.get("document") or {}).get("id")
        docs[d["name"]] = {"id": doc_id, "upload": body}
        rep.add(f"upload[{d['name']}]", "PASS" if doc_id else "FAIL", http=r.status_code, document_id=doc_id, queued=body.get("queued"), state=body.get("status"))
    for name, info in docs.items():
        st, last = wait_ready(c, info["id"], args.ingest_timeout)
        info["status"] = st; info["detail"] = last or {}
        chunks = (last or {}).get("chunk_count") or (last or {}).get("chunks") or ((last or {}).get("stats") or {}).get("chunks")
        rep.add(f"ingest[{name}]", "PASS" if st == "READY" else "FAIL", state=st, chunk_count=chunks, error=(last or {}).get("error") or (last or {}).get("error_message"), keys=sorted((last or {}).keys())[:20])
    stats = c.req("GET", f"/api/v1/rag-spaces/{space_id}/stats/", expect=200).json()
    rep.add("ragspace.stats", "INFO", **{k: stats.get(k) for k in list(stats)[:12]})

    # 5) Recherche hybride seule (sans génération).
    r = c.req("POST", "/api/v1/core/retrieve/", json_body={"rag_space_id": space_id, "question": "profondeur maximale du lac de Vermeil"}, timeout=120, expect=(200, 400, 404))
    if r.status_code == 200:
        rv = r.json(); items = rv.get("chunks") or rv.get("results") or rv.get("passages") or rv.get("sources") or []
        top = [str(i.get("filename") or i.get("document_name") or i.get("document_id"))[:40] for i in items[:5]]
        rep.add("retrieval.hybrid", "PASS" if any("vermeil" in t.lower() for t in top) else "OBSERVED", n=len(items), top=top, keys=sorted(rv.keys())[:15])
    else:
        rep.add("retrieval.hybrid", "INFO", http=r.status_code, body=r.text[:200])

    # 6) Questions documentaires (agrégé + flux).
    answers = {}
    for q in manifest["questions"]:
        answers[q["id"]] = ask(c, space_id, q, rep, args.chat_timeout)

    # 7) Suppression / réimport.
    victim = "fr_lac_aurore.md"; vid = docs[victim]["id"]
    r = c.req("DELETE", f"/api/v1/documents/{vid}/delete/", expect=(200, 202, 204)); rep.add("lifecycle.delete_document", "PASS", http=r.status_code)
    time.sleep(2)
    r = c.req("GET", f"/api/v1/documents/{vid}/"); rep.add("lifecycle.deleted_document_gone", "PASS" if r.status_code in (404, 410) else "OBSERVED", http=r.status_code, state=(r.json().get("status") if r.status_code == 200 else None))
    qa = next(q for q in manifest["questions"] if q["id"] == "Q01-factuelle-fr")
    r = c.req("POST", "/api/v1/core/retrieve/", json_body={"rag_space_id": space_id, "question": "température maximale du lac Aurore en 2025 station Est"}, timeout=120, expect=(200, 400, 404))
    if r.status_code == 200:
        items = r.json().get("chunks") or r.json().get("results") or []
        rep.add("lifecycle.deleted_not_retrievable", "PASS" if not any("aurore" in str(i.get("filename") or i.get("document_name") or "").lower() for i in items) else "FAIL", n=len(items))
    with (corpus / victim).open("rb") as fh:
        r = c.req("POST", f"/api/v1/documents/upload/?rag_space_id={space_id}", files={"file": (victim, fh)}, timeout=300, expect=(200, 201, 202))
    new_id = r.json().get("document_id") or r.json().get("id"); st, last = wait_ready(c, new_id, args.ingest_timeout)
    rep.add("lifecycle.reimport_same_file", "PASS" if st == "READY" and new_id != vid else "FAIL", new_document_id=new_id, state=st)
    # même fichier dans un autre espace
    r = c.req("POST", f"/api/v1/workspaces/{ws_id}/ragspaces/", json_body={"name": space_name + " bis"}, expect=(200, 201)); space2 = r.json().get("id")
    with (corpus / victim).open("rb") as fh:
        r = c.req("POST", f"/api/v1/documents/upload/?rag_space_id={space2}", files={"file": (victim, fh)}, timeout=300, expect=(200, 201, 202))
    id2 = r.json().get("document_id") or r.json().get("id"); st2, _ = wait_ready(c, id2, args.ingest_timeout)
    rep.add("lifecycle.same_file_other_space", "PASS" if st2 == "READY" and id2 not in (vid, new_id) else "FAIL", document_id=id2, state=st2)
    # suppression de l'espace bis puis recréation du même nom : aucune résurgence
    r = c.req("DELETE", f"/api/v1/rag-spaces/{space2}/", expect=(200, 202, 204)); rep.add("lifecycle.delete_space", "PASS", http=r.status_code)
    time.sleep(3)
    r = c.req("POST", f"/api/v1/workspaces/{ws_id}/ragspaces/", json_body={"name": space_name + " bis"}, expect=(200, 201)); space3 = r.json().get("id")
    st3 = c.req("GET", f"/api/v1/rag-spaces/{space3}/stats/", expect=200).json()
    docs3 = c.req("GET", f"/api/v1/documents/?rag_space_id={space3}", expect=200).json()
    n3 = docs3.get("count") if isinstance(docs3, dict) else len(docs3)
    rep.add("lifecycle.recreated_space_is_empty", "PASS" if (n3 in (0, None)) and space3 not in (space2,) else "FAIL", new_space_id=space3, documents=n3, stats={k: st3.get(k) for k in list(st3)[:6]})
    r = c.req("GET", f"/api/v1/documents/{id2}/"); rep.add("lifecycle.old_space_document_gone", "PASS" if r.status_code in (404, 410, 403) else "FAIL", http=r.status_code)

    # 8) Isolation utilisateur B (sans droits sur l'espace A).
    if args.user_b_email:
        cb = Client(args.base, print)
        state["b_totp"] = login(cb, args.user_b_email, args.user_b_password, rep, totp_secret=state.get("b_totp"), label="B"); save_state()
        spaces_b = cb.req("GET", "/api/v1/rag-spaces/", expect=200).json()
        ids_b = {s.get("id") for s in (spaces_b if isinstance(spaces_b, list) else spaces_b.get("results") or spaces_b.get("items") or [])}
        rep.add("isolation.B_does_not_list_A_space", "PASS" if space_id not in ids_b else "FAIL", spaces_b=len(ids_b))
        r = cb.req("POST", "/api/v1/chat/", json_body={"rag_space_id": space_id, "question": qa["question"]}, timeout=60); rep.add("isolation.B_chat_on_A_space_refused", "PASS" if r.status_code in (403, 404) else "FAIL", http=r.status_code)
        r = cb.req("POST", "/api/v1/core/retrieve/", json_body={"rag_space_id": space_id, "question": qa["question"]}, timeout=60); rep.add("isolation.B_retrieve_on_A_space_refused", "PASS" if r.status_code in (403, 404) else "FAIL", http=r.status_code)
        any_doc = docs["fr_lac_vermeil.txt"]["id"]
        r = cb.req("GET", f"/api/v1/documents/{any_doc}/"); rep.add("isolation.B_document_detail_refused", "PASS" if r.status_code in (403, 404) else "FAIL", http=r.status_code)
        r = cb.req("GET", f"/api/v1/documents/{any_doc}/download/"); rep.add("isolation.B_document_download_refused", "PASS" if r.status_code in (403, 404) else "FAIL", http=r.status_code)
        r = cb.req("GET", f"/api/v1/documents/search/?q=Vermeil", expect=(200, 400, 403, 404))
        hits = r.json() if r.status_code == 200 else {}
        n_hits = hits.get("count") if isinstance(hits, dict) else len(hits)
        rep.add("isolation.B_search_no_A_results", "PASS" if not n_hits else "FAIL", http=r.status_code, hits=n_hits)
        r = cb.req("POST", f"/api/v1/documents/upload/?rag_space_id={space_id}", files={"file": ("x.txt", b"intrusion")}); rep.add("isolation.B_upload_to_A_refused", "PASS" if r.status_code in (403, 404) else "FAIL", http=r.status_code)
        r = cb.req("GET", f"/api/v1/rag-spaces/{space_id}/stats/"); rep.add("isolation.B_stats_refused", "PASS" if r.status_code in (403, 404) else "FAIL", http=r.status_code)
    else:
        rep.add("isolation.user_B", "NOT_EXECUTED", reason="--user-b-email absent")

    # 9) Capacités optionnelles OFF.
    caps = c.req("GET", "/api/v1/capabilities/", expect=200).json()
    rep.add("capabilities.asr_vision_off", "INFO", **{k: caps.get(k) for k in list(caps)[:16]})
    r = c.req("POST", f"/api/v1/documents/upload-audio/?rag_space_id={space_id}", files={"file": ("a.wav", b"RIFF\x00\x00\x00\x00WAVEfmt ")}); rep.add("capabilities.audio_upload_refused", "PASS" if r.status_code in (400, 403, 404, 415, 422) else "FAIL", http=r.status_code, body=r.text[:160])

    rep.dump(Path(args.out), base=args.base, corpus_version=manifest["version"], space_id=space_id, documents={k: v["id"] for k, v in docs.items()}, failed=len(rep.failed))
    print(f"\n{len(rep.steps)} étapes, {len(rep.failed)} FAIL -> {args.out}")
    sys.exit(1 if rep.failed else 0)


if __name__ == "__main__":
    main()
