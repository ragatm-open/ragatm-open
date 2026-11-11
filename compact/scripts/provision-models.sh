#!/usr/bin/env bash
# APPROVISIONNEMENT EXPLICITE des modèles — phase distincte de l'exécution nominale.
# Télécharge, dans les volumes, les artefacts du registre Compact, puis relève leurs digests.
#   · Ollama : granite-embedding:278m (fr-en-retrieval), qwen2.5:1.5b-instruct (compact-generation-8)
#   · TEI    : BAAI/bge-reranker-v2-m3 @ 953dc6f6… (rerank-default), via premier démarrage
# Option --with-12gb : ajoute qwen2.5:7b-instruct (compact-generation, profil 12 Go NON qualifié).
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$HERE"
DC=(docker compose -f compose.yml -f compose.provision.yml)
MODELS=(granite-embedding:278m qwen2.5:1.5b-instruct); [[ "${1:-}" == "--with-12gb" ]] && MODELS+=(qwen2.5:7b-instruct)
[[ -f .env ]] || { echo "[provision] .env absent : scripts/gen-secrets.sh d'abord" >&2; exit 1; }
echo "[provision] démarrage ollama + ai-gateway + reranker avec réseau de sortie 'provision'"
"${DC[@]}" up -d ollama ai-gateway reranker
for m in "${MODELS[@]}"; do echo "[provision] ollama pull $m"; "${DC[@]}" exec -T ollama ollama pull "$m"; done
echo "[provision] attente du reranker TEI (téléchargement des poids, révision épinglée)…"
for i in $(seq 1 120); do
  if "${DC[@]}" exec -T ai-gateway python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8300/health', timeout=3).status==200 else 1)" 2>/dev/null; then echo "[provision] TEI prêt"; break; fi
  sleep 5; [[ $i -eq 120 ]] && { echo "[provision] TEI non prêt après 10 min" >&2; "${DC[@]}" logs --tail 30 reranker; exit 2; }
done
mkdir -p "$HERE/state"
{
  echo "{"; echo "  \"provisioned_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
  echo "  \"ollama\": $("${DC[@]}" exec -T ollama ollama list --format json 2>/dev/null || "${DC[@]}" exec -T ai-gateway python -c "import urllib.request,json;print(json.dumps(json.load(urllib.request.urlopen('http://ollama:11434/api/tags'))['models']))"),"
  echo "  \"tei_info\": $("${DC[@]}" exec -T ai-gateway python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8300/info', timeout=5).read().decode())")"
  echo "}"
} > "$HERE/state/provision-manifest.json"
echo "[provision] manifeste : state/provision-manifest.json"
echo "[provision] arrêt des services d'approvisionnement (volumes conservés) ; relancer avec scripts/up.sh"
"${DC[@]}" stop reranker ai-gateway ollama
"${DC[@]}" rm -f reranker ai-gateway ollama >/dev/null
docker network rm "$(grep -E '^COMPOSE_PROJECT_NAME=' .env | cut -d= -f2 || echo ragatm-compact)_provision" 2>/dev/null || true
echo "PROVISION=DONE"
