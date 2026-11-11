#!/usr/bin/env bash
# Diagnostic de retrieval (§9.3) sur une installation Compact en marche.
# Usage : scripts/retrieval-diagnostic.sh <rag_space_id> [sortie.json]
# Ne modifie aucun réglage ; n'écrit rien en base ; aucun contenu documentaire en sortie.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$HERE"
SPACE="${1:?rag_space_id requis}"; OUT="${2:-retrieval-diagnostic.json}"
docker compose cp fixtures/corpus/manifest.json backend:/tmp/manifest.json >/dev/null
docker compose exec -T -e DIAG_RAG_SPACE_ID="$SPACE" -e DIAG_MANIFEST=/tmp/manifest.json backend \
  python manage.py shell -c "$(cat scripts/retrieval_diagnostic.py)" | grep -v "^DIAG_WRITTEN" >/dev/null || true
docker compose cp backend:/tmp/retrieval-diagnostic.json "$OUT" >/dev/null
python3 - "$OUT" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"space={d['space']} embedding={d['embedding']} min_score={d['settings']['RAG_MIN_SCORE_effective']}")
for q in d["questions"]:
    v = q.get("vector", {}); f = q.get("fts", {})
    print(f"{q['id']:<22} fts={f.get('n','?'):>2} vec_raw={v.get('n_raw','?'):>2} vec_kept={v.get('n_after_threshold','?'):>2} best={v.get('best_score')} -> {q.get('diagnosis','?')}")
PY
