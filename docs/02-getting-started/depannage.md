# Dépannage (dev)

Objectif : diagnostiquer et corriger les blocages courants du dev local. Public :
développeuses et développeurs. Le code fait foi. Pour la configuration, voir
[configuration.md](configuration.md) ; pour la procédure, [developpement-local.md](developpement-local.md).

## Incohérence de dimension d'embeddings (`rag_core.E001`)

**Symptôme.** Le démarrage échoue sur un system check `rag_core.E001` :
`RAG_EMBEDDING_DIM` ne correspond pas à la dimension figée de la colonne pgvector.

**Cause.** La colonne vecteur a une dimension figée à la migration `0001` (768). Changer
`RAG_EMBEDDING_DIM` ou de modèle d'embeddings sans réembedder casse cette correspondance.

**Correctif.** Remettre `RAG_EMBEDDING_DIM=768` (défaut). Pour changer réellement de
dimension/modèle, il faut réembedder l'ensemble du corpus (`scripts/reembed_embeddings.sh`),
la dimension pgvector n'étant pas modifiable à chaud.

## Échecs des checks IA `llm_router.E001`–`E005`

Les system checks de la policy AI Gateway bloquent un démarrage mal configuré :

| Check | Cause | Correctif |
|---|---|---|
| `E001` | `AI_GATEWAY_ENABLED != 1` en env officiel | `AI_GATEWAY_ENABLED=1` (ou escape hatch ci-dessous en tests). |
| `E002` | `AI_GATEWAY_BASE_URL` vide, ou loopback/`localhost`/`0.0.0.0` | Pointer vers la Gateway routable (p. ex. `http://ai-gateway:8100`). |
| `E003` | `RAG_LLM_PROVIDER=ollama` alors que la Gateway est éteinte en environnement officiel (appel Ollama direct interdit) | Activer la Gateway (`AI_GATEWAY_ENABLED=1`) : la variable est alors sans effet. |
| `E004` | `AI_GATEWAY_FALLBACK_PROVIDER` ≠ `none` | `none` (aucun fournisseur de repli n'existe). |
| `config.E009` / `llm_router.E009` | variable d'environnement (`OPENAI_API_KEY`…) ou valeur de provider externe configurée | Retirer la variable / la valeur : aucun fournisseur externe n'existe. |

**Échappatoire tests.** `ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=1` dégrade E001/E003 en
avertissement pour un dev/CI hors Gateway. **Jamais en environnement réel.** Détails :
[../06-ai/ai-gateway.md](../06-ai/ai-gateway.md).

## Port déjà occupé

**Symptôme.** `up.sh` refuse de démarrer : `Port 6811 (backend Django) déjà occupé`.

**Correctif.** Identifier le processus puis libérer le port, ou surcharger la variable
`DEV_*` correspondante :

```bash
ss -ltnp | grep ':6811'      # ou lsof -i :6811
# puis, au choix :
DEV_BACKEND_PORT=6812 ./scripts/dev/up.sh
```

Les ports dev réservés (loopback) sont 6300 / 6811 / 65432 ; voir
[../10-reference/ports.md](../10-reference/ports.md). `./scripts/dev/stop_dev.sh` arrête
proprement une stack RagATM déjà lancée.

## Extension pgvector absente

**Symptôme.** Erreurs Postgres du type `type "vector" does not exist` ou échec des
migrations sur les colonnes vecteur.

**Cause.** L'extension `vector` est créée par `infra/postgres/initdb/001_pgvector.sql`,
**uniquement au premier démarrage d'un volume neuf**. Un volume préexistant (mauvaise
image, init manqué) ne l'a pas.

**Correctif.** Repartir d'un volume propre : `./scripts/dev/reset_db.sh` (recrée le service
db et rejoue l'initdb), ou activer l'extension manuellement dans la base dev :
`CREATE EXTENSION IF NOT EXISTS vector;`. Vérifier que l'image est bien
`pgvector/pgvector:pg16`.

## `503 ingest_enqueue_unavailable` (broker indisponible)

**Symptôme.** Un upload ou une réindexation en mode asynchrone renvoie
`503 ingest_enqueue_unavailable`.

**Cause.** Le broker Redis Celery est injoignable alors que l'ingestion async est demandée.
L'API refuse volontairement de renvoyer un faux `queued: true` : le document passe en
`FAILED` et l'appel renvoie `503`.

**Correctif.** En dev, l'ingestion est **synchrone par défaut** (pas besoin de Redis). Si
vous avez activé l'async (`RAG_INGEST_ASYNC_DEFAULT=1`), lancez un Redis loopback et un
worker :

```bash
docker run -p 127.0.0.1:66379:6379 redis:7-alpine
cd backend && celery -A config worker -l info
# .env : CELERY_BROKER_URL=redis://127.0.0.1:66379/0
```

## `stub` vs embeddings réels (RAG dégradé)

**Symptôme.** Le retrieval renvoie des sources incohérentes ou le chat ne « trouve » jamais
rien de pertinent.

**Cause.** `EMBEDDINGS_PROVIDER=stub` produit des embeddings déterministes **sans
sémantique** (escape hatch tests/CI). Le RAG est fonctionnel mais dégradé.

**Correctif.** Pour un RAG réaliste, utiliser `EMBEDDINGS_PROVIDER=ollama` (via la Gateway,
dim 768, modèle `nomic-embed-text`). Après changement de provider, réembedder le corpus
existant. Détails : [configuration.md](configuration.md).

## Document bloqué en `PENDING`

**Symptôme.** Un document reste indéfiniment en `PENDING`, jamais `READY`.

**Cause.** L'ingestion asynchrone est activée mais **aucun worker Celery** ne consomme la
file (le compose dev ne lance pas de worker). En eager/sync, ce cas ne se produit pas.

**Correctif.** Soit lancer un worker Celery (voir section broker ci-dessus), soit revenir
au mode synchrone dev (ne pas activer `RAG_INGEST_ASYNC_DEFAULT`). Rappel : aucun
conteneur `beat` n'est fourni non plus — le scheduler est à lancer séparément si besoin.

## Document en `FAILED`

**Symptôme.** Le document tombe en `FAILED` avec un `error_message`.

**Causes fréquentes.** Timeout d'extraction PDF (`pdf_extraction_timeout`), PDF trop long
(`pdf_too_many_pages`, > 500 pages par défaut), fichier trop volumineux (`413`,
`file_too_large`, > 25 Mo), ou provider d'embeddings lent/absent (`transient_ingest_error`).

**Correctif.** Ajuster les limites (`UPLOAD_MAX_FILE_SIZE_MB`, `UPLOAD_PDF_MAX_PAGES`,
`UPLOAD_EXTRACTION_TIMEOUT_S`) ou vérifier la disponibilité du provider d'embeddings, puis
réindexer le document.

## OCR sans binaire `tesseract`

**Symptôme.** L'OCR échoue (PDF image ou image sans texte) malgré `DOCUMENT_OCR_ENABLED=1`.

**Cause.** L'extra Python `[ocr]` (installé par `up.sh`) fournit `pytesseract`, mais l'OCR
requiert aussi le **binaire système `tesseract`** + les packs de langue.

**Correctif.**

```bash
sudo apt install tesseract-ocr tesseract-ocr-fra
```

Rappel : `DOCUMENT_OCR_ENABLED=0` par défaut ; les images sont OCRisées systématiquement
quand `DOCUMENT_IMAGE_INGEST_ENABLED=1` (défaut). Voir
[../10-reference/feature-flags.md](../10-reference/feature-flags.md).

## Références d'implémentation

- `backend/apps/llm_router/checks.py` (E001–E005)
- `backend/apps/rag_core/apps.py` (`rag_core.E001`, dimension pgvector)
- `backend/api/v1/documents/views_common.py` (`enqueue_async_or_503`)
- `backend/apps/rag_core/models/document.py` (statuts)
- `backend/config/settings/base.py` (limites upload, OCR, async)
- `scripts/dev/up.sh`, `scripts/dev/reset_db.sh`
- `infra/postgres/initdb/001_pgvector.sql`
