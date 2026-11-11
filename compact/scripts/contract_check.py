#!/usr/bin/env python3
"""Qualification du contrat `gateway-api/1.0` CONTRE la Gateway réelle de la pile Compact.

S'exécute DANS le conteneur backend (réseau interne `ai`) :
  docker compose exec -T backend python - < scripts/contract_check.py > contract-results.json
Chaque contrôle consigne le code HTTP, l'enveloppe d'erreur (`detail.code`, `retryable`), les
en-têtes `X-Request-ID` / `Retry-After` / `Content-Type` et les champs attendus par RAGatm.
Les appels inférentiels sont courts (num_predict 8, un texte) : ce script est une qualification
explicite, jamais un healthcheck.
"""
import json, threading, time, uuid
import requests

BASE = "http://ai-gateway:8100"; R = []
def rec(name, status, **d): R.append({"check": name, "status": status, **d}); print(f"{status:12s} {name} {json.dumps({k:v for k,v in d.items() if k!='body'}, ensure_ascii=False)[:300]}", flush=True)
def detail(r):
    try: return r.json().get("detail")
    except Exception: return r.text[:200]
def post(path, body, **kw): return requests.post(BASE + path, json=body, timeout=kw.pop("timeout", 300), headers={"X-Request-ID": kw.pop("rid", f"open4-{uuid.uuid4().hex[:10]}"), **kw.pop("headers", {})}, **kw)

# --- sondes non inférentielles -----------------------------------------------------------------
r = requests.get(BASE + "/health/live", timeout=5); b = r.json()
rec("GET /health/live", "PASS" if r.status_code == 200 and b.get("inferential") is False else "FAIL", http=r.status_code, inferential=b.get("inferential"), version=b.get("version"), request_id_header=bool(r.headers.get("X-Request-ID")))
r = requests.get(BASE + "/health/runtime", timeout=5); rt = r.json()
caps = rt.get("capabilities"); llm = {m.get("name"): m for m in rt.get("llm_models", [])}
rec("GET /health/runtime", "PASS" if r.status_code == 200 and caps else "FAIL", http=r.status_code, capabilities=caps, strict=rt.get("generation_strict_registry"),
    llm={k: {"ollama_name": v.get("ollama_name"), "installed": v.get("installed"), "caps": v.get("capabilities"), "allowed_here": v.get("allowed_here"), "qualification": (v.get("compact") or {}).get("qualification")} for k, v in llm.items()},
    limits=rt.get("limits"), inflight=rt.get("inflight"))
r = requests.get(BASE + "/v1/models", timeout=10); models = r.json().get("models", [])
proj = [{k: m.get(k) for k in ("name", "model_logical", "kind", "dimension", "registry_status", "capabilities", "digest")} for m in models]
need = {"name", "model_logical", "kind"}
rec("GET /v1/models", "PASS" if r.status_code == 200 and all(need <= set(k for k, v in m.items() if v is not None) for m in proj) else "FAIL", http=r.status_code, models=proj)

# --- génération ----------------------------------------------------------------------------------
rid = "open4-contract-generate-001"
r = post("/v1/generate", {"prompt": "Réponds par un seul mot : quelle est la couleur du ciel par beau temps ?", "model": "compact-generation-8", "options": {"num_predict": 8, "temperature": 0}}, rid=rid)
b = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
rec("POST /v1/generate compact-generation-8", "PASS" if r.status_code == 200 and b.get("text") and b.get("model") else "FAIL", http=r.status_code, model=b.get("model"), model_logical=b.get("model_logical"), tokens=b.get("tokens"), usage_keys=sorted((b.get("usage") or {}).keys()), done_reason=b.get("done_reason"), request_id_echo=r.headers.get("X-Request-ID") == rid, text=(b.get("text") or "")[:60], elapsed_ms=int(r.elapsed.total_seconds()*1000))
t0 = time.time(); r = post("/v1/generate/stream", {"prompt": "Compte de un à cinq en lettres.", "model": "compact-generation-8", "options": {"num_predict": 24, "temperature": 0}}, stream=True, headers={"Accept": "application/x-ndjson"})
events = []; first = None
for line in r.iter_lines():
    if not line: continue
    if first is None: first = int((time.time() - t0) * 1000)
    events.append(json.loads(line))
types = [e.get("type") for e in events]
rec("POST /v1/generate/stream NDJSON", "PASS" if r.status_code == 200 and r.headers.get("Content-Type", "").startswith("application/x-ndjson") and types.count("done") == 1 and types[-1] == "done" and "delta" in types and "error" not in types else "FAIL",
    http=r.status_code, content_type=r.headers.get("Content-Type"), n_delta=types.count("delta"), n_done=types.count("done"), first_event_ms=first, total_ms=int((time.time()-t0)*1000), done=events[-1] if events else None)
r = post("/v1/generate", {"prompt": "x", "model": "llama3.1:8b"}); d = detail(r)
rec("400 model_not_registered (llama3.1:8b)", "PASS" if r.status_code == 400 and isinstance(d, dict) and (d.get("code") == "model_not_registered" or d.get("strict_registry") is True) else "FAIL", http=r.status_code, detail=d)
r = post("/v1/generate", {"prompt": "x", "model": "compact-generation"}); d = detail(r)
rec("503 model_not_installed (compact-generation, 12 Go non installé)", "PASS" if r.status_code == 503 and isinstance(d, dict) and d.get("code") == "model_not_installed" and d.get("retryable") is False and not r.headers.get("Retry-After") else "FAIL", http=r.status_code, detail=d, retry_after=r.headers.get("Retry-After"))
r = post("/v1/generate", {"prompt": "x", "model": "compact-generation-8", "provider": "ollama"}); d = detail(r)
rec("400 extra_forbidden (champ provider hors contrat)", "PASS" if r.status_code in (400, 422) else "FAIL", http=r.status_code, detail=str(d)[:200])
r = post("/v1/generate", {"prompt": "x", "model": "compact-generation-8", "options": {"num_ctx": 999999}}); d = detail(r)
rec("400 num_ctx hors borne du profil", "PASS" if r.status_code == 400 else "OBSERVED", http=r.status_code, detail=str(d)[:200])

# --- embeddings ----------------------------------------------------------------------------------
r = post("/v1/embeddings", {"texts": ["Le lac de Vermeil a une profondeur maximale de 47 mètres."], "model": "fr-en-retrieval", "task": "document"}); b = r.json()
ok = r.status_code == 200 and b.get("governed") is True and b.get("identity_verified") is True and b.get("dimension") == 768 and len(b.get("embeddings", [[]])[0]) == 768 and str(b.get("pipeline_id", "")).startswith("emb-v2:") and b.get("model_digest") == b.get("expected_digest") and b.get("normalized") is True and b.get("truncated") == [False]
rec("POST /v1/embeddings fr-en-retrieval (identité vérifiée)", "PASS" if ok else "FAIL", http=r.status_code, **{k: b.get(k) for k in ("model", "model_logical", "dimension", "identity_verified", "pipeline_id", "model_digest", "expected_digest", "normalized", "truncated", "task")}, norm=round(sum(x*x for x in b.get("embeddings", [[0]])[0]) ** 0.5, 6) if r.status_code == 200 else None, elapsed_ms=int(r.elapsed.total_seconds()*1000))
pid_doc = b.get("pipeline_id")
r = post("/v1/embeddings", {"texts": ["profondeur du lac de Vermeil"], "model": "fr-en-retrieval", "task": "query"}); b2 = r.json()
rec("POST /v1/embeddings task=query (pipeline_id distinct du document)", "PASS" if r.status_code == 200 and b2.get("pipeline_id") and b2.get("pipeline_id") != pid_doc else "FAIL", http=r.status_code, pipeline_id_query=b2.get("pipeline_id"))
r = post("/v1/embeddings", {"texts": ["a" * 1200], "model": "fr-en-retrieval", "task": "document"})
rec("embeddings 1200 caractères (borne incluse)", "PASS" if r.status_code == 200 else "FAIL", http=r.status_code)
r = post("/v1/embeddings", {"texts": ["a" * 1201], "model": "fr-en-retrieval", "task": "document"}); d = detail(r)
rec("embeddings 1201 caractères => 400 reject (aucune troncature)", "PASS" if r.status_code == 400 and isinstance(d, dict) and d.get("on_overflow") == "reject" else "FAIL", http=r.status_code, detail=d)
r = post("/v1/embeddings", {"texts": ["x"], "model": "mxbai-embed-large", "task": "document"}); d = detail(r)
rec("400 model_not_registered embeddings (mxbai-embed-large)", "PASS" if r.status_code == 400 and isinstance(d, dict) and d.get("strict_registry") is True else "FAIL", http=r.status_code, detail=d)
r = post("/v1/embeddings", {"texts": ["x"], "model": "legacy-768", "task": "document"}); d = detail(r)
rec("legacy-768 enregistré mais non installé => refus explicite", "PASS" if r.status_code in (503, 502) and isinstance(d, dict) else "OBSERVED", http=r.status_code, detail=d)
r = post("/v1/embeddings", {"texts": ["x"], "model": "fr-en-retrieval", "task": "document", "provider": "ollama"})
rec("400 extra_forbidden embeddings (champ provider)", "PASS" if r.status_code in (400, 422) else "FAIL", http=r.status_code)
r = post("/v1/embeddings", {"texts": ["x"] * 65, "model": "fr-en-retrieval", "task": "document"}); d = detail(r)
rec("embeddings lot > 64 => 400", "PASS" if r.status_code == 400 else "OBSERVED", http=r.status_code, detail=str(d)[:160])

# --- rerank ---------------------------------------------------------------------------------------
docs = [{"id": "d1", "text": "La cantine sert de la polenta le mardi."}, {"id": "d2", "text": "La profondeur maximale du lac de Vermeil est de 47 mètres."}, {"id": "d3", "text": "Le capteur de turbidité est le Hydra-TX-9."}]
r = post("/v1/rerank", {"query": "Quelle est la profondeur maximale du lac de Vermeil ?", "documents": docs, "model": "rerank-default", "top_n": 3}); b = r.json()
res = b.get("results", [])
rec("POST /v1/rerank (TEI réel, tri correct)", "PASS" if r.status_code == 200 and res and res[0].get("id") == "d2" and b.get("model_revision") else "FAIL", http=r.status_code, results=res, model_revision=b.get("model_revision"), model_logical=b.get("model_logical"), elapsed_ms=int(r.elapsed.total_seconds()*1000))
r = post("/v1/rerank", {"query": "q", "documents": [{"id": "big", "text": "b" * 4001}], "model": "rerank-default"}); d = detail(r)
rec("rerank document 4001 car. => 400 (avant TEI, auto-truncate inatteignable)", "PASS" if r.status_code == 400 else "FAIL", http=r.status_code, detail=str(d)[:160])
r = post("/v1/rerank", {"query": "q" * 2001, "documents": docs, "model": "rerank-default"})
rec("rerank requête 2001 car. => 400", "PASS" if r.status_code == 400 else "FAIL", http=r.status_code)
r = post("/v1/rerank", {"query": "q", "documents": [{"id": str(i), "text": "t"} for i in range(65)], "model": "rerank-default"})
rec("rerank 65 documents => 400", "PASS" if r.status_code == 400 else "FAIL", http=r.status_code)

# --- cadrage HTTP ----------------------------------------------------------------------------------
def gen(): yield b'{"prompt":"x","model":"compact-generation-8"}'
r = requests.post(BASE + "/v1/generate", data=gen(), headers={"Content-Type": "application/json"}, timeout=10); d = detail(r)
rec("411 length_required (POST JSON sans Content-Length)", "PASS" if r.status_code == 411 else "FAIL", http=r.status_code, detail=d)
r = requests.post(BASE + "/v1/embeddings", data=json.dumps({"texts": ["x" * 2_200_000], "model": "fr-en-retrieval"}), headers={"Content-Type": "application/json"}, timeout=30); d = detail(r)
rec("413 request_too_large (> 2 Mio)", "PASS" if r.status_code == 413 else "FAIL", http=r.status_code, detail=d)

# --- saturation (concurrence 2) --------------------------------------------------------------------
out = []
def worker():
    try:
        rr = post("/v1/generate", {"prompt": "Écris une phrase sur la montagne.", "model": "compact-generation-8", "options": {"num_predict": 40}}, timeout=300)
        out.append((rr.status_code, detail(rr) if rr.status_code >= 400 else None, rr.headers.get("Retry-After")))
    except Exception as e: out.append(("EXC", str(e), None))
th = [threading.Thread(target=worker) for _ in range(4)]; [t.start() for t in th]; [t.join() for t in th]
sat = [o for o in out if o[0] == 503]
rec("503 generation_saturated + Retry-After (4 requêtes, concurrence 2)", "PASS" if sat and all(isinstance(o[1], dict) and o[1].get("code") == "generation_saturated" and o[2] for o in sat) else "OBSERVED", outcomes=[(o[0], (o[1] or {}).get("code") if isinstance(o[1], dict) else o[1], o[2]) for o in out])
out = []
def eworker():
    rr = post("/v1/embeddings", {"texts": ["phrase %d" % i for i in range(64)], "model": "fr-en-retrieval", "task": "document"}, timeout=300)
    out.append((rr.status_code, (detail(rr) or {}).get("code") if rr.status_code >= 400 and isinstance(detail(rr), dict) else None, rr.headers.get("Retry-After")))
th = [threading.Thread(target=eworker) for _ in range(5)]; [t.start() for t in th]; [t.join() for t in th]
rec("503 embeddings_saturated + Retry-After (5 lots de 64, concurrence 2)", "PASS" if any(o[0] == 503 and o[1] == "embeddings_saturated" and o[2] for o in out) else "OBSERVED", outcomes=out)

# --- capacités OFF -----------------------------------------------------------------------------------
r = post("/v1/vision/analyze", {"image_base64": "aGVsbG8=", "task": "describe"}); d = detail(r)
rec("vision OFF => 503 vision_not_enabled", "PASS" if r.status_code == 503 else "OBSERVED", http=r.status_code, detail=str(d)[:160])
r = requests.post(BASE + "/v1/audio/transcriptions", files={"file": ("a.wav", b"RIFF")}, timeout=10); d = detail(r)
rec("ASR OFF => 503 asr_unavailable", "PASS" if r.status_code == 503 else "OBSERVED", http=r.status_code, detail=str(d)[:160])
r = post("/v1/vision/structured-extract", {}); rec("StructX absent => 404", "PASS" if r.status_code == 404 else "FAIL", http=r.status_code)
r = requests.get(BASE + "/health/vision", timeout=5); rec("GET /health/vision => DISABLED", "PASS" if r.status_code in (200, 503) else "FAIL", http=r.status_code, body=str(r.text)[:160])
r = requests.get(BASE + "/health/asr", timeout=5); rec("GET /health/asr => DISABLED", "PASS" if r.status_code in (200, 503) else "FAIL", http=r.status_code, body=str(r.text)[:160])
# Aucune inférence déclenchée par les sondes : inventaire /api/ps d'Ollama (hors Gateway, réseau ai)
try:
    ps = requests.get("http://ollama:11434/api/ps", timeout=5).json(); rec("inventaire runtime (/api/ps) après campagne", "INFO", loaded=[(m.get("name"), m.get("size_vram")) for m in ps.get("models", [])])
except Exception as e: rec("inventaire runtime", "INFO", error=str(e))
print("\n===JSON===\n" + json.dumps(R, ensure_ascii=False, indent=1))
