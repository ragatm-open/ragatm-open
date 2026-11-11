# Feature flags

Référence de tous les drapeaux booléens de RagATM. **Source unique** : les autres docs
pointent ici. Public : ops et développeurs. Le CODE fait foi (`backend/config/settings/base.py`,
sauf indication).

Un flag se pilote par variable d'environnement (voir
[variables-environnement.md](variables-environnement.md)). La colonne « Défaut » est la valeur
de code ; certaines diffèrent en env officiel (colonne « Effet »). « Activer » un rollback =
remettre le flag à `0`.

## IA / Gateway

| Flag | Défaut | Effet quand activé |
|---|---|---|
| `AI_GATEWAY_ENABLED` | `0` (env officiel = `1`) | Génération routée via la Gateway (sinon system check `llm_router.E001`). |
| `AI_GATEWAY_STREAMING_ENABLED` | `0` | Streaming natif du chat via `/v1/generate/stream` (NDJSON). À `0`, pseudo-stream historique. Repli d'exploitation = repasser à `0` + redémarrage. Voir [ai-gateway §4bis](../06-ai/ai-gateway.md#4bis-streaming-du-chat-ai_gateway_streaming_enabled--activable-par-flag). |
| `CAPABILITIES_GATEWAY_PROBES_ENABLED` | `0` | *Activable par flag*. Sondes AI Gateway de la console d'administration (accueil `/admin-core` et écran « Capacités documentaires ») : `GET /health/runtime`, `/v1/models`, `/health/vision`, `/health/asr`. **Fermées par défaut** : leur caractère non inférentiel n'est pas attesté sur le code Gateway déployé. À `0` : aucun appel Gateway depuis la console, services `UNKNOWN` (`probes_disabled`). N'ouvrir qu'après attestation. Voir [capacites-documentaires.md](../03-backend/capacites-documentaires.md). |
| `AI_GATEWAY_FALLBACK_ENABLED` | `0` | Réservé : aucun provider externe n'existe dans l'édition Open, donc aucun repli. Provider de repli = `none` uniquement. |
| `ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS` | `0` | Dégrade les checks IA `E001`/`E003` en Warning (tests uniquement). |

## Routeur d'actions

| Flag | Défaut | Effet quand activé |
|---|---|---|
| `ACTIONS_ROUTER_ENABLED` | `0` | Le chat délègue la détection au routeur central `apps.actions` (mutations jamais déclenchables par phrase). |
| `ACTIONS_LLM_CLASSIFIER_ENABLED` | `0` | Classification LLM structurée du routeur (local, sans fallback/retry externe). |
| `ACTIONS_ALLOW_NL_DELETE` | `0` | Autorise le déclenchement de `document.delete` en langage naturel (destructif, fortement déconseillé). |
| `ACTION_AUDIT_ENABLED` | `1` | Émission des `ActionAuditEvent` (fail-open ; rollback = `0`). |

## Retrieval / RAG

| Flag | Défaut | Effet quand activé |
|---|---|---|
| `RAG_HYBRID_ENABLED` | `1` | Fusion hybride vectoriel + FTS (RRF). |
| `RAG_MODE_B_FALLBACK_ENABLED` | `0` | Réponse LLM « hors documents » si aucune source pertinente (préfixe d'avertissement). |
| `RAG_ANSWER_LANGUAGE_POLICY_ENABLED` | `0` | Politique de **langue de réponse IA** (P0). OFF ⇒ prompts français historiques inchangés, **et une préférence utilisateur `en` est ignorée**. ON ⇒ langue pilotée par la préférence `answer_language` (`auto`/`fr`/`en`). Distinct de `preferred_locale` (interface). Clé **statique** (pas d'override Admin Core) : bascule = variable d'environnement + redéploiement backend **et** workers. Seules `0`/`1` acceptées. Cf. [06-ai/langue-reponse.md](../06-ai/langue-reponse.md). |
| `RAG_RETRIEVAL_DIVERSITY_ENABLED` | `1` | Diversification du pool de candidats (P1.4). |
| `RAG_RRF_CONDITIONAL_LEXICAL_WEIGHT_ENABLED` | `1` | Renfort lexical RRF sur identifiants forts (P1.5, poids 1.5). |
| `RAG_RERANK_ENABLED` | `0` | Rerank **historique** (étape 8), modèle chargé dans le process. **Exclusif de `RAG_RERANKER_ENABLED`** : les deux à `1` font échouer le démarrage (`rag_core.E008`). Voir [../06-ai/reranking.md](../06-ai/reranking.md). |
| `RAG_RERANKER_ENABLED` | `0` | **Reranking de passages via l'AI Gateway** (`/v1/rerank`). Distinct de `RAG_RERANK_ENABLED` : ce chemin n'embarque aucun modèle local, et **les deux ne peuvent pas être actifs ensemble** (`rag_core.E008`). *Activé en staging **et en production** (constaté le 22/08/2026), et **jamais qualifié** : ni recall, ni latence, ni taux de repli mesurés.* Exigé identique entre les deux environnements par le manifeste de parité. Voir [../06-ai/reranking.md](../06-ai/reranking.md) §6. |
| `RAG_RERANKER_BASE_URL` | *(vide)* | URL de l'**AI Gateway** — jamais celle du backend de reranking. |
| `RAG_RERANKER_MODEL_LOGICAL` | `rerank-default` | Nom **logique** du modèle. RagATM n'utilise jamais le nom du dépôt Hugging Face. |
| `RAG_RERANKER_POOL_K` | `40` | Profondeur soumise au reranking, prélevée sur l'ordre **RRF canonique**. |
| `RAG_RERANKER_TIMEOUT_SECONDS` | `2.0` | Timeout **dédié**, jamais partagé avec le LLM. |
| `RAG_RERANKER_FAILURE_POLICY` | `fail_open_rrf` | `fail_open_rrf` (repli sur l'ordre RRF, **compté et journalisé**) ou `fail_closed` (l'erreur remonte). |
| `RAG_RERANKER_MAX_PER_GROUP` | `3` | Plafond par document **du chemin reranké uniquement**. Drapeau OFF ⇒ plafond historique inchangé. |
| `RAG_OPS_METRICS_TOKEN` | *(vide)* | Jeton facultatif de `/ops/metrics`. Vide : la route reste servie **hors de `/api/`**, donc non routée par le proxy public. Renseigné : `Authorization: Bearer <jeton>` exigé. la documentation d'exploitation (hors périmètre de cette édition) §6bis. |
| `RAG_ANSWERABILITY_ENABLED` | `0` | Answerability par cross-encoder. **Mesuré, non promu** (P2.0) : puissant sur données réalistes, mais 0/192 combinaisons éligibles — le gate est bloqué par la granularité de `demo-fr-extended`. |
| `RAG_ANSWERABILITY_MODEL` | `BAAI/bge-reranker-base` | Modèle **local** (extra `rerank`). Score **sigmoïde [0,1]**, pas un logit. Aucun repli distant. |
| `RAG_ANSWERABILITY_THRESHOLD` | `0.5` | Seuil sur le score sigmoïde. Non calibré (aucun seuil retenu). |
| `RAG_ANSWERABILITY_STRATEGY` | `max` | `max` / `mean_top2` / `max_guarded` / `composite` (protection identifiant fort). |
| `RAG_ANSWERABILITY_TOP_K` | `5` | Paires scorées. top-10 double le coût et dépasse le budget de 3 s. |
| `RAG_ANSWERABILITY_DEVICE` | `cpu` | `cuda` retombe sur CPU si aucun GPU (~300 ms/paire). |
| `RAG_ANSWERABILITY_FAIL_OPEN` | `1` | Une panne du modèle ne doit pas devenir un refus massif ; l'indisponibilité est tracée, jamais silencieuse. |
| `RAG_INGEST_ASYNC_DEFAULT` | off (vide) | Ingestion asynchrone par défaut sur les endpoints. |
| `RAG_TASK_ROUTING_ENABLED` | `0` | Override du modèle par type de tâche (nom seulement, espaces locaux). |
| `RAG_REPHRASING_SUGGESTIONS_ENABLED` | `0` | Suggestions de reformulation sur non-réponse (async, opt-in, non bloquant). |
| `RAG_QUERY_REWRITE_ENABLED` | `0` | Réécriture LLM de requête (mode explicatif seul). Lu dans `rewrite.py`. Défaut passé à `0` le 30/07/2026 : gain retrieval nul et P95 doublé — voir [configuration.md](configuration.md). |
| `RAG_KEYWORDIZE_ENABLED` | `1` | Keywordization de requête (mode explicatif seul). Lu dans `rewrite.py`. |

> ⚠️ `RAG_ENABLE_NEWS_HEURISTIC` (défaut `False`) et d'autres flags `RAG_*` sont lus via
> `getattr()` mais **non déclarés dans les settings** : les poser est un no-op silencieux.
> Voir [limites-connues.md](limites-connues.md).

## Résumés / mémoire

| Flag | Défaut | Effet quand activé |
|---|---|---|
| `SUMMARY_ASYNC_ENABLED` | `1` | Résumés longs en tâche Celery (map-reduce). |
| `CHAT_MEMORY_SUMMARY_ENABLED` | `1` | Résumé hiérarchique de la mémoire de conversation (niveau 2). |
| `DOMAIN_CORPUS_SUMMARY_ENABLED` | `1` | Résumé d'un document/page de **corpus métier** depuis le panneau de sources (endpoint dédié `domain-corpus-documents/`). **Activé par défaut** ; à `0` l'API répond `404`, indistinguable d'un document inexistant. Rollback = `0` + redémarrage ; les résumés déjà produits sont conservés. Voir [../03-backend/resume-corpus-metier.md](../03-backend/resume-corpus-metier.md). |

## Analyses corpus / config Core

| Flag | Défaut | Effet quand activé |
|---|---|---|
| `CORE_CONFIG_EDIT_ENABLED` | `0` | Édition d'overrides de config Core (jamais prod, staging seulement). |
| `CORE_CONFIG_RUNTIME_ENABLED` | `0` | Branche l'overlay runtime de config (requiert `DJANGO_CACHE_URL`, check `rag_core.E003`). |
| `CORPUS_GLOBAL_SEARCH_ALLOW_EXTERNAL_FALLBACK` | `0` | Autorise le repli externe pour les analyses globales (impact confidentialité). |

## Extraction / documents

| Flag | Défaut | Effet quand activé |
|---|---|---|
| `DOCUMENT_OCR_ENABLED` | `0` | OCR PDF automatique à l'ingestion (force l'async). |
| `DOCUMENT_OCR_MIN_ORIENTATION_CONFIDENCE` | `10.0` | Confiance OSD minimale pour pivoter une page. Sous ce seuil, aucune rotation : une rotation à tort détruit une page lisible, une rotation manquée non. |
| `DOCUMENT_IMAGE_INGEST_ENABLED` | `1` | Accepte et OCRise systématiquement les images. |
| `DOCUMENT_VISUAL_ANALYSIS_ENABLED` | `0` | *Activable par flag.* Analyse visuelle via l'AI Gateway (`/v1/vision/analyze`) des images autonomes et des visuels **intégrés** d'un PDF, d'un DOCX ou d'un PPTX (images raster intégrées seulement) : l'OCR reste exécuté, la vision l'enrichit ou devient l'évidence PRIMARY quand aucune évidence non visuelle n'est utile. Ingestion toujours asynchrone pour les sources ouvertes, routée vers la file `ingest_visual` — **n'activer que si un worker `--queues=ingest_visual` tourne** (`worker_visual`, déployé en staging et en production). *Activé en staging et en production* (production depuis une version antérieure, **en l'état** : la précision documentaire de `describe` n'est pas qualifiée — couverture des faits mesurée à 0,19 lors des audits antérieurs). Éteindre la porte ne retire jamais en silence une évidence visuelle publiée (réindexation refusée `visual_analysis_disabled_would_degrade`, sauf `drop_visual_evidence=true`). Seule la tâche `describe` est qualifiée côté Gateway : description factuelle et textes visibles ; tableaux tramés, diagrammes, graphiques, dessins techniques et images mixtes **ne sont pas qualifiés** (400 `vision_task_not_qualified`). |
| `DOCUMENT_PDF_VECTOR_REGION_RENDER_ENABLED` | `0` | Graphiques et schémas **vectoriels** des PDF (*activable par flag*) : régions de tracés détectées et rendues une à une (jamais la page entière), analysées par le `describe` existant, budget visuel PARTAGÉ avec les images intégrées. Exige aussi l'espace dans `DOCUMENT_PDF_VECTOR_REGION_RAGSPACE_IDS` (UUID complets, vide = aucun) et la source `pdf` de l'analyse visuelle. Bornes : `DOCUMENT_PDF_VECTOR_REGION_RENDER_DPI` (150), `DOCUMENT_PDF_VECTOR_REGION_MAX_CANDIDATES` (24). À poser dans backend, worker, worker_reindex, worker_visual. Documents existants changés seulement à la réindexation. |
| `DOCUMENT_VISUAL_ANALYSIS_SOURCES` | `image` | Natures de source soumises à l'analyse visuelle quand la porte est ouverte. `image` (image autonome), `pdf`, `docx` et `pptx` (visuels intégrés, rasters seulement — aucun rendu, donc aucun visuel purement vectoriel : ni SmartArt, ni graphique ou forme PowerPoint, ni SVG/EMF/WMF ; tracés PDF seulement avec `DOCUMENT_PDF_VECTOR_REGION_RENDER_ENABLED`, voir ci-dessus) sont opérationnelles ; toute autre valeur est ignorée. Le défaut n'ouvre que `image`, et chaque source a sa **propre** porte : ajouter `pdf`, `docx` ou `pptx` route aussi ces documents vers `ingest_visual` et force leur ingestion en asynchrone (`pptx` exige aussi `DOCUMENT_PPTX_ENABLED=1` ; aucun drapeau visuel propre au PPTX). Un DOCX n'étant pas paginé avant rendu, ses fragments visuels sont publiés **sans numéro de page** ; ceux d'un PPTX portent leur **diapositive** (`slide_number`), jamais une page. |
| `DOCUMENT_TABLE_OCR_ENABLED` | `0` | Reconstruction de tableaux depuis l'OCR (P3-b, expérimental/fragile). |
| `DOCUMENT_PDF_GENERIC_TABLES_ENABLED` | `0` | Tableaux PDF **génériques** : reconstruit ligne, colonne et page source depuis la mise en page (*activable par flag*). **Allumé, ne suffit plus** : seuls les RagSpace de `DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS` en bénéficient. Éteint, l'indexation PDF reste par page. Lu à l'EXTRACTION par le processus qui extrait : à poser ensemble dans `backend` (ingestion synchrone, commandes), `worker` (file `celery`, ingestion asynchrone et réindexation unitaire), `worker_reindex` (file `reindex`) et `worker_visual` (file `ingest_visual` : reprise primaire `visual_primary` d'un PDF dont la seule évidence est visuelle) — un seul service OFF produit des documents sans tableaux. Activation sur un stock existant = réindexation (`reindex_documents --document-id … --backup`) ; retour arrière = drapeau OFF puis réindexation (répété en staging, lot PRODUCTION-BLOCKERS-14 : état OFF reproduit au caractère et au vecteur près). Chaque réindexation régénère les identifiants de fragments (les citations passées gardent extrait et document). |
| `DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS` | vide | Liste des RagSpace (UUID **complets**, séparés par des virgules) où le rail tabulaire générique s'applique quand le drapeau ci-dessus est allumé (*implémenté*). **Vide = aucun espace** (jamais « tous ») ; une seule entrée qui n'est pas un UUID complet (préfixe, nom, `*`, forme sans tirets) **invalide toute la liste** ; un espace de `STRUCTURED_VISUAL_RAGSPACE_ALLOWLIST` est toujours exclu (pas de superposition avec le rail visuel structuré). Décision prise par document d'après SON espace, transmise à l'extraction (processus enfant compris) : aucun état mémorisé par le worker. Même règle de cohérence que le drapeau : à poser ensemble dans `backend`, `worker`, `worker_reindex`, `worker_visual` (recréation requise : un service non recréé garde l'ancienne valeur — constaté en staging). Qualifié en staging : ingestion asynchrone, réindexation `worker_reindex`, commande `reindex_documents`, trois espaces dans les mêmes processus, aucune contamination ; retrait de l'espace puis réindexation = état OFF au texte et au vecteur près. Un document déjà indexé ne change qu'à sa réindexation. |
| `DOCUMENT_MINERU_HYBRID_ENABLED` | `0` | Rail hybride **Document Vision** : MinerU en enrichissement sélectif page par page — raster sans texte natif, tableaux, géométrie (*expérimental, prototype hors ligne*). L'extraction native garde l'autorité et n'est jamais remplacée. Éteint, le chemin d'extraction est celui de la ligne de base. |
| `MINERU_SERVICE_ATTRIBUTION_SATISFIED` | `0` | Porte de service liée à la licence MinerU : un service en ligne fourni à des tiers doit indiquer clairement l'usage de MinerU. Le prototype hors ligne n'est pas concerné ; toute activation hors prototype doit vérifier ce drapeau. |
| `DOCUMENT_EXCEL_ENABLED` | `1` | Extraction des classeurs `.xlsx`. |
| `DOCUMENT_PPTX_PREVIEW_ENABLED` | `1` | Visualiseur de présentation `.pptx` (aperçu borné par diapositive). Fermé ⇒ `preview_kind=unsupported` : état explicite + téléchargement. Voir [../03-backend/apercu-presentation.md](../03-backend/apercu-presentation.md). |
| `DOCUMENT_DOCX_PREVIEW_ENABLED` | `1` | Visualiseur documentaire `.docx` (aperçu borné de blocs). Fermé ⇒ `preview_kind=unsupported` : état explicite + téléchargement, jamais de zone blanche. Voir [../03-backend/apercu-documentaire.md](../03-backend/apercu-documentaire.md). |
| `DOCUMENT_PPTX_ENABLED` | `1` | Extraction des présentations `.pptx` (force l'async). À `0`, retombe sur le refus historique `unsupported_office_format`. |
| `DOCUMENT_PPTX_INCLUDE_NOTES` | `0` | Indexe les notes du présentateur, en chunks **séparés** (`kind="pptx_notes"`). Off par défaut : une note est un commentaire de l'orateur, pas le contenu publié de la diapositive. |
| `STRUCTURED_VISUAL_SCHEMA_VERSION` | `1` | Version de schéma demandée à `structured-extract` (`1` \| `2`, contrat AI Gateway 22A.1). `2` = blocs `evidence` / `qualification` / `consistency` exigés et persistés. Choix explicite par environnement : `2` seulement si `GET /health/structured-extract` du Gateway visé liste `"2"` (production Gateway V1 : non). Ne rend jamais une valeur probante. Retour arrière : `1`. |
| `DOCUMENT_OFFICE_CHART_DATA_ENABLED` | `0` | Graphiques **natifs** DOCX / PPTX : indexe les catégories et valeurs enregistrées dans le fichier (cache `c:chartSpace`) en tableau « Graphique natif (type, données du fichier) : titre » catégories × séries, à sa place dans le flux (*activable par flag*). Sans appel Gateway ni estimation. Éteint : PPTX = texte alternatif seul, DOCX = rien. À poser dans backend, worker, worker_reindex, worker_visual ; documents existants changés seulement à la réindexation. |
| `DOCUMENT_PPTX_INCLUDE_ALT_TEXT` | `1` | Indexe le texte alternatif des images, marqué `[Image : …]`. Les valeurs automatiques (`image.png`, `Picture 3`) sont écartées : ce n'est pas une description. |
| `RAG_STRONG_SIGNALS_ENABLED` | `0` | *Activable par flag, **inactif par défaut**.* Détection déterministe des signaux forts d'une question (dates, valeurs numériques, opérateurs temporels) — un lot antérieur. Ouvert, ajoute une requête de recherche portant la date en forme ISO canonique et trie les candidats sur la date pour « la plus récente » / « la plus ancienne ». **Strictement conditionnel** : sans signal détecté, le pipeline est inchangé. Gain mesuré sur corpus daté quasi jumeau : Recall@5 0,75 → 0,89, temporel 0,57 → 0,86, MRR 0,51 → 0,75 — sous les seuils GO exigés (0,90 et 1,00), d'où le défaut à `0`. Voir [ingestion & extraction](../03-backend/ingestion-extraction.md). |
| `RAG_TEMPORAL_CANDIDATES_ENABLED` | `0` | *Activable par flag, **inactif par défaut**.* Leg documentaire borné sur les métadonnées de date . Ouvert (et **seulement si `RAG_STRONG_SIGNALS_ENABLED=1`**), une question portant un opérateur temporel déclenche une requête SQL bornée sur `Chunk.meta.email_sent_at`, filtrée par RagSpace **avant** tri, projetée vers le chunk d'en-tête du document retenu. Portée **globale** (« la note la plus récente ») ou **thématique** (« … sur le Brent »), cette dernière restreinte aux documents déjà jugés pertinents — la date ne rend jamais pertinent un document hors sujet. **Strictement conditionnel** : sans opérateur temporel, aucune requête supplémentaire n'est émise. Gain mesuré : temporel Recall@5 0,86 → **1,00**. Voir [ingestion & extraction](../03-backend/ingestion-extraction.md). |
| `RAG_TEMPORAL_CANDIDATE_LIMIT` | `100` | Borne **absolue** du leg temporel : nombre maximum de documents rapportés par la requête de métadonnées (`LIMIT` SQL explicite). Validée à l'usage — une valeur absente, non entière ou `< 1` retombe sur le défaut, une valeur trop grande est plafonnée à `1000`. Il n'existe **aucune** valeur signifiant « illimité ». |
| `RAG_SPACE_MIN_SCORE_ENABLED` | `0` | *Activable par flag, **inactif par défaut**.* Autorise `RagSpace.min_score` à primer sur le défaut global `RAG_MIN_SCORE` . **Ne baisse aucun seuil** : le défaut global est inchangé, et un espace sans valeur explicite se comporte exactement comme avant. Raison d'être : `RAG_MIN_SCORE` est un seuil **absolu** sur une similarité cosinus, dont l'échelle dépend du modèle d'embedding **et** de l'appariement linguistique. Mesuré sur un corpus bilingue (questions FR, documents EN) : documents pertinents 0,727–0,739, questions hors sujet 0,662–0,687, défaut global 0,76 — **au-dessus des deux populations**, donc coupant des documents classés premiers au vectoriel. Rollback immédiat en repassant à `0`. |
| `DOCUMENT_EML_ENABLED` | `0` | *Activable par flag.* Courriels MIME `.eml` par un extracteur dédié (bibliothèque standard `email`). Fermé par défaut ; à `0`, un `.eml` reconnu est refusé (`eml_ingestion_disabled`) au lieu d'être indexé comme texte brut. Plafond `DOCUMENT_EML_MAX_FILE_SIZE_MB` (25). Voir [ingestion des courriels](../03-backend/ingestion-email.md). |
| `DOCUMENT_EMAIL_GLOBAL_ENABLED` | `0` | *Activable par flag.* **Disponibilité générale** des courriels (`.eml`) : tout RagSpace les admet, y compris les espaces créés ensuite. N'élargit **aucun** droit (droit d'écriture sur l'espace, quotas, validation et portes de format toujours exigés) ; jamais déduit d'une liste vide ni d'un joker. À `0` : seule `DOCUMENT_EMAIL_RAGSPACE_IDS` s'applique. Désactivation globale = repasser à `0`. |
| `DOCUMENT_EMAIL_RAGSPACE_IDS` | *(vide)* | *Porte d'ESPACE des courriels* (`.eml`). UUID complets ; **vide = aucun espace** ; une entrée invalide invalide toute la liste. Le dépôt d'un courriel hors liste est refusé (`email_ingestion_not_enabled_for_space`). S'ajoute aux portes de format : aucune combinaison de drapeaux n'ouvre les courriels à tous les espaces. La réindexation d'un courriel déjà ingéré n'est pas refusée. |
| `DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED` | `0` | *Activable par flag.* Contenu des pièces jointes (PDF, DOCX, PPTX, XLSX, CSV, texte, HTML, images en OCR, courriels imbriqués) ingéré par les extracteurs existants, **dans les espaces de `DOCUMENT_EMAIL_RAGSPACE_IDS` seulement**. À `0` : inventaire seul (`NOT_INGESTED`). Bornes `DOCUMENT_EMAIL_MAX_ATTACHMENTS` (20), `DOCUMENT_EMAIL_MAX_ATTACHMENT_SIZE_MB` (20), `DOCUMENT_EMAIL_MAX_TOTAL_DECODED_MB` (50, partagé avec les imbriqués), `DOCUMENT_EMAIL_MAX_NESTING_DEPTH` (2). |
| `DOCUMENT_XML_ENABLED` | `0` | *Activable par flag.* Ingestion XML générique `.xml` — parseur defusedxml (DTD/entités/références externes refusées), unités structurées par records répétés, citations sans page (`meta.locator`). Fermé par défaut (lot neuf). À `0`, un `.xml` reconnu est refusé par `xml_ingestion_disabled` — il n'est **plus** indexé comme texte brut. Limites structurelles `DOCUMENT_XML_MAX_DEPTH` (64) / `DOCUMENT_XML_MAX_NODES` (200000). |
| `DOCUMENT_DXF_ENABLED` | `0` | *Activable par flag.* Ingestion documentaire DXF `.dxf` — ezdxf en lecture stricte, sans rendu : textes, blocs et attributs, cotes, calques, layouts, XREF déclarées (jamais ouvertes) ; unités sémantiques citées sans page. Toujours asynchrone. Fermé par défaut. À `0`, un `.dxf` reconnu est refusé par `dxf_ingestion_disabled`. Limite `DOCUMENT_DXF_MAX_ENTITIES` (250000). |
| `DOCUMENT_IFC_ENABLED` | `0` | *Activable par flag.* Ingestion sémantique IFC `.ifc` — IfcOpenShell sans géométrie : hiérarchie spatiale, éléments riches, propriétés, quantités, matériaux, systèmes, classifications ; citations `Projet / Bâtiment / Niveau / Espace / Classe / Nom · GlobalId`. Toujours asynchrone, routé vers la file dédiée `ingest_ifc` : **n'activer que si un worker `--queues=ingest_ifc` tourne**. Fermé par défaut. À `0`, un `.ifc` reconnu est refusé par `ifc_ingestion_disabled`. Limite `DOCUMENT_IFC_MAX_ENTITIES` (250000). |
| `DOCUMENT_DWG_ENABLED` | `0` | *Activable par flag.* Ingestion documentaire DWG `.dwg` — RagATM ne parse pas le DWG : GNU LibreDWG 0.14 (`dwg2dxf`, sous-processus local, sans réseau) le convertit en DXF ASCII **temporaire**, lu par le DxfExtractor déjà qualifié ; la source durable, la citation et la réindexation restent le `.dwg` déposé. Toujours asynchrone, routé vers la file dédiée `ingest_dwg` : **n'activer que si un worker `--queues=ingest_dwg` portant LibreDWG 0.14 tourne**, et seulement après la revue de conformité GPLv3+ et la qualification d'un DWG de consultation réel. Fermé par défaut. À `0`, un `.dwg` reconnu est refusé par `dwg_ingestion_disabled`. |
| `DOCUMENT_AUDIO_INGEST_ENABLED` | `0` | *Activable par flag*. Dépôt **audio** (`.mp3 .m4a .wav .flac .ogg .webm`) transcrit par l'ASR d'AI Gateway (`asr-whisper-turbo`) ; seule la transcription intégrale horodatée est indexée (classe de preuve `asr_transcript`, jamais probante). S'ajoute à `DOCUMENT_AUDIO_RAGSPACE_IDS` (UUID complets, vide = aucun espace) ou à `DOCUMENT_AUDIO_GLOBAL_ENABLED`. **N'activer que si `worker_audio` (`--queues=ingest_audio`, ffmpeg) tourne.** À `0` : dépôt refusé (`audio_ingestion_disabled`), chaînes en cours annulées explicitement, audios déjà indexés conservés et réindexables. Voir [ingestion-audio.md](../03-backend/ingestion-audio.md). |
| `DOCUMENT_AUDIO_GLOBAL_ENABLED` | `0` | *Activable par flag*. **Disponibilité générale** du dépôt audio : sous `DOCUMENT_AUDIO_INGEST_ENABLED=1`, tout RagSpace l'admet, y compris les espaces créés ensuite ; `DOCUMENT_AUDIO_RAGSPACE_IDS` ne restreint plus. N'élargit **aucun** droit (écriture sur l'espace, quotas, plafond 600 Mo, validation). Valeur booléenne explicite seulement (absent = fermé). À `0` : retour à la liste pour les nouveaux dépôts, transcriptions en cours préservées. |
| `DOCUMENT_PDF_NATIVE_FALLBACK_ENABLED` | `1` | Fallback multi-moteur d'extraction PDF native. |
| `DOCUMENT_IMAGE_PREPROCESS_ENABLED` | `1` | Correction d'orientation EXIF avant OCR. |

## Moteur tabulaire (Excel / CSV)

| Flag | Défaut | Effet quand activé |
|---|---|---|
| `TABULAR_QUERY_ENABLED` | `0` | Active le moteur tabulaire déterministe (`/api/v1/tabular/query/`). À `0`, la route répond `unsupported`/`tabular_query_disabled` et le refus historique `excel_guard` du chat est **inchangé**. |
| `TABULAR_LLM_PLANNER_ENABLED` | `0` | Planner LLM. **Hors périmètre / non branché** : à n'activer que le moteur déterministe validé, et jamais en même temps que lui. |
| `TABULAR_NARRATION_ENABLED` | `0` | Narration LLM d'un résultat **déjà calculé**. Ne peut modifier aucune valeur (le formateur de résultat ne connaît aucun LLM). **Non branché**. |

> Rollback = remettre les flags à `0` : aucun catalogue supprimé, aucune donnée perdue
> (le moteur n'écrit que des métadonnées). Voir
> la documentation d'exploitation (hors périmètre de cette édition).
> Les bornes `TABULAR_MAX_*` ne sont pas des flags : voir
> [variables-environnement.md](variables-environnement.md).

## Invitations & membres

| Flag | Défaut | Effet quand activé |
|---|---|---|
| `WORKSPACE_INVITATIONS_V2_ENABLED` | `1` | Domaine dédié `WorkspaceInvitation` (lien one-shot, acceptation, token haché). Neutralise l'ancien `POST /members/` (409 `use_invitations_endpoint`). Rollback = `0` (réactive l'ancien flux **sans** réexposer `reset_link`). Voir [../03-backend/invitations.md](../03-backend/invitations.md). |
| `INVITE_RETURN_RESET_LINK` | `0` (forcé `False` en prod) | **Legacy / rollback uniquement.** Expose le `reset_link`/`email_error` dans la réponse de l'ancien `POST /members/`. Sans effet sous le flux v2. Fuite de secret si activé. |

## Périmètres owner & ACL RagSpace

*Statut : planifié — schéma & service en place (Lot 1 « fondations »), non branché aux endpoints. Tous OFF par défaut : tant qu'ils le restent, l'autorisation est identique à l'historique workspace-wide.* Voir [../03-backend/owner-scope.md](../03-backend/owner-scope.md).

| Flag | Défaut | Effet quand activé |
|---|---|---|
| `OWNER_SCOPE_ENABLED` | `0` | Applique l'isolation par **périmètre owner** : un owner (et les utilisateurs qu'il a rattachés) ne voit/ouvre que les RagSpaces de son périmètre (`owner_scope`), même à l'intérieur d'un workspace partagé. |
| `OWNER_SCOPE_AUDIT_ONLY` | `0` | Mode observation : calcule la décision cible et journalise (`owner_scope.audit` : `would_allow`/`would_deny`/`owner_scope_mismatch`/`owner_scope_missing`) **sans jamais bloquer**. |
| `RAGSPACE_ACL_ENABLED` | `0` | Exige un `RagSpaceAccess` actif (rôle espace `manager`/`editor`/`viewer`) pour l'accès d'un non-owner, en plus du périmètre. |
| `RAGSPACE_ACL_AUDIT_ONLY` | `0` | Idem `RAGSPACE_ACL_ENABLED` en mode observation, sans blocage. |

## Audit / minimisation

| Flag | Défaut | Effet quand activé |
|---|---|---|
| `RAG_QUERY_LOG_STORE_QUESTION` | `1` | Stocke le texte de la question (`0` = hash + longueur). |
| `RAG_QUERY_LOG_STORE_ANSWER` | `1` | Stocke le texte de la réponse (`0` = hash + longueur). |

## Quotas documentaires par workspace

| Flag | Défaut | Effet quand activé |
|---|---|---|
| `WORKSPACE_QUOTAS_ENFORCED` | `1` (**on**) | Bloque les uploads dépassant le quota (409). À `0`, quotas **calculés/affichés** mais non bloquants (rollout / rollback). Voir [quotas-workspace.md](quotas-workspace.md). |

## Build / infra / tests

| Flag | Défaut | Effet quand activé |
|---|---|---|
| `WITH_OCR` (build arg) | `0` | Image Docker avec Tesseract + extra `[ocr]`. |
| `CELERY_TASK_ALWAYS_EAGER` | off | Exécution des tâches Celery inline (tests / sans broker). |
| `LOAD_DOTENV` | `0` | Charge `.env` hors dev. |

> Les providers de rerank/rewrite par défaut (`stub`/`local`) sont des **escape hatch tests**,
> pas des choix de production. Voir [../06-ai/reranking.md](../06-ai/reranking.md).

---

## Références d'implémentation

- `backend/config/settings/base.py`, `backend/config/settings/prod.py`
- `backend/apps/rag_core/services/query/rewrite.py` (flags de réécriture)
- `backend/apps/actions/registry.py` (invariant mutations non déclenchables par phrase)
- `backend/apps/llm_router/checks.py` (checks liés aux flags Gateway)
