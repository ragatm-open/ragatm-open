#!/usr/bin/env bash
# Sauvegarde d'une installation Compact : base PostgreSQL (pg_dump custom), documents
# originaux (volume media), configuration non secrète, identités nécessaires (registre des modèles,
# manifeste d'approvisionnement). Le `.env` (secrets) N'EST PAS inclus : il est sauvegardé séparément
# par l'exploitant (chiffré). Redis n'est pas une source de vérité : la file Celery est volatile et les
# documents en cours d'ingestion au moment de la sauvegarde devront être réindexés après restauration.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"; cd "$HERE"
DEST="${1:-$HERE/backups/$(date -u +%Y%m%dT%H%M%SZ)}"; mkdir -p "$DEST"; chmod 700 "$DEST"
# secrets de l'instance (COMPACT_ENV_FILE, comme compose.yml et restore.sh) — défaut ./.env
ENVF="${COMPACT_ENV_FILE:-./.env}"; set -a; . "$ENVF"; set +a
echo "[backup] base ${DB_NAME} -> $DEST/db.dump"
docker compose exec -T db pg_dump -U "$DB_USER" -d "$DB_NAME" -Fc > "$DEST/db.dump"
echo "[backup] documents (volume media) -> $DEST/media.tar.gz"
docker compose run --rm --no-deps --entrypoint sh backend -c "cd /data/media && tar czf - ." > "$DEST/media.tar.gz"
echo "[backup] configuration et identités"
cp config/backend.env config/gateway.env "$DEST/"; cp "${AI_GATEWAY_OPEN_DIR:-../../ai-gateway-open}/app/models_registry.yaml" "$DEST/models_registry.yaml"
[[ -f state/provision-manifest.json ]] && cp state/provision-manifest.json "$DEST/"
docker compose exec -T backend python manage.py shell -c "
from apps.rag_core.models import RagSpace, Document
from apps.rag_core.models.document import DocumentStatus
import json
print(json.dumps({'ragspaces': RagSpace.objects.count(), 'documents': Document.objects.count(),
  'documents_not_ready': Document.objects.exclude(status=DocumentStatus.READY).count(),
  'embedding_profiles': sorted(set(RagSpace.objects.values_list('embedding_model', flat=True)))}))" > "$DEST/inventory.json" 2>/dev/null || echo '{}' > "$DEST/inventory.json"
( cd "$DEST" && sha256sum db.dump media.tar.gz backend.env gateway.env models_registry.yaml inventory.json > SHA256SUMS )
echo "[backup] terminé : $DEST"; cat "$DEST/inventory.json"
