# RAGatm Open

**RAGatm Open — plateforme documentaire locale pour la recherche, l'analyse et les réponses sourcées.**

RAGatm Open est une application de questions-réponses sur documents (RAG) pour une organisation :
des espaces documentaires cloisonnés, une ingestion de documents bureautiques et techniques, une
recherche hybride (lexicale + vectorielle) avec reranking, et une génération de réponses citant leurs
sources, le tout **avec des modèles exécutés localement** par [AI Gateway Open](https://github.com/ragatm-open/ai-gateway-open)
(Ollama, TEI). Aucun fournisseur d'IA externe n'est appelé, et aucun repli vers un service distant
n'existe dans le code.

Dépôts du programme : [ragatm-open](https://github.com/ragatm-open/ragatm-open) (ce dépôt, kit Compact inclus), [ai-gateway-open](https://github.com/ragatm-open/ai-gateway-open) (requis), [ai-gateway-pool-open](https://github.com/ragatm-open/ai-gateway-pool-open) (facultatif).

## Fonctionnalités vérifiées (profil Compact, CPU)

- Espaces documentaires (workspaces / RAG spaces) avec contrôle d'accès par rôle, MFA TOTP obligatoire.
- Ingestion asynchrone (Celery) : texte, Markdown, PDF texte, DOCX, XLSX, PPTX, XML, DXF, EML ;
  OCR, IFC, DWG et audio sont des options de construction **désactivées** dans Compact.
- Retrieval hybride (PostgreSQL FTS + pgvector 768) fusionné par RRF, reranking cross-encoder via la
  Gateway (repli lexical si le reranker est indisponible), réponses en flux (SSE) avec citations.
- Identité d'embeddings gouvernée : refus d'indexer ou d'interroger avec un pipeline différent de celui
  de l'espace ; aucun réindexage implicite.
- Erreurs explicites quand une dépendance est indisponible (`503 ai_gateway_unavailable`,
  `502 ai_gateway_error`, `503 dependency_unavailable`) — jamais une réponse fabriquée.

Ce qui n'est **pas** vérifié : profils GPU 8/12 Go (aucune mesure), capacités Vision/ASR (hors Compact),
déploiement multi-machines, charge au-delà d'une génération concurrente.

## Prérequis et installation

```bash
git clone https://github.com/ragatm-open/ragatm-open.git
git clone https://github.com/ragatm-open/ai-gateway-open.git   # requis ; placé à côté de ragatm-open/
cd ragatm-open/compact && scripts/preflight.sh                  # suite : compact/docs/installation.md
```

Le dépôt [ai-gateway-pool-open](https://github.com/ragatm-open/ai-gateway-pool-open) est **facultatif** :
le déploiement Direct (frontend + backend + Gateway) n'en a pas besoin.

Le déploiement de référence est **Compact** (Docker Compose, 9 services) : voir
[`compact/docs/installation.md`](compact/docs/installation.md). Matériel qualifié :
x86_64, 8 threads, 14 Go RAM, sans GPU ; ~3 Go d'images, ~4 Go de modèles téléchargés explicitement
pendant une phase d'approvisionnement distincte de l'exécution (hors ligne ensuite).

Développement : Python 3.12, PostgreSQL 16 + pgvector, Redis 7.2, Node 20.
Dépendances verrouillées à empreintes : `backend/requirements.lock` (image), `ci/requirements-ci.lock`
(CI), `frontend/package-lock.json`. Régénération : `backend/lock-runtime.sh`, `ci/lock-python.sh`.

## Configuration

Source unique : le registre typé `backend/config/registry/` ; référence générée
[`docs/10-reference/configuration.md`](docs/10-reference/configuration.md) et
[`docs/10-reference/variables-environnement.md`](docs/10-reference/variables-environnement.md).
Exemple : [`.env.example`](.env.example). Les secrets ne sont jamais versionnés ; les valeurs de
démonstration sont refusées par `manage.py check`.

## Sécurité et confidentialité

- Aucun provider externe (`AI_GATEWAY_FALLBACK_PROVIDER=none` seul admis, vérifié par des system checks).
- Réseau Compact : un seul port publié (frontend, `127.0.0.1`), réseaux internes, Ollama joignable
  uniquement par la Gateway. Un processus sur l'hôte Docker peut atteindre les services internes :
  voir [`compact/docs/configuration.md`](compact/docs/configuration.md) (« Accès depuis l'hôte »).
- Signalement de vulnérabilité : voir [`SECURITY.md`](SECURITY.md).
- Frontend : `next` 15.5.29 / React 19.2.9 — `npm audit` : 0 avis sur les dépendances de production et
  de développement au 2026-11-09 ; l'API d'optimisation d'images reste désactivée (`images.unoptimized`,
  inutilisée par l'application).

## Mises à jour

Les images de base sont épinglées par digest et les dépendances par empreintes : toute mise à jour est
un changement explicite (lock régénéré, suite de tests, CHANGELOG). Les modèles sont identifiés par
digest dans le registre de la Gateway ; changer de modèle d'embeddings impose une réindexation.

## Diagnostic et tests

- `manage.py check` (system checks `llm_router.*`, `config.*`, `rag_core.*`), `scripts/diagnose.sh`
  (Compact, non inférentiel), `scripts/retrieval-diagnostic.sh` (legs de retrieval, seuil, identité).
- Tests : `make test-backend-all` (suite standard, ~13 000 tests, PostgreSQL + Redis requis),
  `frontend : npm test` (vitest), `ci/` (points d'entrée CI portables, `.github/workflows/ci.yml`
  préparé mais non encore exécuté).

## Modèles

Aucun poids n'est distribué. Profil Compact : `granite-embedding:278m` (embeddings, Apache-2.0),
`qwen2.5:1.5b-instruct` (génération CPU, Apache-2.0), `BAAI/bge-reranker-v2-m3` (reranking, Apache-2.0).
Le profil 12 Go (`qwen2.5:7b-instruct`) est décrit mais **non qualifié**.

## Licence et contribution

Code original sous [Apache License 2.0](LICENSE) (voir aussi [`NOTICE`](NOTICE)). Les composants tiers conservent leurs licences : [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) (dont psycopg LGPL-3.0, certifi MPL-2.0, libvips LGPL-3.0, et les composants optionnels LibreDWG GPL-3.0 / PyMuPDF AGPL-3.0, jamais inclus par défaut). Contribution : [`CONTRIBUTING.md`](CONTRIBUTING.md) ; sécurité : [`SECURITY.md`](SECURITY.md) ; conduite : [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

## Documentation

[`docs/`](docs/) : architecture, ingestion, retrieval, IA/Gateway (`docs/06-ai/`), exploitation,
référence de configuration. Déploiement Compact : [`compact/docs/`](compact/docs/).
