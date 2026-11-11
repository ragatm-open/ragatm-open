#!/usr/bin/env bash
# Contrôle HORS LIGNE (qualification interne, phase E) : depuis CHAQUE conteneur de la pile nominale, vérifier
# qu'aucune sortie Internet n'est possible (DNS externe, TCP vers des API d'IA distantes), tout en
# gardant la communication interservices. Ne modifie rien ; imprime un tableau et un verdict.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$HERE"
TARGETS=(api.openai.com api.anthropic.com api.mistral.ai generativelanguage.googleapis.com huggingface.co registry.ollama.ai 1.1.1.1)
fail=0
probe_py() { # $1 service ; $2 hôte ; $3 port
  docker compose exec -T "$1" python - "$2" "$3" <<'PY' 2>/dev/null || echo "exec_error"
import socket, sys
host, port = sys.argv[1], int(sys.argv[2])
try:
    ip = socket.gethostbyname(host)
except Exception as e:
    print(f"DNS_BLOCKED({type(e).__name__})"); sys.exit(0)
try:
    s = socket.create_connection((ip, port), timeout=3); s.close(); print(f"CONNECTED({ip})")
except Exception as e:
    print(f"TCP_BLOCKED({type(e).__name__})")
PY
}
probe_sh() { # conteneurs sans python : busybox/alpine (redis, frontend) ou ollama (curl)
  docker compose exec -T "$1" sh -c "(getent hosts $2 >/dev/null 2>&1 || nslookup $2 >/dev/null 2>&1) && echo DNS_OK || echo DNS_BLOCKED" 2>/dev/null || echo "exec_error"
}
echo "service      cible                                   résultat"
for svc in backend worker beat ai-gateway; do
  for t in "${TARGETS[@]}"; do
    r=$(probe_py "$svc" "$t" 443 | tail -1); printf "%-12s %-40s %s\n" "$svc" "$t" "$r"
    [[ "$r" == CONNECTED* ]] && fail=1
  done
done
for svc in redis frontend ollama db; do
  for t in api.openai.com huggingface.co; do
    r=$(probe_sh "$svc" "$t" | tail -1); printf "%-12s %-40s %s\n" "$svc" "$t" "$r"
    [[ "$r" == DNS_OK ]] && { [[ "$svc" == "frontend" ]] && echo "   (frontend : seul service sur le réseau de bord 'edge', publié en boucle locale ; il n'appelle aucune API d'IA)" || fail=1; }
  done
done
echo "--- communication interservices (doit fonctionner)"
printf "%-12s %-40s %s\n" backend ai-gateway:8100/health/live "$(probe_py backend ai-gateway 8100 | tail -1)"
printf "%-12s %-40s %s\n" worker  ai-gateway:8100 "$(probe_py worker ai-gateway 8100 | tail -1)"
printf "%-12s %-40s %s\n" backend db:5432 "$(probe_py backend db 5432 | tail -1)"
printf "%-12s %-40s %s\n" backend redis:6379 "$(probe_py backend redis 6379 | tail -1)"
printf "%-12s %-40s %s\n" ai-gateway ollama:11434 "$(probe_py ai-gateway ollama 11434 | tail -1)"
printf "%-12s %-40s %s\n" ai-gateway "127.0.0.1:8300 (TEI)" "$(probe_py ai-gateway 127.0.0.1 8300 | tail -1)"
echo "--- ports publiés sur l'hôte"
docker compose ps --format '{{.Name}} {{.Publishers}}' | sed 's/^/  /'
docker compose ps --format json | python3 -c "
import json,sys
fail=0
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    s=json.loads(line)
    for p in s.get('Publishers') or []:
        if p.get('PublishedPort'):
            ok = p.get('URL') in ('127.0.0.1','::1') and 'frontend' in s['Name']
            print(f\"  {s['Name']}: {p.get('URL')}:{p.get('PublishedPort')} -> {p.get('TargetPort')} {'OK' if ok else 'VIOLATION'}\"); fail |= (not ok)
sys.exit(fail)" || fail=1
[[ $fail -eq 0 ]] && echo "OFFLINE_CHECK=PASS" || { echo "OFFLINE_CHECK=FAIL"; exit 1; }
