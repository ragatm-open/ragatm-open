# Embeddings

Objectif : décrire la production des vecteurs d'embedding : résolution du provider par RagSpace,
provider unique `ai_gateway` (pipeline gouverné de l'AI Gateway), dimension figée, lots, garde
fail-closed et réindexation. Public : développeurs backend, exploitants.

Les embeddings sont **distincts** de la génération chat : ils ont leur propre provider/modèle par
RagSpace. Le retrieval qui les consomme est décrit dans
[../03-backend/pipeline-retrieval.md](../03-backend/pipeline-retrieval.md).

---

## 1. Résolution du provider (`apps/llm_router/embeddings.py::embed_texts`)

Priorité : **RagSpace d'abord** (`resolve_space_embedding` → `embedding_provider` /
`embedding_model`), repli sur les réglages globaux.

Champs modèle (`apps/rag_core/models/rag_space.py`) :

| Champ | Choices | Défaut |
|---|---|---|
| `embedding_provider` | `ai_gateway` | **`ai_gateway`** |
| `embedding_model` | modèle logique du registre de la Gateway | `legacy-768` |

Réglages globaux (`config/settings/base.py`) — valeurs centralisées dans
[../10-reference/configuration.md](../10-reference/configuration.md) :

| Variable | Défaut | Rôle |
|---|---|---|
| `EMBEDDINGS_PROVIDER` | **`ai_gateway`** (`stub` pour les tests) | provider global de repli |
| `EMBEDDINGS_MODEL` | `"default"` (placeholder ignoré) | — |
| `AI_GATEWAY_EMBEDDINGS_ENABLED` | `1` | active le chemin gouverné |
| `AI_GATEWAY_EMBEDDINGS_MODEL` | `legacy-768` | modèle logique par défaut |
| `EMBEDDINGS_TIMEOUT_S` | `30` | timeout |
| `RAG_EMBEDDING_DIM` | **`768`** | dimension (doit matcher pgvector) |
| `RAG_EMBED_BATCH_SIZE` | `64` | taille de lot |

`DEFAULT_RAGSPACE_EMBEDDING_PROVIDER=ai_gateway` et `DEFAULT_RAGSPACE_EMBEDDING_MODEL=legacy-768`
fixent les valeurs posées à la création d'un espace. `EXTERNAL_EMBEDDINGS_EGRESS_ENABLED` existe
toujours mais vaut toujours `0` : aucun provider externe ne peut être configuré.

---

## 2. Escape hatch stub (tests) — refusé en prod

Si `EMBEDDINGS_PROVIDER=stub` **globalement**, le **stub déterministe** (sha256 → pseudo-vecteurs)
est forcé quel que soit le réglage de l'espace. Un provider inconnu/non configuré est refusé.

**Interdit en production** : `config/settings/prod.py` **refuse** au démarrage
`EMBEDDINGS_PROVIDER ∈ {"", "stub"}`. À décrire comme « escape hatch tests », **jamais** comme
défaut de production.

Statut : **implémenté**, utilisé en dev/CI, refusé en prod.

---

## 3. Provider réel : `ai_gateway`

| Provider | Endpoint | Via Gateway ? |
|---|---|---|
| `ai_gateway` | `POST {AI_GATEWAY_EMBEDDINGS_BASE_URL}/v1/embeddings` — `{model, task, texts}` → vecteurs **+ `pipeline_id` opaque** (validé contre `GET /health/embeddings`) | oui, service **gouverné** |

C'est le **seul** provider d'embeddings réel. Les anciens chemins (Ollama direct, provider
externe) ont été supprimés. Un espace est dit **gouverné** : ses vecteurs portent une identité de
pipeline vérifiée (`rag_chunk.pipeline_id`, `pipeline_family`, digest du modèle vérifié), sa
recherche vectorielle est filtrée dessus, et son chunking est borné par la fenêtre du modèle.

### 3.1 Configuration d'un espace — *implémenté*

`ai_gateway` est accepté par **l'API REST** (création et PATCH d'un RagSpace) et par la commande
`set_ragspace_embedding_pipeline`.

L'API refuse à la **configuration** ce que `embed_via_gateway` refuserait à l'ingestion :

| Code d'erreur (HTTP 400) | Condition |
|---|---|
| `governed_embeddings_disabled` | `AI_GATEWAY_EMBEDDINGS_ENABLED=0` |
| `governed_embeddings_base_url_missing` | `AI_GATEWAY_EMBEDDINGS_BASE_URL` vide |
| `governed_model_required` | modèle logique vide **et** aucun `AI_GATEWAY_EMBEDDINGS_MODEL` déclaré |

Sans cette garde, un espace créé sur un environnement non équipé serait syntaxiquement valide et
fonctionnellement mort : chaque document échouerait.

Le couple `(provider, modèle)` est évalué sur l'**état résultant**, pas sur le seul payload :
un PATCH qui ne touche qu'un champ hérite de l'autre.

`embedding_model` porte le **modèle logique** du registre Gateway (par défaut `legacy-768`),
jamais le nom du modèle amont — c'est le registre qui fait la correspondance, et c'est ce qui
permet de changer les poids sans changer la configuration RagATM. Le digest, lui, changerait —
donc l'identité aussi.

**Le corpus métier** utilise le même provider unique (`ai_gateway`, modèle par défaut
`legacy-768`) : voir [politique-egress.md](politique-egress.md) §9.

La configuration reste **superuser only**.

> **Aucun repli.** Un espace dont la Gateway échoue lève une erreur — il ne retombe jamais sur un
> autre chemin, ce qui produirait des vecteurs d'un autre pipeline sous une identité qui
> prétendrait le contraire.

### 3.2 Le corps de requête est CANONIQUE et fermé — *implémenté*

Le schéma `EmbeddingsRequest` de la Gateway n'accepte que **quatre** champs — `texts`,
`model`, `task`, `keep_alive` — et est **fermé** (`additionalProperties: false`). `provider` n'en
fait **jamais** partie : RagATM ne le sérialise pas dans `/v1/embeddings`. Un seul constructeur
produit le corps : `apps/llm_router/embeddings_gateway.py::build_gateway_embeddings_request`.

| Ce que RagATM possède | Ce que la Gateway possède |
|---|---|
| l'intention sémantique `document` / `query` | le **provider amont** |
| la configuration du RagSpace (`embedding_provider`) | la correspondance profil logique → modèle concret |
| la persistance de l'identité d'embedding | le **préfixage** (`search_document:` / `search_query:`) |
| l'usage à l'indexation et à la recherche | la normalisation, le digest, la dimension |

L'endpoint `/v1/embeddings` est fourni par l'AI Gateway (service distinct de ce dépôt).

---

## 3bis. Deux magasins, deux identités — lire les logs sans se tromper

Une même requête produit **deux appels d'embedding**, un par magasin interrogé (espace privé et
corpus métier). Les deux passent par `ai_gateway`, mais avec leur propre modèle logique et leur
propre famille de pipeline.

| magasin | provider / modèle | chemin | ligne de log |
|---|---|---|---|
| **RagSpace** (espace privé) | `ai_gateway` / modèle de l'espace | gouverné | `embedding_gateway_call` |
| **Corpus métier** | `ai_gateway` / modèle du corpus | gouverné | `embedding_gateway_call` |

Chaque magasin compare la question à ses chunks **dans sa propre famille**, et refuse de
faire autrement : filtre `pipeline_family` côté espace (fail-closed si la Gateway ne la
rend pas), filtre `embedding_fingerprint` + provider + modèle + dimension côté corpus.

## 4. Dimension figée et garde fail-closed

- **Dimension 768 figée** : colonne pgvector `rag_chunk.embedding` (`VectorField(dimensions=768)`,
  migration 0001). Le system check `rag_core.E001` échoue si `RAG_EMBEDDING_DIM` diverge. Un
  mismatch de dimension à l'exécution lève `EmbeddingsError` (garde dure).
- **Fail-closed sur textes blancs** : aucun texte blanc n'est transmis à un vrai provider
  (`filter_nonblank_texts` en amont) — préserve l'alignement spec→vecteur et évite qu'un chunk
  blanc fasse échouer tout le document.
- **Lots de 64** : envoi par lots de `RAG_EMBED_BATCH_SIZE=64` (la Gateway refuse > 64
  textes/appel).

---

## 5. Réindexation sur changement de provider/modèle

- L'indexation pose une empreinte sur chaque document : `Document.embedding_provider` /
  `embedding_model` (snapshot) + `reindex_required`.
- Changer `embedding_provider` ou `embedding_model` d'un RagSpace **impose de réindexer** : le
  retrieval vectoriel (P1-D) ne compare qu'aux documents encodés avec le provider+modèle **courant**
  de l'espace ET `reindex_required=False`. Les documents incohérents sont **exclus du vectoriel**
  (mais restent trouvables en FTS lexical). Voir
  [../03-backend/pipeline-retrieval.md](../03-backend/pipeline-retrieval.md).
- La question est toujours encodée avec le **même** provider/modèle que l'espace.
- **Traçabilité par chunk** : un espace `ai_gateway` écrit sur chaque chunk le
  `pipeline_id` rendu par la Gateway — identifiant **opaque** (`emb-v2:<empreinte>`), validé par
  égalité avec la référence `/health/embeddings`, jamais découpé — et la **famille** de
  comparabilité `<modèle_logique>@<digest8>`, dérivée des champs attestés de la réponse. Les
  chunks historiques restent `NULL` et ne sont **jamais** étiquetés après coup — seule une
  réindexation leur donne une identité. La recherche gouvernée compare uniquement des chunks
  de la même **famille** de pipeline ; question et fragments ont des `pipeline_id` différents
  mais la même famille. Voir
  la documentation d'exploitation (hors périmètre de cette édition).

---

## Références d'implémentation

- `backend/apps/llm_router/embeddings.py`
- `backend/apps/rag_core/models/rag_space.py`
- `backend/apps/rag_core/models/{document,chunk}.py`
- `backend/apps/rag_core/services/ingest/indexing.py`
- `backend/apps/rag_core/repositories/chunk_repository.py`
- `backend/config/settings/{base,prod}.py`
- `backend/apps/llm_router/{embedding_pipeline,embeddings_gateway,embedding_reference}.py` (identités opaques)
- `backend/apps/rag_core/services/embedding_pipeline_coverage.py` 
