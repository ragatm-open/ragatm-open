# Limites connues

Limites **vérifiées dans le code** au commit analysé. Factuel, non alarmiste : chaque point
décrit un écart réel entre le comportement attendu et l'implémentation, avec sa localisation.
Public : ops, développeurs, décideurs techniques. Voir aussi
[../09-contributing/roadmap.md](../09-contributing/roadmap.md) (dette technique priorisée).

---

## Infrastructure / stockage

- **Stockage documentaire lié à un hôte unique.** Les documents sont écrits sur le volume
  local `media` (`MEDIA_ROOT`, Django `FileField`, `upload_to=rag_docs/…`), partagé entre
  `backend` et les workers. C'est une **décision assumée** — le stockage objet est écarté, voir
  [../01-overview/decisions/ADR-001-stockage-documentaire.md](../01-overview/decisions/ADR-001-stockage-documentaire.md).
  Limite réelle : un scale-out du backend sur plusieurs hôtes imposerait un partage réseau (NFS)
  ou une bascule S3 ; la reprise sur un autre hôte passe par la restauration de sauvegarde. La
  capacité du disque hôte est un point de surveillance explicite.
  → `backend/apps/rag_core/models/document.py`, `backend/config/settings/base.py`.
- **Sauvegarde/restauration : implémentées, non déployées.** Depuis le 2026-07-16,
  `scripts/backup/` fournit un dispositif réel (PostgreSQL + médias + configuration chiffrée,
  manifeste, checksums, restauration contrôlée, rétention GFS) — les anciens stubs vides
  `scripts/prod/backup.sh`/`restore.sh` sont remplacés.
  Durci par le Lot 3 : `tar` tolère les écritures concurrentes sans invalider toute la
  sauvegarde, préflight d'espace disque, purge des `.incomplete-*`, `pg_dumpall
  --globals-only`, alerte `OnFailure=`, code de sortie **3** pour l'état *dégradé*
  (copie secondaire absente, auparavant totalement muet), et `verify-backup.sh` distingue
  « non vérifiable » de « corrompu ». Un **exercice de restauration réel** a été mené le
  2026-07-22 sur 1,9 Go de données : RTO mesuré ≈ 3 min 40 s, comptages identiques,
  embeddings restaurés (non recalculés).
  Limites restantes : **timer systemd non installé** et **jamais exécuté sur les hôtes**
  staging/production ; **aucune copie secondaire** (`RAGATM_BACKUP_REMOTE_TARGET` non
  configuré) — donc scénario « perte du serveur » NON couvert ; **pas de cohérence
  transactionnelle base ↔ fichiers** (dump et archive médias séquentiels, sans verrou ni
  snapshot : un document ingéré entre les deux peut exister en base sans son fichier) ;
  **pas de PITR** ; `/etc/ragatm/backup.env`, `/etc/nginx` réel et `/etc/letsencrypt`
  **hors périmètre de sauvegarde** ; pas de test de restauration automatique périodique ;
  restauration vers une version majeure PostgreSQL différente non testée.
  la documentation d'exploitation (hors périmètre de cette édition).
- **`scripts/prod/rotate_logs.sh` est un stub vide** (0 octet), hors périmètre du lot
  sauvegarde.
- ~~**Pas de conteneur Celery `beat`.**~~ **RÉSOLU CÔTÉ CODE** (Lot 4) : les services
  `celery_beat` et `worker_maintenance` sont déclarés dans les **cinq** stacks
  officielles, le schedule est centralisé dans `settings.CELERY_BEAT_SCHEDULE`, les
  tâches sensibles s'exécutent sous verrou distribué, et un heartbeat rend l'ordonnanceur
  observable (`manage.py ops_health`). La purge de rétention, qui n'était ordonnancée
  nulle part, l'est désormais. **Limite restante** : un déploiement minimal (sans les stacks officielles) n'a pas de `beat`.

## Retrieval / answerability / qualité RAG

- **Answerability déterministe (P1.8) non branchée au chemin live.** Le module
  `apps/rag_core/benchmark/answerability.py` (couverture pondérée IDF, sans LLM) est
  **implémenté et benchmarké** mais uniquement appelé par le benchmark offline. Le chemin
  runtime décide de répondre via le **seuil de score** `should_answer` (`domain/policies.py`).
  → [../06-ai/answerability.md](../06-ai/answerability.md).
- **No-answer plafonne (~0.13) via `min_score`.** Sur les cas no-answer *plausibles*,
  `RAG_MIN_SCORE` ne résout pas le refus (plateau `no_answer_accuracy≈0.13` quel que soit le
  seuil). L'answerability P1.8 monte à ~0.63 mais reste sous les cibles de promotion — non
  activée en production. → ../08-testing/benchmark-rag.md.
- **Le mode inventaire ne couvre pas les inventaires documentaires.** `is_inventory_question`
  (`services/query/heuristics.py`) exige un mot de liste **et** l'une de quatre racines :
  `startup`, `pitch`, `deck`, `pitchdeck`. Mesuré le 31/07/2026 : « liste les documents
  disponibles sur la maintenance », « quelles sont les sociétés analysées ? » et « donne-moi
  tous les rapports du corpus » **n'activent pas** le mode — pas plus que le témoin `IN01` de la
  qualification production. Ces questions suivent le chemin standard : ni surcharge de budget,
  ni diversité forcée par document, donc **aucune garantie de couverture multi-documents** là où
  l'utilisateur demande précisément une liste. Corollaire : les réglages `RAG_INVENTORY_*`
  pilotent une branche quasi inatteignable sur les corpus actuels. → dette D19.
- **`RAG_HNSW_EF_SEARCH=40` est inférieur au pool demandé (120).** La limite envoyée à pgvector
  vaut `max(vector_top_k, candidate_k)`, soit 120 en production (`RAG_TOP_K=40` × multiplicateur 3).
  Un `ef_search` plus petit que la limite borne ce que l'index HNSW peut restituer : demander 120
  candidats avec `ef_search=40` ne garantit pas d'en obtenir 120 pertinents. **Non évalué** — à
  mesurer séparément. → dette D21.
- **Reranking désactivé par défaut.** `RAG_RERANK_ENABLED=0`, provider `stub` (heuristique
  lexicale). Le cross-encoder `BAAI/bge-reranker-base` (cuda) n'est actif que derrière flag +
  provider `cross_encoder`, avec **fallback silencieux vers stub** si CUDA/dépendance absente
  (tracé dans `RagQueryLog.rerank.fallback_used`). → [../06-ai/reranking.md](../06-ai/reranking.md).
- ~~**Aucun filtre de statut dans le SQL de retrieval.**~~ **RÉSOLU** (Lot 2, levée du
  NO-GO P0-A) : l'éligibilité (`status = READY` **et** `rag_space.deleted_at IS NULL`) est
  définie une seule fois (`domain/eligibility.py`) et appliquée **en SQL, avant `LIMIT`**,
  sur tous les chemins — chat FTS et vectoriel, corpus métier, leg temporel, listings
  documentaires, moteur tabulaire. Résidu de données inerte (chunks présents mais exclus
  des requêtes) mesurable via `manage.py audit_retrieval_eligibility`. →
  [../03-backend/eligibilite-retrieval.md](../03-backend/eligibilite-retrieval.md).
- **~17 réglages `RAG_*` lus via `getattr()` mais jamais définis** dans les settings
  (`RAG_RERANK_POOL_K`, `RAG_INVENTORY_*`, `RAG_NEWS_*`, `RAG_EXPLAIN_RERANK_*`,
  `RAG_LOWCONF_*`, `RAG_ENABLE_NEWS_HEURISTIC`…). Les poser dans `.env` est un **no-op
  silencieux** ; `news_mode` est du code de fait inerte. Le code lui-même le signale
  (commentaire base.py). Les budgets analytiques (`RAG_EXPLAIN_TOP_K`…) et le **budget de
  prompt** (`RAG_MAX_PROMPT_CHARS`, `RAG_SNIPPET_MAX_CHARS`,
  `RAG_SNIPPET_MAX_CHARS_TABLE`) en ont été sortis : déclarés, testés, réglables. →
  [variables-environnement.md](variables-environnement.md).

## Embeddings

- **Dimension figée à 768.** `RAG_EMBEDDING_DIM=768` doit matcher la colonne pgvector
  (`VectorField(dimensions=768)`, migration 0001). Un changement impose une migration + un
  réembedding complet ; sinon le system check `rag_core.E001` échoue au démarrage.
- **Stub d'embeddings non sémantique.** `EMBEDDINGS_PROVIDER=stub` (pseudo-vecteurs
  déterministes depuis `sha256`) est le mode **tests/CI bloquante**. La qualité vectorielle
  réelle n'est donc **pas exercée en CI bloquante** ; seuls les benchmarks sémantiques
  (nomic, self-hosted, non bloquants) la couvrent. La prod refuse le stub au démarrage. →
  [../06-ai/embeddings.md](../06-ai/embeddings.md).

- **`FREEZE V4 / MULTI-STORE IDENTITY` — le sceau de gel ne couvre qu'un seul magasin.**
  Une requête interroge **deux** magasins et produit **deux** embeddings de la question : le
  RagSpace privé (`ai_gateway` / `fr-en-retrieval`) et le corpus métier (`ai_gateway` /
  `legacy-768`). Seul le premier entre dans le `config_hash` du gel
  v3 : `EMBEDDING_IDENTITY_KEYS` est un tuple **fermé** dont `_validate_embedding_identity`
  rejette toute clef inconnue, et l'identité d'un corpus vit dans un **autre espace de noms**
  — `EmbeddingIdentity.fingerprint`, un sha256 — que `pipeline_family`. **Changer le modèle
  d'embedding d'un corpus modifie donc les candidats sans modifier le `config_hash`.**
  Décision du 2026-09-04 : ne **pas** faire évoluer le contrat avant la campagne holdout —
  une rupture de schéma juste avant un usage *one-shot* coûterait plus qu'elle ne protège,
  d'autant que le corpus n'a contribué ni au top1 ni au top5 sur les 74 cas de calibration.
  La campagne compense par un contrôle **P4** à six propriétés vérifié immédiatement avant
  le run (`backend/benchmarks/calibration/holdout-v4-prereg/validation-plan.md`) ; un seul
  écart interrompt la campagne. **Dette à traiter après la campagne holdout**, pas avant.
  → `backend/apps/rag_core/benchmark/holdout_config.py`,
  `backend/apps/corpus/domain/embedding_identity.py`.
- **Le sceau d'embedding décrit le défaut global, pas l'espace mesuré.** `model` provient de
  `DEFAULT_RAGSPACE_EMBEDDING_MODEL` ; un `RagSpace` le surcharge. En staging, le réglage vaut
  `legacy-768` et l'espace tourne sur `fr-en-retrieval`. Deux espaces encodés par deux modèles
  différents portent le **même** `config_hash`. Le garde-fou existe —
  `observed_pipeline_families`, qui entre dans le hash — mais il n'est **pas armé par défaut**
  et sa portée est le **déploiement entier**, pas l'espace : un sceau armé est donc
  sur-sensible. Voir ../08-testing/holdout-config-gel.md.

## Confidentialité / egress externe

- **Throttle anonyme indexé sur l'IP du conteneur Next (dette séparée, non corrigée).** Toutes
  les requêtes API passent par le proxy Next (`frontend/src/app/api/[...path]/route.ts`), qui ne
  relaie pas `X-Forwarded-For`, et `NUM_PROXIES` n'est pas défini : DRF identifie chaque
  requête anonyme par `REMOTE_ADDR` = IP du conteneur frontend. Le seau `anon` (30/min) est donc
  **partagé par tous les clients anonymes** (login sans cookie valide, réinitialisation de mot
  de passe…). Constaté le 08/10/2026 (EVAL-01) ; la correction (relais de l'en-tête, proxies de
  confiance, risque d'usurpation de `X-Forwarded-For`) relève d'un lot dédié.
  → `frontend/src/app/api/[...path]/route.ts`, `backend/config/settings/prod.py`.

- **Politique d'egress par RagSpace uniquement.** Depuis le Lot 1 (levée du NO-GO P0-C),
  tout appel LLM sortant exige un `LlmExecutionContext` porteur d'une politique résolue,
  et l'absence de contexte **interdit** l'egress (fail-closed). La granularité s'arrête
  toutefois au **RagSpace** : il n'existe pas de politique par *workspace*, et l'overlay
  `CoreConfigOverride` reste inerte au runtime (`CORE_CONFIG_RUNTIME_ENABLED=0`).
  → [../06-ai/politique-egress.md](../06-ai/politique-egress.md).
- **`Document.confidentiality` est un champ INERTE.** Il existe (défaut `internal`) mais
  **aucun code ne le lit** : il ne protège aujourd'hui aucun document et ne doit pas être
  présenté comme un contrôle. La migration `0024` le documente explicitement.
  → `backend/apps/rag_core/models/document.py`.
- **`allow_external_fallback` n'est pas exposé dans l'interface.** Ce booléen — qui
  décide si le contenu d'un espace peut quitter l'infrastructure privée — se règle
  uniquement par `PATCH /api/v1/rag-spaces/<id>/`. Son basculement est en revanche tracé
  (`EgressPolicyChangeLog`).
- **Aucune notion de région / résidence des données.** Aucune donnée ne
  quitte l'installation, mais rien ne modélise le pays de traitement ni le site d'hébergement.

## Génération / streaming

- **Pas de vrai streaming quand la Gateway est active.** Le streaming token-par-token n'existe
  que sur le chemin direct (Gateway off). Avec `AI_GATEWAY_ENABLED=1`, le chat émet un unique
  bloc (pseudo-stream) car un vrai stream bypasserait la Gateway. → `apps/chat/services/chat_service.py`.
- **AI Gateway in-repo (v0.1.0) incomplet.** Le service `services/ai_gateway/` n'expose que
  `/health` + `/v1/generate` et **ignore** les champs `provider`/`system`/`think`/`options`
  envoyés par le client de génération, et **n'implémente pas `/v1/embeddings`** attendu par le
  backend. En env officiel c'est la Gateway v2 (hors repo) qui répond.
  → [../06-ai/ai-gateway.md](../06-ai/ai-gateway.md).
- **Le contrat `/v1/embeddings` est fermé — `provider` y est interdit.** Le schéma n'accepte que
  `texts`/`model`/`task`/`keep_alive` ; les builds récents rejettent tout extra en 400
  `extra_forbidden`. Un build plus ancien les ignorait en silence : une requête acceptée ne
  prouve donc pas qu'un champ est au contrat. → [../06-ai/embeddings.md](../06-ai/embeddings.md) §3.2.
- **Modèles de chat non inscrits au registre de génération : non proposés.** Le sélecteur
  n'admet que les modèles que `/health/runtime` déclare `completion` (servis par
  `/v1/generate`). `deepseek-r1:32b`, `glm-4.7-flash`, `qwen3-vl:8b`, `semx-internvl3.5:8b`
  répondent peut-être mais ne sont pas inscrits : exclus par prudence (fail-closed). Les
  espaces qui les portaient déjà sont conservés. → [../06-ai/providers-modeles.md](../06-ai/providers-modeles.md) §5.3.
- **Le résumé de mémoire de conversation n'utilise pas le modèle de l'espace.**
  `maybe_update_thread_summary(thread=thread)` ne transmet aucun modèle : le résumé injecté
  dans le prompt est produit par `RAG_LLM_MODEL` (`llama3.1:8b` en staging et production),
  quel que soit le modèle de génération choisi. → `apps/chat/services/chat_threads.py`.
- **Conflit de sources : `qwen3:30b-instruct` peut trancher sans justification.** Sur deux
  sources contradictoires (42 vs 48 MW), le banc du 02/11/2026 l'a vu choisir une valeur en
  invoquant une préséance absente des sources, au lieu de signaler l'écart. Non corrigé.

## Fil de discussion / persistance

- **Le chat standard n'a pas de contrainte d'unicité en base.** `_ensure_thread_and_persist_messages`
  (`api/v1/chat/views.py`) déduplique par *check-then-act* sur (`thread`, `request_id`, `role`),
  sans `UniqueConstraint` ni verrou : deux requêtes concurrentes de même `request_id` peuvent
  créer deux messages. Les **résumés de document** sont, eux, protégés en base
  (`uniq_chat_message_summary_request`, migration `chat/0007`). Étendre la contrainte à tous les
  messages exigerait un préflight de déduplication sur les données existantes — **dette séparée**,
  volontairement hors du lot résumés. → [../03-backend/chat-generation.md](../03-backend/chat-generation.md).
- **Les échecs du résumé de conversation sont absorbés en silence.**
  `maybe_update_thread_summary` (`chat_threads.py`) est encadrée par `except Exception: return`,
  son appelant par `except Exception: pass`, et le module **ne contient aucun logger**. Aucune
  métrique n'existe (déclenchement, taille d'entrée et de sortie, latence, timeout, échec).
  Un timeout de résumé est donc indiscernable d'un fil trop court ou d'un modèle indisponible —
  et la perte est **définitive** : le checkpoint n'avançant pas, les messages concernés ne seront
  jamais résumés. Le résumé étant de surcroît **synchrone dans la requête HTTP**, sa latence
  s'ajoute à la réponse de l'utilisateur sans être mesurée nulle part. Bloquant pour toute
  qualification de `CHAT_MEMORY_SUMMARY_MAX_INPUT_CHARS` ou d'un timeout dédié. → dette D20.
- **`GET chat/threads/<id>/messages/` plafonne à 500 messages, sans pagination.** Le tri étant
  `created_at, id` **ascendant**, c'est la **fin** d'un fil de plus de 500 messages qui devient
  invisible — y compris un résumé récent. Aucune pagination n'est exposée. → dette séparée.
- **`pushClarification` reste purement local au frontend.** Les demandes de clarification
  (`chat/page.tsx`) sont injectées dans l'état React sans persistance : elles disparaissent au
  rechargement du fil, exactement comme le faisaient les résumés avant `chat/0007`. Même famille
  de défaut, **non traitée** dans le lot résumés (aucun test ni doc ne la couvre) → dette séparée.

## Apps / code mort

- **Un seul moteur OCR (`tesseract`), sans moteur de secours.** Un document dont l'OCR ne
  produit aucun texte (`ocr_no_text`) est classé `replace_source_recommended` : aucune
  reprise automatique ne peut aboutir, l'interface ne propose donc rien plutôt qu'un bouton
  qui reproduirait l'échec. La seule issue est un meilleur fichier source — et **le
  remplacement du fichier source n'existe pas dans l'interface** : il faut supprimer puis
  réimporter. → dette séparée (second moteur OCR, remplacement de source).
- **Un réimport après échec créait un doublon** (*corrigé — `fix(ingest): reuse failed
  documents for duplicate content`*). `find_existing_by_sha` ignorait les documents `FAILED` :
  chaque nouvelle tentative d'import laissait une ligne fantôme de même contenu, et dès que
  l'une de ces lignes redevenait active, les autres ne pouvaient plus jamais quitter `FAILED`
  (collision sur `uniq_rag_document_space_sha256_not_failed`, constaté sur 28 PNG identiques
  en staging le 06/10/2026). Désormais la **première occurrence FAILED est réutilisée** comme
  cible de reprise (même `document_id`, réponse `queued:true, deduped:true,
  detail:"failed_document_retried"`), et une collision à la transition `FAILED → INGESTING`
  est réglée comme échec métier **`duplicate_content`** (non retryable, `meta.duplicate_of`).
  Les lignes FAILED identiques **historiques** ne sont pas normalisées par ce correctif.
- **Le code d'erreur d'une ancienne tentative pilotait la reprise** (*corrigé — `fix(ingest):
  scope error metadata to current attempt`*). `meta.extraction.error_code` n'était jamais
  remis à zéro au début d'une tentative, et seuls les échecs passés par la couche d'indexation
  l'écrivaient : après un échec réglé ailleurs (collision DB, erreur de tâche), le document
  portait le **message** de la tentative N+1 et le **code** de la tentative N — lu en priorité
  par la stratégie de reprise et affiché par l'API (constaté en staging le 06/10/2026 :
  `pipeline_task_inconsistent` périmé devant une collision `duplicate_content`). Désormais le
  passage à `INGESTING` retire les marqueurs d'erreur active dans le même `UPDATE`, et un code
  connu porté par `error_message` prime sur un code `meta` différent. Les lignes historiques
  ne sont pas réécrites : elles se corrigent à leur prochaine tentative ; une ligne historique
  dont le message courant est de la prose reste indécidable en lecture jusque-là.
- **Backlog Celery `reindex` à résorber au premier déploiement du worker.** La queue a
  accumulé sans consommateur du 17/07 au 01/08/2026 (1 218 messages en production,
  2 323 en staging), essentiellement des `reap_stale_reindex_items_task` périodiques.
  Ces tâches portent un `expires` égal à leur intervalle : elles sont **écartées à la
  consommation**, pas exécutées. Le démarrage du worker résorbe donc le backlog sans
  effet de bord, mais la décision (purge ciblée `-Q reindex` ou drain) doit être prise
  explicitement avant la mise en service. Cf.
  [../03-backend/reindexation-jobs.md](../03-backend/reindexation-jobs.md).
- **Aucune annulation d'un job de réindexation en cours.** Le statut `cancelled` existe dans
  le modèle mais aucun endpoint ne le déclenche : l'interface n'affiche donc pas de bouton
  d'annulation plutôt qu'un bouton sans effet.
- **Apps `documents` et `billing` vides.** Enregistrées dans `INSTALLED_APPS` mais sans
  modèles/vues/urls ; la couche HTTP documents vit dans `api/v1/documents/`. Placeholders.
- **`apps/rag_core/services/feedback.py` est un fichier vide.** Vestige. Le feedback vit
  désormais dans l'app `apps/feedback` (`AnswerFeedback` + `FeedbackDiagnosticSnapshot`),
  exposé par `api/v1/chat/views.py::ChatFeedbackView`. →
  [../03-backend/feedback-incidents.md](../03-backend/feedback-incidents.md).
- **La capture diagnostique observe le pool POST-FUSION.** Les candidats écartés en amont
  par les seuils (dans le `CandidateRetriever`) ne sont pas visibles : la raison
  `below_threshold` n'est donc jamais émise. Un chunk totalement absent de la trace
  signifie « écarté avant la fusion ».
- **La langue RÉSOLUE n'est pas persistée** par le pipeline : le snapshot n'expose que la
  langue détectée, recalculée et marquée `source: "recomputed"`.
- **« Admin plateforme » et superuser sont confondus** dans l'instance actuelle
  (`is_ragatm_owner` ⇒ `is_superuser`) : la séparation des rôles d'export est implémentée
  et testée, mais ne devient effective qu'avec un rôle plateforme distinct.

## Tests / CI

- **Suite backend unique** (`tests/tests_root`) : la lancer via `make test-backend-all`.
  Les deux arbres historiques (`tests_root` + l'ancien `tests_legacy`) ont été fusionnés. →
  [../08-testing/strategie.md](../08-testing/strategie.md).
- **Tests `ai_gateway` hors CI.** Les 14 tests de `services/ai_gateway/tests` ne sont couverts
  par aucun workflow.
- **Pas de garde lint/type Python en CI.** `ci.yml` n'exécute ni ruff, ni black, ni
  `manage.py check` ; **aucun mypy** dans le dépôt (ni config, ni dépendance). ruff/black sont
  configurés mais manuels. → [commandes.md](commandes.md).
- **Qualité vectorielle non testée en CI bloquante** (stub non sémantique, cf. Embeddings).
- Autres manques documentés : pas de couverture (coverage.py), pas d'e2e/Playwright, pas de
  tests de montée de migration, pas de tests property-based sur le chunker, pas de tests de charge.

---

## Vision V2 orchestrée (CV-3E1) — *désactivée par défaut*

- **Balayeur en filet, pas en temps réel** (CV-3E4A) : un envoi perdu est ré-émis au plus tard
  après sa période de grâce (10 min, 5 min pour un report échu) plus une passe (300 s). La
  fenêtre READY → préparation est **fermée** (CV-3E4C) : la préparation automatique est écrite
  dans la transaction READY, seul l'envoi est post-commit et il est rattrapable.
- **Fermeture par environnement seule** : effective au redémarrage uniquement ; toujours fermer
  d'abord la politique en base (`visual_v2_scope --disable`).
- **POST échappé** : au plus un POST (réservé juste avant la fermeture) peut partir ; s'il réussit,
  son résultat interne est conservé.
- **Contexte textuel vide** : les crops partent sans légende ni texte voisin (comme les visuels
  PDF du rail V1) ; `context_sha256` porte sur ce contexte canonique vide.
- **Régions tabulaires non exclues** : la découverte V2 ne reçoit pas les régions de tableaux
  natifs que la passe primaire masque au rail V1.
- **PDF sans texte (`handoff`)** : la reprise V1 *primaire* reste active dans le périmètre V2 ;
  un tel document est analysé par V1 (avant READY) puis préparé par V2 (après READY).
- **Retraitements concurrents** d'une version sans job convergent sur la même génération (clé
  idempotente, jamais un doublon).

## Extraction structurée chart/table — *désactivée par défaut*

Mesures sur le corpus pilote staging (22 PDF réels, référence manuelle de 269 pages), bornes 32
par document et 8 par page : graphiques raster indexés 49/51, tableaux raster 68/82, 12 questions
de chat historiques 12/12. Ce n'est **pas** une qualification générale.

- **Valeurs de courbes échantillonnées** : le moteur relève les séries aux dates de graduation ;
  un pic entre deux graduations n'est pas mesuré. Depuis EXTRACTION-FIDELITY-10 le fragment le
  déclare (maximum « parmi les points relevés », maximum réel non déterminable) au lieu de laisser
  lire un point comme un maximum ; le pic lui-même reste **non extrait** (aucune numérisation de
  courbe).
- **Valeurs hors échelle** : le moteur peut produire des valeurs hors de l'axe visible ; elles sont
  désormais signalées « non fiables » quand l'échelle de l'axe est lisible (axe unique).
- **Titre / identifiant** : repris depuis l'OCR du moteur quand il est imprimé au-dessus du tracé ;
  un titre placé ailleurs (sous le graphique, dans la légende) ou illisible n'est pas repris.
- **Graphiques vectoriels** : couverts par rendu de région depuis une version antérieure (*activable par flag*,
  OFF par défaut et en production ; qualifié sur PDF synthétiques seulement). Restent non couverts : SmartArt /
  EMF / SVG des fichiers Office, page de plus de 4 000 tracés, figure faite surtout de texte positionné (sans
  tracés), grille de tableau sans aplat ni courbe (écartée comme tableau). Les valeurs lues sur une figure
  rendue restent une DESCRIPTION, jamais une preuve chiffrée.
- **Capacité Vision** : un visuel non décrit (`vision_timeout`, `host_capacity_deferred`) n'est
  jamais classé, donc jamais extrait (4 tableaux).
- **Classification** depuis le `describe` en français : une description anglaise (« a table »)
  n'est pas reconnue (2 tableaux) ; un tableau de bord peut être classé tableau.
- **Pages entières** (PILOT-2, documents réels) : la première phrase décrit souvent le document
  (« un rapport de l'Insee… ») et non le tableau. Depuis PAGE-TABLE-ROUTING-1, la liste
  `Objects:` est lue (*implémenté*). Une page **sans description** (Vision en échec) reste non
  classée ; une page qui contient tableau ET graphique n'est routée que vers une tâche.
- **Groupes de lignes sans signal de grille** : des groupes marqués seulement par la typographie
  (gras, retrait) ne sont pas reconstruits. Les libellés répétés sont signalés comme ambigus,
  jamais rattachés à un groupe deviné : la question peut rester sans réponse fiable.
- **OCR ordinaire concurrent** : l'OCR de page peut perdre signes et séparateurs (« 07 62 62 »
  pour « 0,7 -6,2 6,2 »). La ligne n'est écartée du contexte que si la même page fournit la ligne
  structurée aux mêmes chiffres. Sans extraction structurée (tableau refusé ou non routé), l'OCR
  fautif reste la seule source et peut tromper la génération.
- **Extractions vides** : 3 visuels (tableau de bord, tableau lu comme image) ; consignées, sans
  fragment.
- **Dépendance** à la route `POST /v1/vision/structured-extract`, exposée par les Gateway staging et
  production depuis le build de référence. En production : refusée sauf `STRUCTURED_VISUAL_PRODUCTION_ALLOWED`,
  et alors **tableaux seuls** (plafond codé) — *activable par flag* (activé en production sur l'espace de
  qualification seul depuis le lot STRUCTVIS-PRODUCTION-CANARY ; état réel à lire dans les processus, pas ici).
  Les valeurs de graphiques PP-Chart2Table restent **non qualifiées** et ne sont jamais publiées en production ;
  publier un tableau ne certifie pas ses valeurs (`requires_visual_check`, avis expérimental conservés).
- **Preuve chiffrée** (durcie lors de la qualification, *implémenté*) : une
  sortie structurée n'est preuve que si le Gateway déclare le moteur qualifié ET la valeur certifiée
  (`evidence.certified`, `numeric_claims_supported`, `business_numeric_use_authorized`) sans anomalie de
  cohérence ; le contrat 22A.1 déclare `certified` toujours faux : AUCUNE sortie actuelle ne l'est. Conséquence : un nombre porté seulement par un tableau ou un graphique extrait
  (staging : 1 477 fragments ; production : 8) n'est plus certifié — la proposition est retirée, ou la réponse
  devient une abstention, sauf si une source native indépendante porte la même valeur. Les fragments restent
  recherchés et cités.
- **Numérisation des courbes (shadow, lot 14)** : *activable par flag, staging seulement*. Le pic
  réel peut être mesuré depuis les pixels, mais la valeur **ne sert à rien côté RAG** (ni fragment,
  ni prompt, ni API). Couverture mesurée au lot 13B : 5 graphiques sur 12 (41,7 %, sous le seuil
  de 50 % fixé) ; abstention attendue pour une légende dans le tracé, des séries superposées ou
  de couleurs voisines, un axe à droite ou logarithmique, un pic au-delà de la dernière
  graduation. Précision **non qualifiée** : aucune annotation humaine indépendante.
- **Garde-fous du shadow des courbes (lot 16)** : graphiques à **deux axes Y** non pris en charge
  (abstention sur tout le graphique, `dual_y_axis_unsupported`) ; deux étiquettes numériques alignées à
  droite suffisent à faire suspecter un axe (`possible_right_y_axis`, abstention prudente même si l'axe est
  unique) ; extremum d'une série grise ou noire posé sur un axe/une grille de même couleur rejeté
  (`neutral_series_axis_grid_ambiguity`). Mesuré uniquement sur vérité synthétique.
- **Association valeur ↔ en-tête des tableaux (lot STRUCTVIS-TABLE-SAFETY-3)** : les grilles
  incohérentes, les régions sans boîte `table` et les en-têtes hiérarchiques ambigus sont refusés
  (*implémenté*). Mais un **décalage de colonnes cohérent** (libellés d'en-tête glissés d'un cran,
  grille complète, sans doublon ni chevauchement) n'est pas détectable dans la sortie du moteur :
  sur deux réponses adversariales dérivées du Gateway de référence, 30/30 et 36/36 valeurs publiées
  sous le mauvais mois. Non reproduit par le build de référence sur la fixture mensuelle (grille exacte) ;
  constaté historiquement sous un build staging antérieur (registre p36). Seul un contrôle indépendant de
  l'image (relecture OCR des en-têtes, ou référence humaine) le détecterait ; un en-tête vide
  au-dessus d'une colonne chiffrée en est un indice partiel, **non utilisé** (défaut non reproduit,
  risque de refus de tableaux valides non mesuré). Les marqueurs `experimental` /
  `requires_visual_check` restent la seule réserve. Précision sur documents réels : **non qualifiée**.
  Depuis COLUMN-SHIFT-GUARD-1, le décalage **observable** — valeurs glissées dans la colonne des
  libellés après fusion de libellés de lignes (constat réel staging, 162 valeurs) — est refusé
  (`structured_visual_column_association_unsafe`, *implémenté*). Restent publiés : un décalage qui
  touche toutes les lignes (aucune ligne libellée de référence), une ligne isolée, un nombre collé
  au libellé (« Pompe A 18 ») et une valeur unique sous la mauvaise colonne (1 cas réel publié
  sur 6 valeurs, non détecté). Un refus structuré ne protège pas la réponse du chat contre l'OCR
  ordinaire de la même page, qui reste indexé.
- **Tableaux de PDF natifs (lot NATIVE-TABLE-GUARD-1)** : en staging réel (extraits natifs, drapeau
  `DOCUMENT_PDF_GENERIC_TABLES_ENABLED` éteint), 11–12/18 réponses justes mais 3–4 chiffres faux affichés :
  le texte d'un tableau aplati perd la relation valeur/colonne et échappait au contrôle. Avec le drapeau,
  les lignes structurées sont la preuve (ligne + colonne + unité) et le texte des grilles non structurées
  n'est plus une preuve (*implémenté*). Lot NATIVE-TABLE-RECOVERY-2 (*implémenté*, non requalifié en
  réel) : en-tête d'un tableau paginé repris d'un bandeau réglé (page même ou précédente), continuation
  sans en-tête répété chaînée par la géométrie, texte de page aux glyphes non droits conservé (moteur de
  repli) mais non probant, colonne neutre non probante dès que la ligne porte plusieurs nombres.
  Désormais, le rail ne s'applique qu'aux RagSpace listés
  (`DOCUMENT_PDF_GENERIC_TABLES_ALLOWED_RAGSPACE_IDS`, vide = aucun) : un espace non listé garde le texte
  aplati et cette limite ; un document indexé avant l'ajout de son espace n'en bénéficie qu'après
  réindexation ; un espace du rail visuel structuré est exclu, sans repli tabulaire natif.
  Lot NATIVE-TABLE-GROUNDING-3 (*implémenté*, non requalifié en réel) : grilles à colonnes réglées sans
  filets horizontaux certifiées rangée par rangée (ancrage centré), affirmation prouvée conservée seule
  quand elle est séparable de ses voisines non prouvées.
  Restent ouverts : drapeau éteint par défaut (et non isolable par espace), tableaux que le détecteur ne
  voit pas du tout (colonnes non tracées ; en-tête séparé par un seul filet horizontal ; cellules non
  centrées : leurs lignes restent cherchables mais ne prouvent aucun chiffre si la page a dû être relue par
  le moteur de repli, sinon texte natif ordinaire), affirmations liées (comparaison, total, fourchette…)
  toujours retirées entières,
  encadrés d'une colonne contenant un vrai tableau (`single_semantic_column`), grilles dont les filets sont des
  rectangles fins (double bord), interrompus à des hauteurs DIFFÉRENTES par des rangées de total fusionnées,
  aux bords coupés aux sections et aux filets horizontaux limités aux colonnes de libellé (constat A6 p.3,
  NATIVE-TABLE-RECOVERY-9 : rejet `single_semantic_column`, puis aucune grille de colonnes démontrée ; depuis
  NATIVE-TABLE-GEOMETRY-12, certifiée par le contrat de grille segmentée JUSQU'À la première rangée de total ou
  interruption multi-colonnes — les sections suivantes restent du texte non probant ; un sous-tableau des colonnes de
  libellé fermé par des filets horizontaux partiels et STRUCTURÉ par l'analyseur masque toujours la grille ; unité
  jamais déduite d'un libellé de colonne), cartouches de page
  structurés en petits tableaux neutres, texte narratif natif (une PHRASE qui porte le nombre et partage un
  mot avec la question suffit — un seuil cité dans une phrase peut « prouver » une mauvaise attribution ;
  depuis NATIVE-TABLE-SAFETY-6, une ligne d'allure tabulaire doit porter le libellé de la ligne demandée,
  mais une phrase longue qui nomme une ligne voisine reste admise),
  chiffres d'un seul caractère, proposition juste retirée entière quand elle contient d'autres chiffres non
  prouvés ET qu'elle n'est pas séparable sans risque. Lot NATIVE-TABLE-RESPONSE-HARDENING-4 (*implémenté*,
  rejoué sur sorties réelles archivées, non requalifié en génération réelle) : frontière de phrase après un
  caractère fermant ; attribution d'une ligne à une source vérifiée (forme explicite « la ligne « X » … [n] »
  seulement — les autres affirmations non chiffrées et les phrases hors périmètre du contrôle ne sont pas
  vérifiées). Abstention du modèle malgré la ligne exacte au rang 1 (R13 du pilote) : comportement de
  génération, sans correction locale. Lot NATIVE-TABLE-SAFETY-6 (*implémenté*, rejoué sur les 166 sorties
  réelles archivées, non requalifié en génération réelle) : une ligne de texte natif d'allure tabulaire ne
  prouve plus la valeur d'une autre ligne (R06 de FQ5 : « Local CTA » ≠ « Local technique CFA ») ; la
  distinction tabulaire / narratif est une heuristique de forme (mots par valeur), pas une métadonnée.
  Garde de citation réparée : plus aucun remplacement de citation, seules les citations rattachées au
  libellé sont contrôlées, négation limitée à la portée de l'attribution, « catégorie « X » » contrôlée,
  libellé tronqué toléré s'il est unique. Restent : rattachement syntaxique heuristique (une citation en fin
  de phrase longue peut être rattachée à tort, une attribution sans mot « ligne / catégorie » n'est pas
  vérifiée), une négation en tête de phrase (« Sans ambiguïté, la ligne « X » … [n] ») soustrait
  l'attribution au contrôle. Lot NATIVE-TABLE-RECOVERY-9 (*implémenté*, rejoué sur les 192 sorties réelles
  archivées, non requalifié en génération réelle) : une affirmation d'absence (ou d'exclusivité) d'un objet nommé,
  contredite par une source fiable de sa portée, n'est plus publiée (R05 de GENERATION-8). Restent : contrôle
  limité au PÉRIMÈTRE du contrôle numérique — hors périmètre le flux n'est pas tamponné et une absence fausse
  reste publiée (7 constats dans les archives 1R/TRQ, drapeau éteint) ; formes non reconnues (« sans ligne
  « X » », exclusivité implicite, objet repris par un pronom) ; une source citée qui décrit une autre page ne
  contredit pas l'affirmation (portée prise au mot). Lot CONTEXT-GROUNDING-11 (*implémenté*, non requalifié en
  génération réelle) : en mode `centered`, l'extrait d'un fragment non tabulaire montre la ligne désignée par la
  question quand le fragment la porte (constat R05 : l'extrait omettait la ligne) ; une valeur décimale d'une
  ligne aplatie n'est plus prouvée si la ligne porte d'autres décimales (colonne indéterminable). Restent : mode
  `head` (production) non ancré ; dernière source rognée au budget de prompt non réancrée ; en-têtes de colonnes
  d'une page aplatie hors de la fenêtre. Le contrôle des absences cherche désormais la présence dans le fragment
  récupéré entier de la source (cohérent avec l'extrait) ; un fragment n'est pas le document entier : une ligne
  située dans un fragment NON récupéré ne contredit rien. Précision réelle avec le drapeau : **non qualifiée**.
- **Unités affichées (lot PRODUCTION-BLOCKERS-14)** (*implémenté*, rejoué sur les 68 sorties réelles de
  FINAL-QUALIFICATION-13 : les 7 unités non prouvées retirées, 0 statut de valeur modifié) : une unité affichée
  n'est conservée que si la preuve de la valeur la porte. Restent : contrôle limité au PÉRIMÈTRE du contrôle
  numérique (hors périmètre, aucun nombre ni aucune unité n'est contrôlé) ; unités hors liste (« USD », « $ »,
  « milliards », « billion ») ni certifiées ni retirées ; complément après une unité reconnue non comparé
  (« kWh/m².an » lu « kWh/m² ») ; une colonne d'unité n'est rattachée qu'à une colonne de QUANTITÉ (« QTE »).
- **Affirmations narratives (lot PRODUCTION-BLOCKERS-14)** (*implémenté*, rejoué sur les 68 sorties réelles de
  FINAL-QUALIFICATION-13 : B001 sync et flux corrigés, 0 autre sortie touchée sur ~90 phrases négatives) : une
  non-quantification n'est retirée que si la phrase CITE le passage de la source qui porte le chiffre. Restent :
  négation sans passage cité (vraie ou fausse) jamais contrôlée ; preuve limitée au fragment récupéré (un chiffre
  d'un fragment non récupéré ne contredit rien) ; **flux hors périmètre non tamponné** — le texte retiré a été
  visible avant `done` (aucune garantie « avant émission » hors périmètre) ; **inversion de relation** (B002 :
  bonne valeur, mauvais sujet, citation exacte) **non détectée** — aucune représentation structurée des relations
  d'un texte narratif ; une citation exacte ne prouve pas la relation.
  Lot RELATION-GROUNDING-16 (*implémenté*, rejoué sur 176 décisions réelles : 2 retraits, les 2 erreurs « 16 % »
  de B002) : seule la forme « énumération respectively + entités géopolitiques du lexique » est contrôlée. Restent NON
  contrôlés : sens d'une relation (67 % inversé), nature d'un montant (375 M€ : taille de fonds présentée comme engagement),
  entités hors lexique, réponses sans citation ; écart architectural documenté (représentation affirmation-preuve
  bilingue absente du pipeline). Une valeur coïncidente d'une autre métrique dans la même source citée est retirée comme
  non étayée.
- **Mode de contexte et qualité documentaire (lot PRODUCTION-BLOCKERS-14)** (*benchmarké*, comparaison historique
  `head` / `centered` sur 24 + 24 générations réelles, corpus métier VC) : aucun des deux modes ne transmet toujours le
  passage utile — `centered` coupe un chiffre situé en fin de fragment (B001, 43 % en position 750), `head` un chiffre au-delà
  des 800 premiers caractères (B004) ; dans les deux cas le modèle peut affirmer que la source « ne précise pas » le
  chiffre. Qualité égale à ±1 ; erreurs de relation valeur-sujet dans les deux modes (2 en `head`, 1 en `centered`). Contrôle
  numérique : sur un contexte métier, un fragment tabulaire NON VÉRIFIÉ d'un autre document suffit à placer toute la
  réponse sous contrôle, qui juge alors une phrase française contre une source anglaise par recoupement lexical de LIGNE
  (PDF coupé en lignes) : valeurs JUSTES retirées (B018), jamais une valeur fausse publiée. Questions multi-passages : un
  passage non retrouvé laisse la réponse partielle (retrieval, non corrigé).
- **Nombres lus par OCR (lots OCR-NUMERIC-CAVEAT-1, NUMERIC-CLAIM-GUARD-2)** : en staging réel, les réponses
  fausses données avec assurance venaient toutes de l'OCR ordinaire de pages tabulaires ; sous 66a5282, une
  valeur fausse restait affichée avec une réserve (un nombre de contexte « confirmait » la réponse entière).
  Désormais (*implémenté*) chaque nombre affirmé est contrôlé seul et n'est conservé que si une source non
  issue d'une lecture d'image le porte pour la même ligne et la même colonne ; sinon la proposition est
  retirée, ou la réponse devient une abstention ; le flux ne montre rien avant le contrôle. Coût assumé :
  une valeur JUSTE lue seulement par OCR ou vision n'est plus donnée (rejeu des réponses réelles archivées :
  3 réponses justes sur 14 perdues). Restent ouverts : un fragment structuré faux (valeur sous la mauvaise
  colonne) compte comme preuve, la pertinence d'un texte natif ne se lit que par recoupement de mots, et la
  confusion entre documents non nommés. Précision sur documents réels : **non requalifiée** sous ce lot.
- **Récupération des tableaux raster (lot TABLE-RECOVERY-1)** : sur un corpus de tableaux raster réels (11 tableaux, 9 pages),
  le routage passe de 7 à 8 pages tabulaires routées (attributs d'objets « tableau »). Restent : une page
  dont ni la description ni les objets ne nomment de tableau (non routable sans nouvelle source
  d'information) ; des refus d'en-tête complexe confirmés justes par diagnostic (texte vertical lu en une
  cellule fusionnée, trois tableaux collés en une grille) ; d'autres refus sans grille archivée, non
  diagnostiqués. Le « 1 valeur sur 6 sous la mauvaise colonne » relevé sur un tableau publié vient très
  probablement de la RÉFÉRENCE (grille couche texte : rangées de données découpées sur d'autres filets que
  l'en-tête) ; l'association du moteur est cohérente mais non prouvée — à trancher par les coordonnées des
  mots de la couche texte du PDF original.
- **Réponse sans mise en page** : *fermé* depuis NO-LAYOUT-FAIL-CLOSED-4 — sans boîte `table`
  fiable, la publication structurée est refusée (`structured_visual_table_region_unproven`). Coût
  assumé : un vrai tableau rendu sans mise en page (2/113 réponses staging historiques sous
  un build staging antérieur, 0/8 sous le build de référence) n'est plus publié en structuré ; son texte reste disponible par la
  description visuelle ordinaire, qui peut elle-même contenir des nombres lus sur l'image.
- **Graduations Y (lot 18, C2)** : une valeur de graduation lue deux fois n'empêche plus l'étalonnage ;
  une étiquette de l'axe X de valeur DIFFÉRENTE entrée dans la colonne des graduations Y (« 2019 » sous
  « 0,0 ») provoque toujours une abstention.

## Courriels et pièces jointes — *activable par flag, borné par espace*

- **Pas d'analyse Vision ni StructX des pièces jointes** : les images jointes sont lues par
  OCR seulement ; les images et figures des PDF/DOCX/PPTX joints ne sont pas décrites.
- **Pas d'antivirus** : aucune chaîne antivirus ou de quarantaine n'existe dans RAGatm. Les
  pièces ne sont **pas** garanties sans logiciel malveillant ; seules s'appliquent les
  protections passives (type réel par contenu, refus des exécutables, des macros, des
  archives, des formats anciens, parsing sans exécution ni accès réseau).
- **Pas de calcul tabulaire** sur un classeur joint (le moteur lit le fichier d'un `Document`).
- **Pas d'aperçu ni de téléchargement propres à une pièce** : le courriel parent se
  télécharge, sous ses droits.
- **Formats à worker dédié** (IFC, DWG) en pièce jointe : `UNSUPPORTED`.
- **S/MIME signé, pièces par référence web, objets OLE** : `UNSUPPORTED` (non lus).
- **Budget de temps partagé** : les pièces s'extraient sous le budget du courriel ; au-delà
  de 60 % du budget, les pièces restantes sont `SKIPPED_LIMIT`. Un PDF joint très long
  (OCR) peut encore épuiser le budget du courriel entier, comme le même PDF déposé seul.
- **Courriels au dépôt bornés par espace** quand `DOCUMENT_EMAIL_GLOBAL_ENABLED=0` : un `.eml`
  n'est accepté que dans un espace de `DOCUMENT_EMAIL_RAGSPACE_IDS` (mode
  général disponible).
- **GIF non pris en charge** (image) : signalé `UNSUPPORTED`, sans rendre le courriel partiel.
- **Courriels ingérés avant 27C** : statuts « sans texte » normalisés à la LECTURE par l'API ; les
  métadonnées stockées gardent l'ancien `FAILED` jusqu'à une éventuelle réindexation.
- **Corps courts ingérés avant une version antérieure** : un corps de moins de 40 caractères
  visibles était écarté (seul l'en-tête indexé) ; corrigé pour les nouvelles ingestions, une
  réindexation rattrape les anciens.

## Références d'implémentation

- `backend/apps/rag_core/repositories/chunk_repository.py`, `backend/apps/rag_core/domain/policies.py`
- `backend/apps/rag_core/benchmark/answerability.py`
- `backend/apps/llm_router/embeddings.py`, `backend/config/settings/prod.py`
- `backend/apps/chat/services/chat_service.py`, `services/ai_gateway/`
- `scripts/prod/backup.sh`, `scripts/prod/restore.sh`, `backend/config/celery.py`
- `.github/workflows/ci.yml`, `Makefile`
- `backend/apps/rag_core/services/ingest/visual/structured.py`, `structured_auto.py`, `curve_shadow.py`, `curve_guards.py`, `backend/apps/rag_core/checks_structured_visual.py`

## Audio

- *Activable par flag*, borné par espace ou général (`DOCUMENT_AUDIO_GLOBAL_ENABLED`). Un seul job ASR en vol par instance : en disponibilité générale, les dépôts concurrents **attendent** (`waiting_for_asr`) et se sérialisent. Formats V1 : MP3, M4A, WAV, FLAC, OGG, WEBM **audio seul** ; ni `.opus` nu ni vidéo.
- Dépôt ≤ **600 000 000 octets** (audio seulement, chemin `/api/v1/documents/upload-audio/`) ; enregistrement ≤ 12 h ;
  parties ≤ 90 min traitées **séquentiellement** (un seul job ASR en vol par environnement, GPU partagé prod/staging/un projet interne).
- `.m4a` : seuls les conteneurs ISO-BMFF reconnus sont admis (dont 3GPP depuis une version antérieure) ; un flux AAC brut (ADTS) renommé `.m4a` et un fichier QuickTime restent refusés (`unsupported_audio`).
- Langues annoncées par le Gateway : français, anglais. Diarisation : 4 locuteurs au plus, libellés techniques (`speaker_00`…), jamais des noms.
- Aucune mesure de qualité ASR (WER) : la transcription est **non vérifiée** (classe `asr_transcript`) ; ses nombres ne sont jamais une preuve.
- Une coupe sans silence disponible peut trancher un mot à la frontière de deux parties (tracé `cut=hard`).
- Pas de lecteur audio ni de lien vers l'instant cité (V1) ; pas de synthèse. Un dépôt interrompu pendant l'envoi au Gateway conclut en échec (`asr_unavailable`), sans second dépôt à l'aveugle.
- Détail : [ingestion-audio.md](../03-backend/ingestion-audio.md).

## AI Gateway Pool

- La Gateway peut être jointe **directement** ou **via AI Gateway Pool** (routeur interne) ;
  les trois URL Gateway doivent suivre le même chemin. Vision via le pool : contrats simulés
  seulement, non qualifiée en live.
- `/health/ready`, `/health/embeddings`, `/health/rerank` (et `/health/structured-extract`,
  coût non documenté) **calculent** côté Gateway : aucun diagnostic RagATM ne les appelle plus,
  à l'exception de la vérification d'identité de pipeline des embeddings (`embedding_reference`,
  une lecture par 5 min et par processus, sur le chemin fonctionnel) et des commandes manuelles
  `check_embedding_capabilities` / `set_ragspace_embedding_pipeline`.
- Un job ASR créé en direct est inconnu du pool (`JOB_AFFINITY_UNKNOWN`) : **vidange obligatoire**
  avant bascule (`asr_drain_preflight`). Filet seulement : ~10 min de relectures puis
  `asr_job_tracking_lost`.
- Un orphelin ASR peut subsister côté Gateway si la compensation du pool échoue
  (`compensation=failed`) ou si la réponse d'un dépôt est perdue
  (`asr_submit_outcome_unknown`) : purgé par la Gateway à expiration, non suivi par RAGatm.
- Pas de TLS ni d'authentification entre RAGatm et le pool (comme l'accès direct) ;
  protection par réseau Docker interne.
