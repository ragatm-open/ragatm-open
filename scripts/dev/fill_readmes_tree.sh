#!/usr/bin/env bash
set -euo pipefail

ROOT="$(pwd)"

write_readme () {
  local path="$1"
  local title="$2"
  local body="$3"

  mkdir -p "$path"

  cat > "$path/README.md" <<EOF
# $title

## Emplacement
- Chemin (relatif racine repo) : \`$path\`

$body
EOF
}

# =========================
# RACINE
# =========================
write_readme "." "RAG-ATM (monorepo)" "
## Rôle
Monorepo du projet **RAG-ATM**, plateforme RAG professionnelle.

## Contenu
- \`backend/\` : Django API + logique applicative
- \`frontend/\` : Next.js (UX type ChatGPT)
- \`services/\` : services Docker (LLM local, OCR local)
- \`infra/\` : Postgres/Redis pour dev/prod via Docker
- \`deploy/\` : docker-compose, configurations dev/prod
- \`docs/\` : documentation + ADR
- \`shared/\` : contrats (OpenAPI, events) + outillage
- \`scripts/\` : scripts dev/prod

## Contraintes RAG-ATM (non négociables)
- Multi-RAG **étanche** (isolation stricte par espace)
- OCR **local sélectif**
- Hybrid search + rerank **conditionnel**
- LLM local + LLM externe (priorité privacy/coût)
- Réponse \"je ne sais pas\" si sources insuffisantes
- Budget LLM externe < 50€/mois
"

# =========================
# BACKEND
# =========================
write_readme "backend" "Backend (Django)" "
## Rôle
Backend Django principal : API, sécurité, orchestration RAG, routage LLM, audit et coûts.

## Contenu
- \`api/\` : endpoints HTTP versionnés
- \`apps/\` : domaines Django (accounts, chat, documents, rag_core, llm_router…)
- \`config/\` : settings Django (dev/prod)
- \`tests/\` : tests (dont règles RAG)
- \`media/\`, \`static/\` : fichiers uploadés / assets (selon config)

## Conventions
- Les views/serializers ne contiennent **pas** de logique métier.
- Les décisions (rerank, \"je ne sais pas\") sont prises côté backend (pas par le LLM).

## Sécurité & contraintes
- Isolation multi-RAG obligatoire (filtrage systématique)
- Logs et audit : minimiser PII, respecter privacy
"

write_readme "backend/api" "API HTTP" "
## Rôle
Couche HTTP : expose les endpoints consommés par le frontend et les intégrations.

## Contenu
- \`v1/\` : première version stable de l’API (versionnée)

## Conventions
- Validation via serializers
- Orchestration via services applicatifs
- Aucun accès direct DB dans les views si évitable (passer par services/repositories)
"

write_readme "backend/api/v1" "API v1" "
## Rôle
Version stable V1 de l’API.

## Contenu
- \`chat/\` : endpoints conversationnels
- \`documents/\` : endpoints documents (upload/ingestion/gestion)

## Compatibilité
- Les breaking changes se font via \`v2\` (pas en modifiant v1).
"

write_readme "backend/api/v1/chat" "API v1 – Chat" "
## Rôle
Endpoints chat : envoi d’un message, récupération d’une réponse et des sources.

## Contenu
- \`views.py\` : endpoints (REST)
- \`serializers.py\` : validation des inputs/outputs

## Flux attendu
1) validation input
2) appel \`backend/apps/chat/services/\`
3) récupération réponse + sources
4) réponse API (texte + citations + metadata)
"

write_readme "backend/api/v1/documents" "API v1 – Documents" "
## Rôle
Endpoints documents : upload, listing, suppression, statut d’ingestion.

## Flux attendu
1) upload + validations (type/taille)
2) stockage (selon stratégie)
3) ingestion via \`rag_core.services.ingest\`
4) indexation pgvector + métadonnées
"

# =========================
# APPS
# =========================
write_readme "backend/apps" "Apps Django (domaines)" "
## Rôle
Découpage par domaines fonctionnels.

## Apps
- \`accounts/\` : utilisateurs, auth, organisations/tenants (si retenu)
- \`audit/\` : traces, journaux, conformité
- \`billing/\` : suivi des coûts LLM (budget < 50€/mois)
- \`chat/\` : orchestration conversationnelle
- \`documents/\` : gestion documentaire
- \`llm_router/\` : routage LLM (local/externe)
- \`rag_core/\` : cœur RAG (retrieval, chunking, ranking, policies)

## Conventions
- Dépendances **unidirectionnelles** :
  - \`chat\` et \`documents\` peuvent appeler \`rag_core\`
  - \`rag_core\` ne dépend pas de \`chat/documents\`
"

write_readme "backend/apps/accounts" "accounts" "
## Rôle
Gestion des utilisateurs et de l’accès (authN/authZ) dans un contexte multi-utilisateurs.

## Contenu attendu
- modèles User/Profil (ou extension)
- permissions/roles (si nécessaire)
- endpoints auth (via API)

## Contraintes RAG-ATM
- Pas de quotas (mais contrôle d’accès strict)
- Les ressources RAG doivent être isolées par espace (rag_space)
"

write_readme "backend/apps/audit" "audit" "
## Rôle
Traçabilité des actions : qui a fait quoi, quand, et sur quel espace/document/chat.

## Contenu attendu
- événements audit (create/read/update/delete)
- logs structurés (minimiser PII)
- export/rotation (via scripts prod)

## Contraintes RAG-ATM
- Logs conservés 1 mois (selon tes specs)
- Privacy d’abord : éviter de logger du contenu sensible
"

write_readme "backend/apps/billing" "billing" "
## Rôle
Suivi et contrôle des coûts LLM (local vs externe) par espace / utilisateur / période.

## Contenu attendu
- enregistrement tokens / coût / provider
- politiques de bascule (forcer local si budget atteint)
- dashboards/exports (plus tard)

## Contraintes RAG-ATM
- Budget LLM externe < 50€/mois : le backend doit pouvoir imposer une policy.
"

write_readme "backend/apps/chat" "chat" "
## Rôle
Orchestration conversationnelle : assemblage contexte, appel RAG, appel LLM.

## Contenu
- \`services/\` : cas d’usage chat

## Conventions
- Le chat n’implémente pas le retrieval bas niveau : délègue à \`rag_core\`.
- Le chat ne parle pas directement à un provider LLM : passe par \`llm_router\`.
"

write_readme "backend/apps/chat/services" "chat/services" "
## Rôle
Services applicatifs du domaine chat.

## Contenu attendu
- \`chat_service.py\` : point d’entrée principal (use-case “répondre à un message”)

## Responsabilités typiques
- charger l’historique
- appeler \`rag_core.services.query\`
- appliquer policy (rerank conditionnel, 'je ne sais pas')
- appeler \`llm_router\`
- retourner : réponse + sources + métriques
"

write_readme "backend/apps/documents" "documents" "
## Rôle
Gestion documentaire (métadonnées, stockage, permissions, statuts ingestion).

## Contenu attendu
- modèles documentaires (métadonnées)
- validation types (PDF, images…)
- déclenchement ingestion (texte direct ou OCR sélectif)

## Contraintes RAG-ATM
- OCR local uniquement et **sélectif**
- chaque document appartient à un **rag_space**
"

write_readme "backend/apps/llm_router" "llm_router" "
## Rôle
Routage LLM : choisir entre LLM local et externe selon policy (privacy/coût).

## Responsabilités
- normaliser l’interface : prompt -> réponse + tokens + coût + latence
- appliquer politiques : forcer local, bloquer externe si budget/contraintes
- gérer secrets provider côté backend (jamais côté frontend)

## Contraintes RAG-ATM
- Router \"bête\" : pas de logique métier RAG.
"

write_readme "backend/apps/rag_core" "rag_core" "
## Rôle
Cœur RAG : ingestion, chunking, indexation, retrieval, hybrid search, rerank conditionnel, politiques.

## Dossiers
- \`domain/\` : règles métier pures (sans I/O)
- \`services/\` : orchestration des cas d’usage
- \`models/\` : modèles Django (RagSpace, Document, Chunk…)
- \`repositories/\` : accès DB (pgvector, hybrid search)

## Contraintes RAG-ATM
- Multi-RAG étanche : **rag_space_id obligatoire partout**
- pgvector = vérité unique
- \"je ne sais pas\" si sources insuffisantes
"

write_readme "backend/apps/rag_core/domain" "rag_core/domain" "
## Rôle
Logique métier RAG pure (sans Django/DB).

## Contenu attendu
- stratégies retrieval (vector/hybrid)
- chunking (découpage + nettoyage)
- ranking/fusion (scoring, BM25+vector fusion si retenu)
- policies (quand rerank, quand dire \"je ne sais pas\")

## Conventions
- pas d’I/O (pas DB, pas réseau, pas filesystem)
- fonctions testables unitairement
"

write_readme "backend/apps/rag_core/services" "rag_core/services" "
## Rôle
Orchestration des cas d’usage RAG.

## Contenu attendu
- \`ingest.py\` : ingestion (texte, OCR sélectif, chunking, embeddings)
- \`query.py\` : recherche (hybrid + rerank conditionnel)
- \`feedback.py\` : feedback utilisateur (optionnel), logs qualité

## Conventions
- gère l’I/O en s’appuyant sur repositories et services externes (OCR/LLM)
"

write_readme "backend/apps/rag_core/models" "rag_core/models" "
## Rôle
Modèles Django liés au RAG.

## Modèles typiques (à implémenter)
- RagSpace : espace RAG étanche (multi-RAG)
- Document : métadonnées + état ingestion
- Chunk : texte + embedding + provenance (doc/page/offset)

## Contraintes
- Filtrage rag_space systématique
- Index pgvector + indexes texte (pour hybrid)
"

write_readme "backend/apps/rag_core/repositories" "rag_core/repositories" "
## Rôle
Accès aux données (Postgres + pgvector) et requêtes de retrieval.

## Contenu attendu
- fonctions requêtes vectorielles (cosine/L2 selon choix)
- hybrid search (texte + vecteur)
- filtres sécurité (rag_space_id, permissions)

## Conventions
- pas de logique métier (uniquement accès données)
- optimisation contrôlée (indexes, EXPLAIN)
"

# =========================
# CONFIG / SETTINGS
# =========================
write_readme "backend/config" "config (Django)" "
## Rôle
Configuration Django (settings, URLs, WSGI/ASGI, etc.).

## Contenu
- \`settings/\` : settings par environnement (dev/prod)

## Conventions
- secrets via variables d’environnement (pas en dur)
- config DB Postgres + pgvector
"

write_readme "backend/config/settings" "config/settings" "
## Rôle
Settings Django par environnement.

## Contenu attendu
- dev.py : dev local
- prod.py : prod
- base.py : commun

## Contraintes
- Séparer strictement dev/prod
- Logs configurables, pas de données sensibles dans les logs
"

# =========================
# MEDIA / STATIC
# =========================
write_readme "backend/media" "media" "
## Rôle
Stockage local des fichiers uploadés (si activé en dev).

## Notes
En prod, ce répertoire peut être remplacé par un volume ou un stockage dédié selon la stratégie retenue (privacy d’abord).
"

write_readme "backend/static" "static" "
## Rôle
Fichiers statiques Django (admin, etc.) selon la config.

## Notes
La stratégie exacte dépendra de dev/prod (Docker volumes).
"

# =========================
# TESTS
# =========================
write_readme "backend/tests" "tests" "
## Rôle
Tests backend (pytest/pytest-django).

## Conventions
- tests unitaires : domain (rag_core/domain)
- tests d’intégration : services + DB (pgvector)
- tests de sécurité : étanchéité multi-RAG
"

write_readme "backend/tests/rag_core" "tests/rag_core" "
## Rôle
Tests du cœur RAG.

## Tests critiques
- retrieval : renvoie les bons chunks
- rerank policy : rerank seulement quand nécessaire
- no source : \"je ne sais pas\" si pas assez de sources
"

# =========================
# DEPLOY
# =========================
write_readme "deploy" "deploy" "
## Rôle
Déploiement via Docker (dev/prod).

## Contenu
- \`docker/dev\` : stack dev
- \`docker/prod\` : stack prod

## Conventions
- secrets via env vars
- volumes maîtrisés
"

write_readme "deploy/docker" "deploy/docker" "
## Rôle
Configurations Docker (dev/prod).

## Conventions
- dev : simple, rapide, logs verbeux
- prod : durci (security), logs + rotation, backups
"

write_readme "deploy/docker/dev" "deploy/docker/dev" "
## Rôle
Stack Docker de développement.

## Objectif
Permettre une boucle dev rapide (Django + Postgres + Redis + services).
"

write_readme "deploy/docker/prod" "deploy/docker/prod" "
## Rôle
Stack Docker de production.

## Objectif
Déploiement durci : sécurité, volumes, sauvegardes, rotation logs, monitoring minimal.
"

# =========================
# DOCS
# =========================
write_readme "docs" "docs" "
## Rôle
Documentation du projet.

## Contenu
- architecture, sécurité, runbook
- décisions (ADR)
- docs RAG (pipeline, hybrid, policies)

## Conventions
- La doc doit refléter la réalité du code (pas de vœux pieux)
"

write_readme "docs/decisions" "docs/decisions (ADR)" "
## Rôle
Architecture Decision Records : décisions structurantes et traçables.

## Conventions
- 1 décision = 1 fichier ADR
- contexte -> décision -> conséquences
"

# =========================
# FRONTEND
# =========================
write_readme "frontend" "frontend (Next.js)" "
## Rôle
Interface utilisateur type ChatGPT.

## Conventions
- aucune clé LLM côté client
- tout passe par l’API backend
- UX simple : chat + sources + gestion documents

## Dossiers
- \`src/app\` : routes (App Router)
- \`src/components\` : UI
- \`src/lib\` : client API, helpers
- \`src/types\` : types TS
"

write_readme "frontend/src" "frontend/src" "
## Rôle
Code source frontend.

## Conventions
- appels API centralisés dans \`lib/\`
- types partagés dans \`types/\`
"

write_readme "frontend/src/app" "frontend/src/app" "
## Rôle
Routes et pages Next.js (App Router).

## Contenu attendu
- page chat
- page documents
- page settings (policies, etc.) si prévu
"

write_readme "frontend/src/components" "frontend/src/components" "
## Rôle
Composants UI réutilisables.

## Contenu attendu
- chat UI (liste messages)
- message bubble + citations
- upload documents
"

write_readme "frontend/src/lib" "frontend/src/lib" "
## Rôle
Client API et utilitaires.

## Conventions
- une couche d’accès API (fetch wrappers)
- gestion erreurs standardisée
- pas de logique métier RAG côté client
"

write_readme "frontend/src/styles" "frontend/src/styles" "
## Rôle
Styles globaux et utilitaires CSS/Tailwind (selon choix).

## Conventions
- styles simples, maintenables
"

write_readme "frontend/src/types" "frontend/src/types" "
## Rôle
Types TypeScript partagés.

## Contenu attendu
- DTO API (ChatRequest, ChatResponse, Source…)
- types UI
"

# =========================
# INFRA
# =========================
write_readme "infra" "infra" "
## Rôle
Infra locale (principalement Docker) : Postgres + Redis.

## Contenu
- \`postgres/initdb\` : scripts init (extensions, rôles)
- \`redis\` : config redis si nécessaire
"

write_readme "infra/postgres" "infra/postgres" "
## Rôle
Configuration Postgres pour dev/prod.

## Points clés
- activation extension pgvector (via initdb)
- paramètres de perf adaptés (à préciser en prod)
"

write_readme "infra/postgres/initdb" "infra/postgres/initdb" "
## Rôle
Scripts d’initialisation Postgres (executés au démarrage du conteneur).

## Contenu attendu
- création extension pgvector
- création rôles/db si nécessaire
"

write_readme "infra/redis" "infra/redis" "
## Rôle
Redis (cache / queue légère si retenue).

## Notes
Rester minimal : pas indispensable tant que non justifié.
"

# =========================
# SCRIPTS
# =========================
write_readme "scripts" "scripts" "
## Rôle
Scripts utilitaires.

## Conventions
- scripts destructifs : annoncer clairement et demander confirmation
- scripts prod : backups/restore/rotation logs
"

write_readme "scripts/dev" "scripts/dev" "
## Rôle
Scripts de dev local.

## Contenu
- bootstrap.sh : préparer l’environnement dev
- reset_db.sh : reset base dev (destructif)
"

write_readme "scripts/prod" "scripts/prod" "
## Rôle
Scripts de production.

## Contenu
- backup.sh : sauvegarde
- restore.sh : restauration
- rotate_logs.sh : rotation logs

## Contraintes
- scripts robustes, idempotents quand possible
- logs clairs (audit)
"

# =========================
# SERVICES
# =========================
write_readme "services" "services" "
## Rôle
Services isolés du backend (Docker) : LLM local, OCR local.

## Conventions
- stateless si possible
- interfaces stables (contrats)
- privacy : pas de fuite de données
"

write_readme "services/llm_local" "services/llm_local" "
## Rôle
Serveur de LLM local (on-prem).

## Contenu
- Dockerfile
- config/ : configs runtime
- server/ : code serveur

## Contraintes
- aucune donnée utilisateur persistée
- API stable pour llm_router
"

write_readme "services/llm_local/config" "services/llm_local/config" "
## Rôle
Configuration du serveur LLM local.

## Contenu attendu
- fichiers de config (modèles, limites, options)
- variables d’environnement documentées
"

write_readme "services/llm_local/server" "services/llm_local/server" "
## Rôle
Code du serveur LLM local.

## Contenu attendu
- endpoints (HTTP) pour génération
- normalisation input/output
- métriques minimales (latence, tokens)
"

write_readme "services/ocr" "services/ocr" "
## Rôle
OCR local (sélectif) pour PDFs non textuels et images scannées.

## Contrat
- input : fichier + options (langue, dpi…)
- output : texte + métadonnées (idéalement coordonnées)

## Contraintes RAG-ATM
- OCR uniquement si nécessaire (sélectif)
- pas d’envoi externe (privacy)
"

# =========================
# SHARED
# =========================
write_readme "shared" "shared" "
## Rôle
Éléments partagés : contrats (OpenAPI/events) et outillage (codegen).

## Objectif
Source de vérité des interfaces entre frontend/backend/services.
"

write_readme "shared/contracts" "shared/contracts" "
## Rôle
Contrats d’interface.

## Contenu
- openapi.yaml : contrat API
- events.json : schéma des événements (si utilisés)

## Conventions
- versionner les changements
- éviter les breaking changes sans nouvelle version d’API
"

write_readme "shared/tooling" "shared/tooling" "
## Rôle
Outillage commun (génération, lint, helpers).

## Contenu
- codegen/ : génération à partir des contrats
"

write_readme "shared/tooling/codegen" "shared/tooling/codegen" "
## Rôle
Génération automatique à partir des contrats.

## Contenu attendu
- génération clients (TS) depuis OpenAPI (optionnel)
- validation schémas events (optionnel)
"

echo "✅ README complétés sur l’arborescence cible."
echo "ℹ️  Vérifie avec: git diff"
