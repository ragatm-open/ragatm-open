#!/usr/bin/env bash
# Diagnostic NON inférentiel de la pile Compact : états, santé Gateway (/health/live, /health/runtime), journaux.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$HERE"
echo "== Services"; docker compose ps --format 'table {{.Name}}\t{{.Status}}\t{{.Ports}}'
echo "== Gateway /health/live"; docker compose exec -T ai-gateway python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8100/health/live',timeout=5).read().decode())" || true
echo "== Gateway /health/runtime (capacités, modèles installés)"; docker compose exec -T ai-gateway python - <<'PY' || true
import json, urllib.request
d = json.load(urllib.request.urlopen('http://127.0.0.1:8100/health/runtime', timeout=5))
print("capabilities:", json.dumps(d.get("capabilities"), ensure_ascii=False))
for m in d.get("llm_models", []): print(f"  llm {m.get('name')} -> {m.get('ollama_name')} installed={m.get('installed')} qualification={(m.get('compact') or {}).get('qualification')}")
for m in d.get("models", d.get("embedding_models", [])) or []: print(f"  emb {m.get('model_logical', m.get('name'))} status={m.get('registry_status', m.get('status'))}")
PY
echo "== Backend /api/health/"; docker compose exec -T backend python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8010/api/health/',timeout=5).read().decode())" || true
echo "== Worker"; docker compose exec -T worker celery -A config inspect ping --timeout 10 2>/dev/null | tail -3 || true
echo "== Derniers journaux (20 lignes/service)"; for s in backend worker beat ai-gateway ollama reranker frontend; do echo "--- $s"; docker compose logs --no-color --tail 20 "$s" 2>/dev/null | cut -c1-220; done
