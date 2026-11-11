# Observabilité du retrieval — trace structurée (Lot P1-A0)

*Statut : implémenté, activé par défaut (additif).* 

Le noyau `query_rag` produit, à chaque appel, une **trace structurée** qui rend les
dégradations du pipeline **explicitement observables**. C'est une donnée
d'**observabilité** : elle n'influence ni les scores, ni l'ordre des sources, ni la
sélection, ni la politique de réponse. Les valeurs centralisées vivent dans
[`backend/apps/rag_core/services/query/trace.py`](../../backend/apps/rag_core/services/query/trace.py).

## Distinction centrale

Un résultat sans source n'est plus interprétable à partir du seul
`{"status": "no_answer", "sources": []}` :

| Situation | Interprétation |
|---|---|
| `no_answer` + `retrieval_degraded=false` | **absence LÉGITIME** de preuve (le corpus ne contient pas la réponse) |
| `no_answer` + `retrieval_degraded=true` | **réponse vide obtenue PENDANT une dégradation** (un leg a échoué) |

## Outcome global

`schema_version = 1`. Valeurs stables (snake_case) :

| `outcome` | Règle |
|---|---|
| `success` | des sources ET aucun leg dégradé |
| `empty_legitimate` | aucune source, aucun leg dégradé |
| `degraded` | un leg demandé/nécessaire a échoué, le pipeline a continué |
| `error` | le pipeline a été interrompu et l'erreur remontée (`raise`) |

## Statuts de leg

| `status` | Sens | Compte comme dégradation ? |
|---|---|---|
| `not_requested` | leg non demandé pour cette requête | non |
| `disabled` | leg désactivé par configuration (ex. `RAG_HYBRID_ENABLED=0`) | non |
| `succeeded` | exécuté, au moins un candidat | non |
| `empty` | exécuté normalement, aucun candidat | non |
| `degraded` | demandé mais a échoué (pipeline poursuivi) | **oui** |
| `failed` | demandé mais impossible à exécuter | **oui** |

`retrieval_degraded` est vrai **si et seulement si** au moins un leg est `degraded`
ou `failed`. Un leg `disabled` / `not_requested` / `empty` n'est **jamais** une
dégradation.

Legs instrumentés : `fts`, `vector`, `business_corpus`, `temporal`, `rerank`,
`rerank_gateway`. Le leg `temporal` est **informatif** (jamais `degraded` en P1-A0 :
les erreurs de date sont best-effort/cosmétiques). Le leg `rerank` réutilise
`RerankInfo.fallback_used` (cross-encoder → stub) comme signal de dégradation, sans
seconde doctrine.

## Les deux rerankings occupent deux legs

*Statut : implémenté.*

`rerank` = reranking **historique** (`RAG_RERANK_ENABLED`, modèle chargé dans le
process). `rerank_gateway` = reranking via l'**AI Gateway** (`RAG_RERANKER_ENABLED`,
`POST /v1/rerank`). Deux mécanismes, deux legs : un état unique ne pourrait pas dire
lequel a tourné, et les deux ne peuvent d'ailleurs pas être actifs ensemble
(`rag_core.E008`).

> **Le défaut corrigé.** `query_rag` appelait `trace.record_rerank_gateway(...)`
> derrière un `hasattr(trace, "record_rerank_gateway")` — une méthode qui n'avait
> jamais existé. Le garde était donc toujours faux, l'appel toujours sans effet.
> Constaté en production le 22/08/2026 : **29 appels Gateway comptabilisés, 570
> `RagQueryLog` inspectés, zéro trace de reranking**, et le leg historique affichant
> `rerank: not_requested`. Ce n'était pas une métrique manquante : c'était un audit
> qui affirmait le contraire de ce qui s'était passé.

### États du leg `rerank_gateway`

| État | Sens |
|---|---|
| `not_requested` | drapeau éteint, ou pool vide — aucun appel |
| `success` | la Gateway a rendu un classement complet et conforme |
| `timeout` | délai dédié dépassé |
| `invalid_response` | réponse structurellement fausse (cardinalité, identifiant, score) |
| `failure` | tout autre échec d'appel |
| `not_recorded` | **lecture seule** : trace antérieure à ce leg |

Deux distinctions portent tout le reste :

- **`fallback` est un booléen séparé, pas un état.** L'état dit ce qui est arrivé à
  l'*appel* ; `fallback` dit ce que le pipeline en a fait. Sous `fail_open_rrf`, une
  requête aboutit pour l'utilisateur pendant que le reranking a échoué — les
  confondre est exactement le défaut que ce leg corrige. Un état `FALLBACK` effacerait
  en outre la cause, alors qu'une Gateway lente et une réponse non conforme ne se
  corrigent pas de la même façon.
- **`not_recorded` ≠ `not_requested`.** « Le reranking n'a pas eu lieu » et « on ne
  sait pas s'il a eu lieu » sont deux affirmations différentes. Les traces antérieures
  à ce leg se lisent avec `trace.read_gateway_rerank()`, qui rend la seconde.

Un repli marque le leg `degraded` — même doctrine que le leg historique : servir
l'ordre RRF en croyant servir un classement reranké **est** une dégradation.

### Mouvements de rang

| Champ | Source | Moment du pipeline |
|---|---|---|
| `before_rank` | position dans le pool **canonique RRF** (`select_rrf_pool`) | ce qui est réellement soumis à la Gateway |
| `after_rank` | `rerank_rank` rendu par la Gateway | **avant** diversification et budget de prompt |
| `delta` | `before_rank − after_rank` (positif = remonté) | — |
| `score` | `rerank_score` **natif**, jamais normalisé ; absent plutôt que `0` | — |
| `survived_in_final_context` | intersection avec les sources transmises | après sélection |

`after_rank` n'est **pas** la position finale au prompt : la diversification et le
budget passent après. C'est précisément l'écart entre les deux que
`survived_in_final_context` rend mesurable.

`before_rank` suit le pool canonique, jamais l'ordre physique de la liste reçue — qui
est celui de l'hygiène. C'est le piège que `select_rrf_pool` existe pour empêcher, et
le tracer autrement le réintroduirait ; un test le verrouille.

### Deux surfaces, deux besoins

Les mouvements sont **persistés** (`RagQueryLog.retrieval`) mais **absents de la
réponse** de `query_rag` : à 40 candidats ils pèsent ~7 ko, que le chat n'a aucun
usage de transporter à chaque requête. L'état et les agrégats (`moved_count`,
`top1_changed`, `duration_ms`, survie) figurent dans les **deux** — savoir ce que le
reranking a fait ne doit jamais exiger d'aller lire la base.

Volumétrie bornée par construction : le pool est plafonné à 64 au registre.

## Trace des candidats — `candidate_trace`

*Statut : implémenté, **activable par flag**, `RAG_CANDIDATE_TRACE_ENABLED` **OFF par défaut**.*

### Pourquoi elle existe

Le run one-shot de `balanced-v1` n'a persisté **qu'un scalaire par requête** :
`top1_score`. Le diagnostic qui a suivi a donc pu établir *que* le signal échouait,
mais pas *où* : impossible de distinguer un support jamais remonté d'un support mal
classé, ni de dire si un candidat venait du corpus privé ou du corpus métier. Le
diagnostic a dû écrire « non prouvable » là où une colonne aurait suffi — et le jeu
consommé ne se rejoue pas.

`candidate_trace` rend ces questions décidables **pour les requêtes à venir**.

### Ce que la trace n'est pas

Une **observation**, pas une entrée de policy. Rien ne la lit : ni l'admission, ni le
classement, ni la réponse. Un test compare le retrieval drapeau ON et drapeau OFF
champ par champ plutôt que de faire confiance à cette phrase.

### Schéma v1

Au niveau de la **requête** :

| Champ | Sens |
|---|---|
| `version` | `1`. Une évolution incompatible l'incrémente : un consommateur doit pouvoir refuser une trace qu'il ne sait pas lire. |
| `ranking_mode` | `normal_reranker` · `fallback_rrf` · `reranker_unavailable` · `no_candidates` |
| `reranker_used` / `fallback_used` | booléens, dérivés du mode |
| `pool_size` | nombre de candidats tracés |
| `submitted_to_reranker` | nombre réellement soumis à la Gateway |
| `truncated` | le pool dépassait le plafond dur (200) |
| `failure_kind` | catégorie d'échec du reranker, quand il y en a une |
| `identities` | identité du reranker, et **familles d'embedding ventilées par magasin** |

Par **candidat** :

| Champ | Sens | Absent quand… |
|---|---|---|
| `candidate_id` / `chunk_id` | identifiant du fragment | jamais (une ligne sans identifiant est écartée) |
| `document_id` | document porteur | le pipeline ne l'a pas posé |
| `source_store` | `private_ragspace` · `business_corpus` · `unknown` | — |
| `source_methods` | `["lexical"]`, `["vector"]`, ou les deux | liste vide si aucun rang de leg |
| `pre_rerank_rank` | rang dans le pool **canonique RRF** | — |
| `post_rerank_rank` | rang rendu par le reranker | `null` si non reranké |
| `reranker_score` | logit **natif**, jamais normalisé | `null` si non reranké |
| `vector_score` / `lexical_score` | score du leg correspondant | `null` si le candidat n'a pas participé à ce leg |
| `fusion_score` | score RRF | `null` si absent |
| `submitted_to_reranker` | le candidat faisait partie de la tête soumise | — |

### Sémantique des scores — quatre échelles, quatre champs

**Aucune n'est comparable aux autres**, et les mélanger produirait des comparaisons
fausses. C'est pourquoi elles ne partagent jamais un champ :

| Score | Origine | Sens | Échelle |
|---|---|---|---|
| `lexical_score` | `ts_rank` PostgreSQL | plus grand = meilleur | ~0,01–0,1, non normalisé |
| `vector_score` | similarité cosinus | plus grand = meilleur | ~0,4–0,9, non normalisé |
| `fusion_score` | RRF, `Σ poids / (K + rang)` | plus grand = meilleur | somme de rangs réciproques — **pas** une pertinence |
| `reranker_score` | logit du cross-encoder | plus grand = meilleur | non calibré, **sans zéro absolu** ; observé de −4,95 à +8,30 |

Le dernier point est celui qui a fait échouer `balanced-v1` : un score
d'**ordonnancement** avait été employé comme mesure **absolue**. Voir
[answerability.md](../06-ai/answerability.md).

### `null` n'est pas `0`

`merge_rrf` initialise `text_score` et `vector_score` à `0.0` pour **tous** les
candidats. Un `0` ne distingue donc pas « absent de ce leg » de « trouvé avec une
pertinence nulle ». La trace se fonde sur la présence du **rang** de leg
(`text_rank`, `vector_rank`), et écrit `null` quand le candidat n'y a pas participé.
Un `0` se lirait comme un jugement de non-pertinence ; c'est une affirmation, pas un
fait.

### Magasin d'origine : lu, jamais déduit

`source_store` se lit sur le `source_type` que le pipeline pose lui-même au moment où
il sait d'où vient le fragment. Rien n'est inféré d'un nom de document ni d'un score.
Un fragment non marqué est déclaré `unknown` — une information, là où un
`private_ragspace` inventé serait une erreur silencieuse.

Les deux magasins n'utilisent pas la même famille d'embedding : `identities.embedding_by_store`
les ventile, sans quoi un score vectoriel privé et un score vectoriel métier se
liraient comme comparables alors qu'ils ne le sont pas.

#### Provenance de chaque identité

Elle n'est ni dérivée ni devinée : chaque magasin la fait remonter depuis la couche
qui vient de **l'employer**.

| magasin | champs | source canonique |
|---|---|---|
| `private_ragspace` | `provider`, `model`, `pipeline_family`, `dimension` | l'appel d'embeddings de la requête : `provider`/`model` bornent la recherche vectorielle, `pipeline_family` vient de l'identité rendue par la Gateway, `dimension` est la longueur du vecteur obtenu |
| `business_corpus` | `provider`, `model`, `dimension`, `groups` | le groupement `(provider, modèle)` que le service corpus calcule **déjà** pour n'encoder qu'une fois par famille ; `dimension` est la **longueur du vecteur obtenu** (`len(query_embedding)`) |

**Aucune requête supplémentaire, donc aucun N+1** : les deux valeurs existaient dans le
chemin de récupération, elles n'y étaient simplement pas transportées. Un test compte les
requêtes SQL drapeau éteint et drapeau allumé et exige le **même nombre**.

`pipeline_family` n'apparaît **pas** pour le corpus métier : la table
`domain_corpus_chunk` ne porte pas cette colonne et l'identité gouvernée est propre aux
espaces privés. Le champ est donc **absent**, pas nul — un `null` se lirait comme
« famille inconnue » plutôt que « notion sans objet ici ».

Quand plusieurs corpus d'un même workspace n'utilisent pas le même modèle, `groups`
les expose tous plutôt que d'en élire un.

**La dimension est celle du vecteur obtenu, jamais celle demandée.** Un provider peut
rendre autre chose que la valeur réclamée, et une identité d'embedding qui recopierait
la constante de configuration masquerait exactement l'écart qu'elle devrait montrer.

Si une couche n'a rien consigné — recherche vectorielle désactivée, par exemple — la
clé reste **absente**. Rien n'est rempli d'un placeholder ; côté corpus métier, une
`dimension` absente signale qu'aucune question n'a été encodée pour ce magasin.

### Activation et coût

```bash
RAG_CANDIDATE_TRACE_ENABLED=1
```

**OFF par défaut** : une capacité de diagnostic ne s'allume pas seule en production.
Drapeau éteint, le pipeline ne paie même pas le tri du pool canonique.

Surcoût mesuré sur données synthétiques (2 000 itérations) :

| Pool | p50 | p95 | JSON |
|---|---:|---:|---:|
| 40 candidats | 0,130 ms | **0,136 ms** | 17 ko |
| 100 candidats | 0,328 ms | 0,355 ms | 43 ko |
| 200 candidats | 0,656 ms | 0,707 ms | 86 ko |

Soit **0,06 %** de la latence p95 du reranker en production (221,9 ms). Le volume est
borné par construction : plafond dur à 200 candidats, `truncated` le dit.

### Confidentialité

La trace ne contient **jamais** de texte métier : ni question, ni passage, ni document,
ni vecteur, ni secret. Uniquement identifiants, rangs, scores, magasins et identités.
Un test parcourt récursivement la structure et échoue sur toute clé de contenu.

L'hydratation du texte se fait **à part**, à partir des identifiants — comme pour le
jeu de cas difficiles.

### Capture par le benchmark

```bash
RAG_CANDIDATE_TRACE_ENABLED=1 python manage.py rag_benchmark \
    --dataset <jeu>.jsonl --capture-candidates candidate-traces.jsonl
```

Le pipeline **produit** la trace, le caller la **persiste**. Ce découplage est ce qui
permet de capturer **sans utilisateur, sans session et sans `RagQueryLog`** — le manque
exact qui a privé le diagnostic de toute donnée. La sortie est un JSONL : une ligne
d'en-tête portant l'identité runtime, puis une ligne par cas.

Sans le drapeau, la commande **refuse** d'écrire plutôt que de produire un fichier vide :
un fichier vide se lirait « aucun candidat », alors que le fait est « aucune trace
demandée ».

### Limites

- La trace décrit le pool **au moment du reranking**. La diversification et le budget
  de prompt passent après : `post_rerank_rank` n'est pas la position finale au prompt.
- Elle n'est **pas** persistée en base par défaut ; à 17 ko pour 40 candidats, ce n'est
  pas une charge à porter sur chaque requête d'exploitation.
- Elle ne dit pas *pourquoi* le reranker a jugé ainsi — seulement ce qu'il a fait.

## Voisins visuels de page — `visual_neighbors` (VISUAL-RETRIEVAL-QUALITY-1B)

Clé **additive** de `retrieval_trace` (réponse) et de `RagQueryLog.retrieval` (résumé
persisté), **absente** quand aucune promotion n'a eu lieu. Une entrée par visuel placé
derrière son passage natif, bornée par le nombre de passages natifs examinés (3) :

```json
{"reason": "same_page_visual_neighbor", "native_chunk_id": "…", "visual_chunk_id": "…",
 "document_id": "…", "page_number": 22, "original_position": 24, "promoted_position": 2}
```

`original_position` est la position (1-based) dans la sélection diversifiée, `null` si le
visuel venait du pool sans y figurer ; `promoted_position` sa position avant le budget de
prompt — qui peut encore l'écarter. Identifiants et positions seulement : aucun texte,
aucun score. Un visuel déjà juste derrière son natif est aussi consigné (positions
égales). Détail du mécanisme : [pipeline de retrieval §5bis](pipeline-retrieval.md).

## Point d'entrée

Chaque appel porte un `entrypoint` stable, transmis EXPLICITEMENT (jamais déduit de
la stack) : `chat_sync`, `chat_stream`, `core_api`, `benchmark`, `holdout`,
`internal` (défaut sûr). Toute valeur inconnue retombe sur `internal`.

## Exposition

| Surface | Contenu | Champ |
|---|---|---|
| Résultat interne `query_rag` | trace complète + drapeau | `retrieval_trace` (dict), `retrieval_degraded` (bool) |
| API `/core/retrieve/` | **résumé sûr** (outcome, degraded, legs) | `retrieval` |
| Audit `RagQueryLog` | résumé non sensible + mouvements de rang Gateway | colonne `retrieval` (JSON, migration `0014`) |
| Audit `RagQueryLog` | reranking **historique** uniquement (forme inchangée) | colonne `rerank` (JSON) |
| Benchmark (artefacts) | résumé par cas | `CaseResult.retrieval` |
| Logs serveur | log structuré `retrieval_leg_degraded` (`error_kind` = nom de classe) | — |

## Confidentialité (impératif)

La trace, les logs structurés et l'audit d'instrumentation ne contiennent **jamais** :
la question, le prompt, un chunk, du contenu documentaire, un secret, un détail SQL,
une stack trace, ni un identifiant à forte cardinalité. Seule la **catégorie**
d'erreur (nom de classe, via `safe_error_kind`) est conservée.

## Politique d'erreur des chemins critiques (Lot P1-A1)

*Statut : implémenté, activé par défaut.*

P1-A1 exploite la trace ci-dessus pour **restreindre** les captures larges des chemins
critiques : un **bug de programmation** n'est plus transformé en résultat vide / fallback
trompeur (FAIL CLOSED), tandis qu'une **panne de dépendance attendue** reste dégradable
quand un fallback légitime existe. Taxonomie :
[`backend/apps/rag_core/services/query/errors.py`](../../backend/apps/rag_core/services/query/errors.py).

| Exception typée | Sens | Politique |
|---|---|---|
| `RetrievalInvariantError` | bug de programmation / invariant brisé | **propagée** (FAIL CLOSED) |
| `RetrievalConfigurationError` | configuration invalide | **propagée** (FAIL CLOSED) |
| `RetrievalDependencyError` | panne externe attendue | dégradable **si** fallback valide |
| `RetrievalLegUnavailable` | leg requis KO, aucun fallback | **propagée** (FAIL CLOSED) |

Le **classifieur** `is_programming_error` traite comme bugs (FAIL CLOSED) : `TypeError`,
`AttributeError`, `NameError`, `KeyError`, `IndexError`, `ImportError`,
`NotImplementedError`, `AssertionError`, `UnboundLocalError` et
`django.db.ProgrammingError` (SQL erroné). `OperationalError`/timeout/`RuntimeError` =
dépendance (dégradable). `ValueError` n'est **pas** classé bug (souvent donnée invalide).

### Matrice de politique par leg

| Leg | Erreur | Politique | Outcome |
|---|---|---|---|
| **FTS** | bug interne (TypeError, SQL `ProgrammingError`…) | propagée | `error` |
| **FTS** | panne dépendance, **aucun** fallback vectoriel valide | propagée (`RetrievalLegUnavailable`) | `error` |
| **FTS** | panne dépendance, fallback vectoriel valide | degraded | `degraded` |
| **FTS** | exécuté, aucun candidat | légitime | `empty_legitimate` |
| **Vector** | désactivé (`RAG_HYBRID_ENABLED=0`) | disabled | inchangé |
| **Vector** | bug interne / config | propagée | `error` |
| **Vector** | provider indispo / timeout (FTS valide) | degraded | `degraded` |
| **Corpus** | non configuré | not_requested | inchangé |
| **Corpus** | bug interne | propagée | `error` |
| **Corpus** | indispo (SQL/embedding) — corpus **optionnel** | degraded + repli privé | `degraded` |
| **Ranking** (boost/tri/strong-id/enrich) | bug interne | propagée | `error` |
| **Ranking** | donnée invalide | best-effort loggé | inchangé |
| **Garde Excel** | erreur interne | **fail closed** (`RetrievalInvariantError`) | 500 / SSE `error` |
| **Audit** (`maybe_update_log`) | échec DB | secondaire : chat continue, **loggé** (plus de silence) | — |

### Frontières de capture

- Le **dispatch chat** (`_query_rag_dispatch`) n'entoure plus l'exécution d'un
  `except TypeError` : la compatibilité de signature est résolue par **feature
  detection** (`inspect.signature`). Un `TypeError` interne se propage (jamais réessayé).
- Les **vues HTTP/SSE** conservent leur dernière barrière `except Exception` →
  `500` contrôlé / `done{status:"error"}`, `request_id`, ordre `meta→sources→token→done`
  inchangé, aucune donnée sensible exposée.

### Cas restés volontairement best-effort

Les enrichissements purement cosmétiques (date document, tri news, boost) restent
best-effort **pour les données invalides** — mais tout **bug** y est désormais propagé,
et l'échec est loggé (plus de `pass`/`return` muet).

## Contrats internes de l'orchestrateur (Lot P1-B1)

*Statut : implémenté, activé par défaut. Contrats **internes** — jamais sérialisés en API.*

`query_rag` reste la **façade publique compatible** : elle normalise ses arguments
historiques vers des DTO internes typés puis poursuit sur le corps existant (**aucune
étape déplacée** — le découpage est le périmètre de P1-B2). Le **dict historique reste
la source de vérité unique** ; `QueryResult` n'en est qu'une vue immuable
(`to_legacy_dict()` le renvoie verbatim). Contrats :
[`backend/apps/rag_core/services/query/contracts.py`](../../backend/apps/rag_core/services/query/contracts.py).

| DTO | Propriétaire | Immuable | Contient du texte | Sérialisable publiquement |
|---|---|---|---|---|
| `QueryRequest` | orchestrateur (entrée) | oui | question (repr masqué) | non |
| `PreparedQuery` | phase préparation | oui | requêtes (repr masqué) | non |
| `RetrievalPolicy` | résolution politique | oui | non | non |
| `RetrievalCandidate` | unité candidat | oui | texte (repr masqué) | non |
| `CandidateSet` | phase retrieval | non (références) | chunks (références) | non |
| `RankedCandidateSet` | phase ranking | oui | chunks (références) | non |
| `QueryResult` | façade (sortie) | oui | prompt/sources (via `raw`) | **adaptation `to_legacy_dict` uniquement** |
| `QueryExecutionContext` | transversal | non | non | non |

Invariants clés : `QueryRequest` accepte `rag_space_id=None` (→ `no_answer`, pas une
erreur) ; `RetrievalPolicy` valide `top_k>0` et seuils finis (sinon
`RetrievalConfigurationError`) ; `RetrievalCandidate` exige `chunk_id` + score fini
(sinon `RetrievalInvariantError`) ; **l'ordre de `RankedCandidateSet` EST le contrat de
ranking** (aucun re-tri). Aucun `repr` ne divulgue de contenu. Les DTO **ne sont pas**
threadés dans le corps des legs (P1-B2) ; ils sont peuplés à partir des données réelles
(références, pas de copie de texte, pas de conversion dict↔dataclass en boucle chaude).

## Pipeline extrait de query_rag (Lot P1-B2)

*Statut : implémenté, activé par défaut. Comportement byte-identique.*

`query_rag` est désormais un **orchestrateur allégé** (≈ 800 → 460 lignes, complexité
cyclomatique 58 → 29) qui prépare le contexte, appelle trois composants extraits,
applique les gardes métier restantes (answerability, diversité, leg temporel), assemble
le résultat et gère audit + trace. Il ne réimplémente plus le détail des trois étapes.

```
QueryRequest → PreparedQuery + RetrievalPolicy
  → CandidateRetriever.retrieve_candidates() → CandidateSet
  → (gardes : diversité, leg temporel, should_answer)          [orchestrateur]
  → Reranker.rank_candidates()               → RankedCandidateSet
  → promote_same_page_visuals()              → voisins visuels de page
  → (enrich date, low-confidence)                              [orchestrateur]
  → PromptBuilder.build_prompt_stage()        → PromptBuildResult
  → QueryResult.to_legacy_dict()             → dict historique inchangé
```

| Composant | Entrée | Sortie | Effets de bord |
|---|---|---|---|
| `candidate_retriever.retrieve_candidates` | ctx (request/prepared/policy/trace) + `fts_search`/`vector_search` injectés | liste fusionnée ; `ctx.candidates` | repositories FTS/vecteur/corpus, trace des legs FTS/vector/business_corpus |
| `candidate_reranker.rank_candidates` | ctx + `chunks`/`selected` + `rerank_fn` injecté | `(selected, rerank_info)` ; `ctx.ranked` | reranker, trace du leg rerank |
| `prompt_builder.build_prompt_stage` | ctx + `selected` + `low_confidence`/`strict_refusal` | `PromptBuildResult` | aucun hors mémoire (pas de DB, pas de LLM) |

**Injection de dépendances** : les fonctions `search_chunks_hybrid`/`search_chunks_vector`
et `rerank_chunks_observed` sont passées depuis `orchestrator` (résolues à l'appel) —
les points de monkeypatch existants restent effectifs, sans coupler les composants à
l'orchestrateur.

**Invariants préservés** : scoping `rag_space_id` sur chaque repo ; **ordre du reranker
autoritaire** (aucun re-tri en aval) ; fusion déterministe ; politique d'erreur typée
P1-A1 inchangée ; trace P1-A0 (un seul builder, un leg par étape) ; prompt byte-identique.

## Limites / travaux reportés

- La préparation de requête et la résolution de politique restent inline dans
  `query_rag` (prep légère, sans logique lourde) : leur extraction est un raffinement
  possible ultérieur, non requis par ce lot.
- Le gel complet du holdout relève de **P1-C1**.
- Les captures des **parseurs d'ingestion** et les frontières non critiques ne sont pas
  traitées par ces lots (hors périmètre).

## Références d'implémentation

- [`backend/apps/rag_core/services/query/trace.py`](../../backend/apps/rag_core/services/query/trace.py)
- [`backend/apps/rag_core/services/query/errors.py`](../../backend/apps/rag_core/services/query/errors.py) (taxonomie P1-A1 + classifieur)
- [`backend/apps/rag_core/services/query/contracts.py`](../../backend/apps/rag_core/services/query/contracts.py) (DTO internes P1-B1)
- [`backend/apps/rag_core/services/query/candidate_retriever.py`](../../backend/apps/rag_core/services/query/candidate_retriever.py), [`candidate_reranker.py`](../../backend/apps/rag_core/services/query/candidate_reranker.py), [`prompt_builder.py`](../../backend/apps/rag_core/services/query/prompt_builder.py) (composants extraits P1-B2)
- [`backend/apps/rag_core/services/query/orchestrator.py`](../../backend/apps/rag_core/services/query/orchestrator.py) (`query_rag`, `_merge_bi_source`)
- [`backend/apps/chat/services/chat_service.py`](../../backend/apps/chat/services/chat_service.py) (`_query_rag_dispatch`, garde Excel) et [`chat_audit.py`](../../backend/apps/chat/services/chat_audit.py)
- [`backend/apps/corpus/services/retrieval.py`](../../backend/apps/corpus/services/retrieval.py) (`retrieve_corpus_candidates`, paramètre `recorder`)
- [`backend/apps/rag_core/core/facade.py`](../../backend/apps/rag_core/core/facade.py) et [`core/dto.py`](../../backend/apps/rag_core/core/dto.py) (`RetrievalResult.as_dict`)
- [`backend/apps/audit/models.py`](../../backend/apps/audit/models.py) (`RagQueryLog.retrieval`)
- `backend/apps/rag_core/benchmark/runner.py` (`CaseResult.retrieval`)
- Tests : `tests/tests_root/unit/query/test_retrieval_trace_types.py`, `tests/tests_root/integration/test_retrieval_trace.py`
