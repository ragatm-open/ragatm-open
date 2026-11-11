# Sauvegarde et restauration

## Ce qui constitue l'installation

| Élément | Source de vérité | Sauvegardé par `scripts/backup.sh` |
|---|---|---|
| Comptes, workspaces, espaces, documents (métadonnées), chunks, vecteurs, références de page/passage, fils de discussion, identités de pipeline (`pipeline_id`, digest) | PostgreSQL (`db_data`) | `db.dump` (`pg_dump -Fc`) |
| Fichiers originaux des documents | volume `media` | `media.tar.gz` |
| Configuration non secrète | `config/backend.env`, `config/gateway.env` | copiés |
| Identité des modèles | `../../ai-gateway-open/app/models_registry.yaml`, `state/provision-manifest.json` | copiés |
| Secrets | `.env` (0600) | **non inclus** — à sauvegarder séparément, chiffré |
| Poids des modèles | volumes `ollama_data`, `tei_data` | non inclus — réapprovisionnables (digests dans le manifeste) |
| File Celery, cache, verrous, états de jobs en cours | Redis (`redis_data`) | **non inclus** — Redis n'est pas une source de vérité |

`inventory.json` consigne les comptes d'espaces et de documents et le nombre de documents non
`READY` au moment de la sauvegarde.

## Sauvegarder

```bash
scripts/backup.sh [dossier]     # défaut : backups/<horodatage UTC>/ (0700) + SHA256SUMS
```

## Restaurer dans une instance isolée

```bash
mkdir ../compact-restore && cp -r compose*.yml config scripts .env.example ../compact-restore/
# puis, dans ../compact-restore/.env : AI_GATEWAY_OPEN_DIR=<chemin du clone ai-gateway-open> (RAGATM_OPEN_DIR si le kit n'est pas dans ragatm-open/compact)
cd ../compact-restore && scripts/gen-secrets.sh        # secrets PROPRES à l'instance
COMPOSE_PROJECT_NAME=ragatm-compact-restore COMPACT_FRONTEND_PORT=18081 \
  scripts/restore.sh /chemin/vers/backups/<horodatage>
```

Le script vérifie les sommes de contrôle, démarre `db`/`redis` du projet cible, restaure la base
(`pg_restore --clean --if-exists`), les documents, puis la pile complète. Les modèles doivent être
approvisionnés dans l'instance cible (mêmes digests) : les identités de pipeline stockées en base
(`pipeline_id`, `model_digest`) sont alors vérifiées à la première requête par la Gateway.

## Limites

- **Jobs en cours** : un document `PENDING`/`INGESTING` au moment de la sauvegarde n'a pas de
  message Celery dans la restauration (Redis non sauvegardé). Il reste dans cet état et doit être
  réindexé (`POST /api/v1/documents/<id>/reindex/` ou reprise en masse depuis l'espace).
- **Jetons de session** : les jetons d'authentification sont en base et restent valides après
  restauration ; les révoquer si la sauvegarde a pu être exposée.
- **Même `DJANGO_SECRET_KEY`** : une instance restaurée avec une autre clé invalide les liens de
  réinitialisation de mot de passe émis avant la sauvegarde (comportement attendu).
- Aucune sauvegarde privée (programme d'origine) n'est utilisée ni utilisable ici.
