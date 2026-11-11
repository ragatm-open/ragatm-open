# Documentation RagATM

Documentation technique de **RagATM**, plateforme RAG (Retrieval-Augmented Generation)
multi-tenant. Point d'entrée unique.

> Cette documentation est **reconstruite à partir du code réel** du dépôt. Le code est la
> source de vérité. Les fonctionnalités partielles, expérimentales ou non branchées sont
> explicitement signalées. Aucune fonctionnalité future n'est présentée comme disponible.

- **Langue** : français

## Je veux…

| Objectif | Aller à |
|---|---|
| Comprendre le produit | [01-overview/produit.md](01-overview/produit.md) |
| Comprendre l'architecture | [01-overview/architecture.md](01-overview/architecture.md) |
| Lancer RagATM en local | [02-getting-started/developpement-local.md](02-getting-started/developpement-local.md) |
| Comprendre le retrieval | [03-backend/pipeline-retrieval.md](03-backend/pipeline-retrieval.md) |
| Réduire le bruit structurel du contexte (questions, titres) | [03-backend/bruit-structurel-contexte.md](03-backend/bruit-structurel-contexte.md) |
| Comprendre l'ingestion des documents | [03-backend/ingestion-extraction.md](03-backend/ingestion-extraction.md) |
| Ingérer des courriels et leurs pièces jointes | [03-backend/ingestion-email.md](03-backend/ingestion-email.md) |
| Ingérer des fichiers audio (transcription ASR, citations horodatées) | [03-backend/ingestion-audio.md](03-backend/ingestion-audio.md) |
| Savoir quels traitements documentaires sont réellement actifs (écran admin « Capacités documentaires ») | [03-backend/capacites-documentaires.md](03-backend/capacites-documentaires.md) |
| Lister, chercher et trier les documents d'un RagSpace (contrat, compteurs) | [03-backend/liste-documents.md](03-backend/liste-documents.md) |
| Interroger un classeur Excel / CSV (moteur tabulaire) | [03-backend/tabular-query-architecture.md](03-backend/tabular-query-architecture.md) |
| Comprendre l'internationalisation (FR / EN) | [04-frontend/i18n.md](04-frontend/i18n.md) |
| Intégrer l'API | [05-api/vue-ensemble.md](05-api/vue-ensemble.md) |
| Configurer l'AI Gateway | [06-ai/ai-gateway.md](06-ai/ai-gateway.md) |
| Autoriser ou interdire la sortie de données vers un provider externe | [06-ai/politique-egress.md](06-ai/politique-egress.md) |
| Contrôler la langue des réponses IA (auto / fr / en) | [06-ai/langue-reponse.md](06-ai/langue-reponse.md) |
| Comprendre quels documents sont retrouvables (et pourquoi) | [03-backend/eligibilite-retrieval.md](03-backend/eligibilite-retrieval.md) |
| Comprendre l'answerability / no-answer | [06-ai/answerability.md](06-ai/answerability.md) |
| Instruire la non-exhaustivité des énumérations (rapport, non implémenté) | [06-ai/enumeration-non-exhaustivite.md](06-ai/enumeration-non-exhaustivite.md) |
| Savoir quand une question est traitée comme comparative | [06-ai/politique-comparative.md](06-ai/politique-comparative.md) |
| Instruire une relance après abstention (rapport, non implémenté) | [06-ai/relance-anti-sur-abstention.md](06-ai/relance-anti-sur-abstention.md) |
| Comprendre les permissions et l'isolation | [03-backend/permissions-isolation.md](03-backend/permissions-isolation.md) |
| Comprendre pourquoi une conversation longue réduit les sources | [03-backend/budget-de-prompt-memoire-et-sources.md](03-backend/budget-de-prompt-memoire-et-sources.md) |
| Comprendre pourquoi une réponse est coupée, et comment on le sait | [06-ai/troncature-des-reponses.md](06-ai/troncature-des-reponses.md) |
| Comprendre pourquoi un espace gouverné ne retrouve plus rien | [06-ai/derive-de-famille-de-pipeline.md](06-ai/derive-de-famille-de-pipeline.md) |
| Comprendre pourquoi un gros import est cadencé, et ce que fait un HTTP 429 | [04-frontend/import-cadence-et-reprise.md](04-frontend/import-cadence-et-reprise.md) |
| Supprimer définitivement un RagSpace (contrat, permissions, audit) | [03-backend/suppression-ragspace.md](03-backend/suppression-ragspace.md) |
| Diagnostiquer une mauvaise réponse à partir d'un pouce bas (incident, export, replay) | [03-backend/feedback-incidents.md](03-backend/feedback-incidents.md) |
| Lancer les tests et benchmarks | [08-testing/strategie.md](08-testing/strategie.md) |
| Trouver une variable d'environnement | [10-reference/variables-environnement.md](10-reference/variables-environnement.md) |
| Connaître le type, le défaut, les bornes et la portée d'un réglage | [10-reference/configuration.md](10-reference/configuration.md) *(généré)* |
| Connaître les limites connues | [10-reference/limites-connues.md](10-reference/limites-connues.md) |

## Parcours par profil

- **Développeur** → [02-getting-started/](02-getting-started/developpement-local.md) ·
  [03-backend/vue-ensemble.md](03-backend/vue-ensemble.md) ·
  [04-frontend/frontend.md](04-frontend/frontend.md) ·
  [08-testing/strategie.md](08-testing/strategie.md) ·
  [09-contributing/regles-developpement.md](09-contributing/regles-developpement.md)
- **Exploitant** → [10-reference/variables-environnement.md](10-reference/variables-environnement.md) ·
  [10-reference/ports.md](10-reference/ports.md) ·
  [02-getting-started/configuration.md](02-getting-started/configuration.md)
- **Administrateur** → [03-backend/permissions-isolation.md](03-backend/permissions-isolation.md) ·
  [03-backend/corpus-metier.md](03-backend/corpus-metier.md) ·
  [06-ai/ai-gateway.md](06-ai/ai-gateway.md)
- **Intégrateur API** → [05-api/](05-api/vue-ensemble.md)
- **Contributeur** → [09-contributing/](09-contributing/regles-developpement.md)

## Plan de la documentation

| Section | Contenu |
|---|---|
| [01-overview](01-overview/produit.md) | Produit, architecture, structure du dépôt, glossaire |
| [02-getting-started](02-getting-started/developpement-local.md) | Développement local, configuration, premier lancement, dépannage |
| [03-backend](03-backend/vue-ensemble.md) | Backend : modèle de données, permissions, ingestion, retrieval, chat, moteur tabulaire, suppression d'un RagSpace, tâches async, config dynamique |
| [04-frontend](04-frontend/frontend.md) | Frontend Next.js |
| [05-api](05-api/vue-ensemble.md) | API HTTP : vue d'ensemble, authentification, référence des endpoints, contrats d'erreur |
| [06-ai](06-ai/ai-gateway.md) | IA : AI Gateway, politique d'egress, providers, politiques LLM, embeddings, reranking, answerability |
| [08-testing](08-testing/strategie.md) | Stratégie de tests, benchmark RAG, benchmark modèles LLM |
| [09-contributing](09-contributing/regles-developpement.md) | Règles de développement, roadmap, backlog |

## Statut des fonctions principales

| Fonction | Statut |
|---|---|
| Recherche hybride (FTS + pgvector, RRF) | activé par défaut |
| Reranking | activable par flag (off) |
| No-answer par seuil de score | implémenté |
| Answerability déterministe (IDF, P1.8) | benchmarké, non branché live |
| Moteur tabulaire déterministe (Excel/CSV) | activable par flag (`TABULAR_QUERY_ENABLED=0` par défaut) |
| Ingestion multi-format + OCR local | implémenté (OCR off par défaut) |
| AI Gateway obligatoire (génération) | policy en env officiel |
| Streaming SSE | réel hors Gateway, pseudo-stream sinon |
| Coûts LLM | implémenté |
| Stockage documentaire | volume local (stockage objet écarté, [ADR-001](01-overview/decisions/ADR-001-stockage-documentaire.md)) |
| Sauvegarde/restauration | implémenté et restauration prouvée ; **non déployé** (timer non installé) |

## Stack

- **Backend** : Django + DRF, Celery + Redis, PostgreSQL 16 + pgvector. Voir [backend/pyproject.toml](../backend/pyproject.toml).
- **Frontend** : Next.js 15 (App Router) + React 19 + TypeScript. Voir [frontend/package.json](../frontend/package.json).
- **IA** : génération et embeddings via **AI Gateway** (accès direct ou via AI Gateway Pool).
- **Embeddings** : par RagSpace, via l'AI Gateway, dim 768 ; `stub` en tests.

## Conventions documentaires

- Terminologie stable (voir [glossaire](01-overview/glossaire.md)).
- Étiquettes de statut : *implémenté / activé par défaut / activable par flag / expérimental
  / benchmarké / planifié / non branché*.
- Source unique pour les variables, flags, ports, commandes : section [10-reference](10-reference/variables-environnement.md).
- Chaque doc technique majeure liste ses **références d'implémentation** (chemins de code).
- Vérification des liens : `python scripts/check_docs.py` (voir [09-contributing/regles-developpement.md](09-contributing/regles-developpement.md)).
