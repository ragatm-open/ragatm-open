# Corpus métier

Objectif : décrire le socle documentaire **partagé** entre workspaces, son administration
superuser, son autorisation par workspace et le retrieval bi-source. Public : développeurs backend,
administrateurs RagATM. Le corpus métier conserve l'étanchéité stricte des documents privés.

## Deux couches documentaires séparées

| | Documents privés | Corpus métier |
|---|---|---|
| Tables | `rag_document` / `rag_chunk` | `domain_corpus_document` / `domain_corpus_chunk` |
| Rattachement | un `RagSpace` d'un workspace | un `DomainCorpus` (aucun workspace) |
| Écriture | membres autorisés du workspace | **propriétaire global RagATM (superuser) uniquement** |
| Partage | jamais | plusieurs workspaces (lecture/retrieval) via association explicite |
| Stockage | `MEDIA_ROOT/rag_docs/...` | `MEDIA_ROOT/corpus_docs/...` (chemins disjoints) |
| Ingestion | asynchrone (Celery) | **synchrone** (déterministe) |

Les deux couches sont **physiquement séparées** : aucune réindexation ou suppression de corpus ne
touche un document privé, et inversement. Voir [modele-donnees.md](modele-donnees.md).

## Modèles

- `BusinessDomain` (`business_domain`) — domaine regroupant des corpus (`slug`, `name`,
  `is_active`).
- `DomainCorpus` (`domain_corpus`) — corpus rattaché à un domaine ; embeddings configurables **par
  corpus** (`embedding_provider` : `ai_gateway` uniquement, `embedding_model` défaut
  `legacy-768`).
- `DomainCorpusDocument` (`domain_corpus_document`) — document de corpus, `status`
  `PENDING/INGESTING/READY/FAILED`, dédup partial-unique(`corpus`, `sha256`) non-FAILED.
- `DomainCorpusChunk` (`domain_corpus_chunk`) — chunk (`VectorField(768)`), `corpus_id` dupliqué
  pour l'isolation.
- `WorkspaceDomainCorpusAccess` (`workspace_domain_corpus_access`) — **seul** mécanisme
  d'autorisation (`enabled`, unique(`workspace`, `corpus`)).

## Administration (superuser uniquement)

Tous les endpoints d'administration sont sous `/api/v1/admin/` et protégés par
`[IsAuthenticated, IsRagATMOwner]` (= `is_superuser`). Un rôle de workspace → `403` ; anonyme →
`401`. Les serializers n'exposent jamais de chemin disque interne.

| Méthode | Chemin |
|---|---|
| GET/POST | `business-domains/` |
| GET/PATCH/DELETE | `business-domains/<domain_id>/` (DELETE cascade) |
| GET/POST | `domain-corpora/` |
| GET/PATCH/DELETE | `domain-corpora/<corpus_id>/` |
| GET/POST | `domain-corpora/<corpus_id>/documents/` (POST = upload, ingest synchrone, `allow_excel=False`) |
| POST | `domain-corpora/<corpus_id>/reindex/` |
| GET/DELETE | `domain-corpus-documents/<document_id>/` |
| POST | `domain-corpus-documents/<document_id>/reindex/` |
| GET/POST | `workspaces/<workspace_id>/domain-corpora/` (associations) |
| DELETE | `workspaces/<workspace_id>/domain-corpora/<corpus_id>/` (idempotent) |

Endpoint **lecture seule** côté workspace (tout membre) :

```
GET /api/v1/workspaces/<workspace_id>/domain-corpora/
```

Ne renvoie que les corpus **associés ET actifs** (`enabled=True` + `is_active`). Workspace sans
corpus → `[]`. Non-membre → `403`. Aucun champ sensible (chemin, texte, modèle d'embedding) exposé.

Détails complets : [../05-api/reference-endpoints.md](../05-api/reference-endpoints.md).

## Ingestion corpus (synchrone)

L'ingestion corpus est **synchrone** (pas de Celery) — `PENDING → INGESTING → READY` dans la même
requête, comportement déterministe. **Excel (XLSX) est refusé pour le corpus** (`allow_excel=False`).
Déduplication par `sha256` au sein du corpus. Les embeddings utilisent le provider/modèle du
corpus.

Réindexation :
- `reindex_corpus(corpus)` — supprime/recrée uniquement les `domain_corpus_chunk`, document par
  document, depuis le fichier source conservé.
- `reindex_corpus_document(document)` — un seul document.
- Aucune table privée n'est lue/modifiée. Changer `embedding_provider`/`embedding_model` impose une
  réindexation (vecteurs incomparables sinon).

## Retrieval bi-source

Le pipeline RAG interroge deux pools et les fusionne :

1. les chunks **privés** du `RagSpace` actif (toujours filtrés par `rag_space_id`) ;
2. les chunks des **corpus autorisés** du workspace — liste d'ids issue **uniquement** de
   `WorkspaceDomainCorpusAccess` (`enabled=True` + corpus actif). Aucun corpus ⇒ liste vide ⇒ le
   retrieval corpus est un **no-op** (comportement privé strictement inchangé).

Le leg lexical corpus exécute le **même plan de requêtes FTS, avec le même repli**, que le
leg privé — stratégie unique dans `rag_core/domain/fts_plan.execute_fts_plan`. Sans elle, le
corpus n'essayait que la requête la plus sélective du plan et rendait **zéro candidat** dès
qu'elle ne matchait pas (typiquement toute question commençant par une majuscule), sans
erreur ni trace.

À score équivalent, le privé est **légèrement favorisé** :

```
RAG_WORKSPACE_SOURCE_BOOST=1.10      # sources privées
RAG_DOMAIN_CORPUS_SOURCE_BOOST=1.00  # sources corpus
```

L'assemblage bi-source a **deux modes** (`RAG_BUSINESS_CORPUS_UNIFIED_FUSION_ENABLED`,
défaut `0`) : historique (corpus ajouté après la fusion privée, donc sans `fusion_score`)
ou unifié (les quatre listes votent dans la même fusion de rangs). Le détail, ses
conséquences de classement et la mesure sont dans
[pipeline-retrieval.md](pipeline-retrieval.md) § 6.

Les sources corpus sont typées `source_type="domain_corpus"` (`source_type` du contrat de sources, introduit en v2 ; payload courant v3). Une provenance **absente ou non reconnue** vaut `unknown` — elle n'est jamais présentée comme un document privé. Voir
[pipeline-retrieval.md](pipeline-retrieval.md) et [../06-ai/answerability.md](../06-ai/answerability.md).

## Résumer un document ou une page de corpus

Les actions « Résumer le document » / « Résumer la page N » du panneau de sources fonctionnent
aussi sur une source de corpus métier, via un **endpoint dédié** et un modèle de permissions
fondé sur la même association `WorkspaceDomainCorpusAccess` que le retrieval. *Activé par
défaut* (`DOMAIN_CORPUS_SUMMARY_ENABLED`, défaut `1`). Voir
[resume-corpus-metier.md](resume-corpus-metier.md).

## Provenance des vecteurs — modèle EXPLICITE obligatoire

Un corpus vectorisé **doit** porter son `embedding_model`. Provider renseigné et modèle
vide ⇒ `CorpusEmbeddingConfigurationError` (code stable
`corpus_embedding_model_missing`), levée **avant** tout appel d'encodage, à l'indexation
comme au retrieval.

Le repli sur les variables globales (`EMBEDDINGS_MODEL`, défaut
de code) est **supprimé** pour ce cas. Il avait produit un incident : à données
identiques et configuration alignée, le leg vectoriel corpus rendait 120 candidats dans
un environnement et **0** dans l'autre — l'un envoyait un nom de modèle inconnu du provider (404,
leg dégradé en silence), l'autre ne marchait que parce qu'une variable globale s'y
trouvait posée.

Le pire cas, permis par la cascade et désormais impossible : un modèle **valide mais
différent** de celui ayant produit les vecteurs encoderait la question dans un espace
incomparable, et le leg répondrait — de façon plausible et fausse.

Un corpus **sans provider** reste résolu par le réglage global : ce n'est pas une
provenance douteuse, c'est un corpus non vectorisé.

Le leg lexical survit à cette erreur ; la trace expose `corpus_vector_error_code`.

Renseigner le modèle d'un corpus existant :

```bash
python manage.py set_corpus_embedding_model                        # inventaire
python manage.py set_corpus_embedding_model --corpus-slug <slug> \
    --model <modèle>                                               # vérification seule
python manage.py set_corpus_embedding_model --corpus-slug <slug> \
    --model <modèle> --confirm                                     # écriture
```

La commande **prouve avant d'écrire** : elle recalcule l'embedding de plusieurs chunks
réels avec le couple annoncé et exige une similarité cosinus ≥ 0,999 avec les vecteurs
stockés. Sans preuve, elle refuse — écrire un modèle non vérifié inscrirait une
provenance fausse, c'est-à-dire le défaut d'origine sous une autre forme. Aucune
réindexation : on documente le modèle qui a produit les vecteurs, on ne les recalcule pas.

## Identité d'embeddings — explicite dès la création

*Statut : **implémenté**.*

Un corpus **naît** avec un couple (provider, modèle) résolu et persisté. Source de vérité
unique : [`apps/corpus/domain/embedding_identity.py`](../../backend/apps/corpus/domain/embedding_identity.py)
— provider unique `ai_gateway`, modèle par défaut `legacy-768` (profil
logique du registre de la Gateway). L'ancienne exception qui permettait à un corpus d'interroger
un provider externe n'existe plus (source de politique `domain-corpus-local-only`).

Le défaut corrigé : l'API de création écrivait `embedding_model=""` parce que son
serializer portait `default=""` — la clé était toujours présente et la chaîne vide
gagnait sur le défaut de champ Django. L'interface n'envoyant que domaine, nom et slug,
**tout corpus créé par l'UI naissait sans modèle**, donc avec un leg vectoriel refusé
(`corpus_embedding_model_missing`) et une admission impossible. C'est l'origine exacte de
l'incident `refvc`.

Choix retenu : un **défaut métier centralisé**, pas une exigence explicite dans la
requête — exiger le modèle aurait cassé l'interface existante, et le côté privé applique
déjà la même politique via `default_ragspace_embedding_*`. Le défaut n'est écrit qu'à un
seul endroit.

Erreurs structurées possibles : `corpus_embedding_provider_invalid`,
`corpus_embedding_model_required`, `corpus_embedding_model_invalid`,
`corpus_embedding_dimension_invalid`.

## Empreinte d'indexation — ce qui a produit les vecteurs

*Statut : **implémenté**.*

Chaque `DomainCorpusDocument` porte l'identité **réellement utilisée** pour produire ses
vecteurs : `embedding_provider`, `embedding_model`, `embedding_dimension`,
`embedding_fingerprint` (sha256 de `version_pipeline|provider|modèle|dimension`) et
`reindex_required`.

**Pourquoi au niveau document et non du chunk.** `index_corpus_extraction_result` indexe
un document en **une transaction** : suppression des anciens chunks, un seul appel
d'embeddings pour tous les passages, création en lot, `READY`. Un échec supprime les
chunks et pose `FAILED`. Un document ne peut donc jamais porter deux modèles — une
empreinte par chunk serait redondante et bien plus coûteuse à vérifier.

**L'empreinte décrit un fait, pas une intention.** Elle est construite **après** l'appel
d'embeddings et écrite dans la même transaction que les chunks et le statut `READY` : un
document déclaré cohérent l'est nécessairement. En cas d'échec, elle est effacée avec les
chunks — un document en erreur ne conserve pas la preuve d'une indexation qui n'existe
plus.

Une empreinte **vide** signifie « provenance non prouvée » (documents antérieurs à ce
lot), jamais « conforme au modèle courant ». Seule la commande
`backfill_corpus_embedding_fingerprints` peut la renseigner, et uniquement sur preuve :

```bash
python manage.py backfill_corpus_embedding_fingerprints --inventory
python manage.py backfill_corpus_embedding_fingerprints --corpus <slug> --dry-run
python manage.py backfill_corpus_embedding_fingerprints --corpus <slug> --apply
```

La preuve est celle de `set_corpus_embedding_model` : réencodage de trois chunks réels
avec le couple déclaré, cosinus ≥ 0,999 exigé. Trois verdicts :

| verdict | traitement |
|---|---|
| `proven_consistent` | empreinte écrite, `reindex_required=False` |
| `proven_inconsistent` | **aucune empreinte**, `reindex_required=True` — les vecteurs ne viennent pas du couple déclaré (dimension différente, ou cosinus sous le seuil) |
| `unknown` | **aucune empreinte**, `reindex_required=True` — la preuve n'a pas pu être faite (provider injoignable, aucun chunk vectorisé) |

Aucun embedding n'est modifié, aucun chunk supprimé, aucune calibration touchée. La
commande est idempotente : un document déjà prouvé est reprouvé et réécrit à l'identique.

## Changer le modèle d'un corpus — jamais silencieux

*Statut : **implémenté**.*

Un `PATCH` de `embedding_provider` ou `embedding_model` ne refait pas les vecteurs. Sans
garde, le retrieval encoderait la question avec le **nouveau** modèle et la comparerait
aux **anciens** vecteurs. Mesuré sur cette installation, cette comparaison croisée ne
lève **aucune erreur** : elle rend une page pleine de candidats, des scores plausibles
(0,512–0,522, à l'intérieur de la plage légitime des négatifs) et **0 à 1 chunk pertinent
sur 10**.

Le `PATCH` applique donc la même politique que celui d'un `RagSpace` :

1. le nouveau couple est validé puis persisté ;
2. **tous** les documents du corpus passent à `reindex_required=True` ;
3. les vecteurs, chunks et fichiers **ne sont pas touchés** ;
4. la calibration active dont l'identité ne correspond plus est **désactivée** (conservée,
   pas supprimée : une décision passée doit rester explicable) ;
5. la réponse est explicite :

```json
{ "embeddings_reindex_required": true, "affected_documents": 12,
  "calibrations_invalidated": 1 }
```

Un `PATCH` qui renvoie la même valeur — ou la même à la casse près — ne déclenche rien :
la comparaison est normalisée. Renommer un corpus ou changer sa description n'a aucun
effet sur l'indexation.

## Admission calibrée — un corpus ne participe pas par défaut

*Statut : **implémenté, activable par flag** (`RAG_BUSINESS_CORPUS_CALIBRATED_ADMISSION_ENABLED`,
défaut `0` partout).*

Un corpus associé et autorisé n'est pas pour autant pertinent pour **toute** question.
Mesuré sur 70 questions annotées : sans porte, 52 % des passages du top-8 viennent du
corpus métier même sur les questions manifestement hors sujet.

La porte décide, **par corpus et par question**, si le corpus participe à la fusion. La
règle et ses limites sont documentées dans
[pipeline-retrieval.md § admission calibrée](pipeline-retrieval.md#admission-calibrée-dun-corpus--rag_business_corpus_calibrated_admission_enabled).
Trois points structurants pour l'exploitation d'un corpus :

1. **La calibration est propre au corpus** et à son modèle d'embeddings. Elle n'est pas
   transposable : l'échelle des similarités change avec le modèle.
2. **Sans calibration active et valide, le corpus est rejeté** quand la porte est
   activée — jamais admis en silence. Un corpus non calibré n'est pas un corpus réputé
   pertinent.
3. **Changer `embedding_model` invalide la calibration** (identité vérifiée à chaque
   décision) : il faut recalibrer, comme il faut réindexer.

```bash
python manage.py calibrate_business_corpus_admission --corpus <slug> \
    --dataset <annotations.json>                       # plan + métriques, aucune écriture
python manage.py calibrate_business_corpus_admission --corpus <slug> \
    --dataset <annotations.json> --output <rapport.json> --apply
```

Les calibrations sont **versionnées** (`domain_corpus_admission_calibration`, au plus une
active par corpus) et conservées : une décision de retrieval prise en production reste
explicable après un recalibrage. Ne sont stockés que deux coefficients, l'identité de
l'espace vectoriel, l'empreinte du jeu annoté et les métriques — jamais de question, de
texte documentaire ni de réponse.

## État de cohérence exposé à l'administration

L'API d'administration (`GET`/`POST`/`PATCH` sur `domain-corpora/`) renvoie, **calculés
par le serveur** à chaque lecture :

| Champ | Sens |
|---|---|
| `embedding_dimension` | dimension attendue (colonne pgvector) |
| `embedding_identity_complete` | provider **et** modèle renseignés |
| `embedding_consistency_status` | `consistent`, `partially_consistent`, `reindex_required`, `fingerprint_missing`, `identity_mismatch`, `no_ready_documents` |
| `embedding_consistency_error_code` | code stable quand la situation en appelle un |
| `embedding_consistent_document_count` | documents dont les vecteurs sont comparables |
| `embedding_inconsistent_document_count` | empreinte **présente et différente** |
| `embedding_unknown_document_count` | empreinte **absente** (provenance non prouvée) |
| `embedding_reindex_required_document_count` | documents marqués à réindexer |
| `embeddings_reindex_required` | au moins un document à réindexer |
| `vector_retrieval_available` | au moins un document sûr à interroger |
| `active_calibration_valid` / `_error_code` / `_version` | état de la calibration d'admission |

Aucune de ces valeurs n'est acceptée depuis le client : une cohérence affirmée par un
client serait une affirmation invérifiable sur l'état de la base.

L'interface `/ragatm-admin` affiche le statut en pastille, signale explicitement
« recherche vectorielle indisponible », et détaille en infobulle l'identité, les
décomptes et la validité de la calibration. L'admin Django expose la même colonne — il
permet d'écrire provider et modèle sans passer par l'API, et la garde de réindexation
est posée **sur le modèle** (`DomainCorpus.save`) pour couvrir toutes les voies d'écriture.

## Compteurs de documents exploitables

`documents_count` est le **total** et ne dit rien de l'exploitabilité : un corpus dont
l'ingestion a échoué affiche le même nombre qu'un corpus pleinement indexé. Les endpoints
de consultation et d'administration exposent donc aussi :

| Champ | Définition |
|---|---|
| `documents_count` | total, tous statuts (**conservé** pour compatibilité) |
| `ready_documents_count` | statut `READY` — **exactement** la définition d'éligibilité du retrieval |
| `processing_documents_count` | `PENDING` + `INGESTING` |
| `failed_documents_count` | `FAILED` |

Source unique : `apps/corpus/services/document_counts.py` (agrégats filtrés, une seule
requête, aucun N+1). L'UI affiche « 12 documents · 10 prêts · 2 en erreur ». Un backend
antérieur à ces champs n'affiche aucune mention de documents prêts, plutôt qu'une valeur
inventée.

## Observabilité du leg corpus

`retrieval_trace.business_corpus` répond à « pourquoi ce corpus n'a-t-il pas été cité ? »
sans exposer ni question, ni requête FTS, ni extrait, ni nom de document :

- périmètre : `associated_corpus_count`, `active_corpus_count`, `ready_corpus_document_count` ;
- exécution : `corpus_lexical_status`, `corpus_vector_status`, `corpus_fts_plan_index`,
  `corpus_fts_attempt_count`, `corpus_fts_fallback_used` ;
- survie : `corpus_candidate_count`, `corpus_pool_count`, `corpus_pre_rerank_count`,
  `corpus_post_rerank_count`, `corpus_selected_count`, `private_candidate_count`,
  `private_selected_count`, `per_corpus` (UUID + compteurs, borné à 10 corpus) ;
- budget de prompt (`prompt_budget.*`) : `observed`, `dropped_workspace`, `dropped_corpus`,
  plus la comptabilité du budget — `prompt_overhead_chars` (coût FIXE des consignes),
  `source_budget_chars`, `selected_source_chars`, `sources_before_budget`,
  `sources_after_budget`, `sources_dropped_by_budget`, `final_source_truncated`,
  `final_source_original_chars`, `final_source_retained_chars`,
  `prompt_budget_exhausted`, `prompt_budget_unable_to_fit_minimum_source` ;
- admission (`admission.*`) : `calibrated_admission_enabled`, `admission_signal_name`,
  `admission_signal_value`, `admission_calibrator_type`, `admission_calibration_version`,
  `admission_probability`, `admission_probability_threshold`, `corpus_admitted`,
  `corpus_admission_reason`, `corpus_admission_error_code`,
  `corpus_candidate_count_before_admission`, `corpus_candidate_count_after_admission`,
  `per_corpus` (décision par corpus, bornée à 10) ;
- cohérence d'empreinte (`embedding_consistency.*`) : `vector_refused_for_incoherence`
  et, `per_corpus` (borné à 10), l'identifiant technique du corpus avec ses décomptes
  (`ready_document_count`, `embedding_consistent_document_count`,
  `embedding_inconsistent_document_count`, `embedding_unknown_document_count`,
  `embedding_reindex_required_document_count`), son `embedding_consistency_status`,
  son `embedding_consistency_error_code` et `vector_retrieval_available` ;
- verdict : `corpus_drop_stage` ∈ `not_associated`, `disabled`, `no_ready_documents`,
  `fts_error`, `vector_error`, `fts_no_match`, `admission`, `merge`, `diversification`,
  `reranking`, `prompt_budget`, `threshold`, `selected`.

`corpus_drop_stage` distingue **l'absence de candidat** de **l'élimination d'un candidat**,
et ne déclare jamais `reranking` si aucun reranking n'a tourné. L'étape `admission` dit
que des candidats existaient et que le corpus a été écarté **par décision** — sans elle,
ce rejet se lirait `merge`, c'est-à-dire exactement le défaut de classement que la porte
n'a pas.

La trace expose la **version** de la calibration, jamais ses coefficients : la version
suffit à rejouer une décision, et n'alourdit pas les journaux. Les valeurs rapportées de
façon agrégée sont celles du corpus dont la probabilité est la plus élevée — sur un
rejet, celui qui a failli passer, donc le seul utile pour régler un seuil.

## Garanties d'étanchéité

- Un workspace A ne voit **jamais** les documents privés d'un workspace B, même s'ils partagent le
  même corpus (filtre SQL `rag_space_id`, verrou anti-fuite sur le JOIN).
- Le retrieval corpus n'interroge **jamais** tous les corpus : uniquement la liste explicite d'ids
  autorisés. Un corpus non associé ou désactivé est invisible.
- Pas de viewer de document corpus côté workspace : les originaux ne sont pas exposés aux membres.

Voir les limites connues dans [../10-reference/limites-connues.md](../10-reference/limites-connues.md).

## Références d'implémentation

- `backend/apps/corpus/services/document_counts.py` (compteurs par statut)
- `manage.py set_corpus_embedding_model` (provenance des vecteurs, preuve avant écriture)
- `backend/apps/rag_core/domain/fts_plan.py` (exécution du plan FTS, partagée)
- `backend/apps/rag_core/domain/provenance.py` (énumération de provenance)
- `backend/apps/rag_core/benchmark/bi_source.py` + `manage.py bench_bi_source_retrieval`
- `backend/apps/corpus/models/` (`domain.py`, `corpus.py`, `document.py`, `chunk.py`, `access.py`)
- `backend/apps/corpus/services/ingest.py`, `backend/apps/corpus/services/retrieval.py`
- `backend/api/v1/admin_corpus/`
- `backend/api/v1/workspaces/` (endpoint `domain-corpora/` lecture seule)
- `backend/apps/rag_core/services/query/orchestrator.py` (fusion bi-source)
