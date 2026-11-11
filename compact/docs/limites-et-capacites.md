# Limites matérielles, qualification et capacités désactivées

## Profils

| Profil | Modèle de génération | Matériel | Verdict |
|---|---|---|---|
| CPU (8 Go) | `compact-generation-8` = `qwen2.5:1.5b-instruct` | 8 cœurs, 14 Go RAM, sans GPU | **qualification fonctionnelle exécutée** (dossier de qualification interne) |
| 12 Go GPU | `compact-generation` = `qwen2.5:7b-instruct` | GPU 12 Go isolé | **NOT_EXECUTED** (aucun GPU de qualification disponible) |
| 8 Go GPU | `compact-generation-8` | GPU 8 Go isolé | **NOT_EXECUTED** |

Le modèle 1.5 B sert à démontrer le fonctionnement du parcours (ingestion, retrieval, reranking,
génération, citations). Ses réponses ne constituent pas une qualification de qualité documentaire
et ne préjugent pas du profil 12 Go.

## Limites connues

- Génération CPU lente (voir mesures de latence dans le dossier de qualification interne) ; délais relevés
  à 300 s (Gateway) et 600 s (gunicorn).
- Reranker TEI CPU : ~3,8 Go résidents ; `--max-batch-tokens 4096 --auto-truncate` obligatoires
  sous 7 Go ; pool de 20 passages.
- Bornes d'entrée : embeddings ≤ 1 200 caractères par entrée (reject) — le chunking gouverné de
  RAGatm produit des chunks ≤ 1 200 car. ; rerank ≤ 2 000 car. (requête) / 4 000 car. (document) ;
  corps JSON Gateway ≤ 2 Mio ; upload ≤ 50 Mo (`UPLOAD_MAX_FILE_SIZE_MB`).
- Un seul worker Celery (concurrence 2) ; ingestion séquentielle à l'échelle d'un poste.
- Pas de TLS dans la pile : à placer derrière un reverse-proxy pour tout accès hors boucle locale.
- Aucune authentification entre services (confiance réseau : réseaux internes).

## Capacités désactivées (profil Compact)

| Capacité | État | Effet |
|---|---|---|
| ASR (audio) | OFF (`AI_GATEWAY_ASR_ENABLED=0`, `DOCUMENT_AUDIO_*=0`) | `/v1/audio/*` ⇒ `503 asr_unavailable` ; upload audio refusé par RAGatm |
| Vision / analyse visuelle | OFF | `/v1/vision/*` ⇒ `503 vision_not_enabled` ; images non analysées |
| StructX (extraction structurée) | **absent** de la distribution | route absente (404), capacité `UNAVAILABLE` |
| OCR | OFF (`DOCUMENT_OCR_ENABLED=0`, image sans Tesseract) | PDF image non texté ⇒ échec propre (`ocr_engine_missing`) |
| IFC / DWG / audio (workers dédiés) | non construits (`WITH_IFC/DWG/AUDIO=0`) | formats refusés proprement |
| `.msg` (extract-msg, GPL) | retiré | `.eml` seul |
| PyMuPDF (AGPL) | hors cœur | moteur PDF = pypdf (+ pdfplumber pour les tableaux) |
| `/v1/chat*` Gateway | aucun modèle Compact ne déclare `chat` | 400 ; RAGatm n'utilise que `/v1/generate*` |
| AI Gateway Pool | absent | `AI_GATEWAY_POOL_HOSTS` vide |
| Fournisseurs d'IA externes | inexistants | `config.E009` / `llm_router.E009` refusent toute configuration |

## Licences (inventaire préparatoire — validation)

Code RAGatm Open / AI Gateway Open : aucune licence déclarée (propriétaire ; Apache-2.0 proposé
lors de la qualification, non attribué ici). Images : PostgreSQL (PostgreSQL License) + pgvector (PostgreSQL
License), Redis 7.2 (BSD-3), Ollama (MIT), TEI (Apache-2.0), python:3.12-slim / node:20-alpine
(licences des distributions). Modèles : Granite Embedding 278M, Qwen2.5 1.5B/7B Instruct,
bge-reranker-v2-m3 — Apache-2.0. Dépendances Python/Node : permissives (cf.
le dossier de qualification interne, SBOM à produire lors de la qualification). Fixtures : corpus synthétique OFLA
produit par `fixtures/generate_corpus.py`.
