# Reranking

Objectif : décrire le reranking optionnel des candidats retrouvés (`services/rerank.py`) :
déclenchement, providers stub vs cross-encoder, pool/top_k et observabilité. Public :
développeurs backend, exploitants.

Le reranking s'insère **après** l'hybridation RRF et la diversification du retrieval, avant la
décision de réponse. Contexte : [../03-backend/pipeline-retrieval.md](../03-backend/pipeline-retrieval.md).

---

## 1. Statut et déclenchement

- **Désactivé par défaut** : `RAG_RERANK_ENABLED=0`.
- Quand activé, ne se déclenche que si le pool est assez large :
  `len(chunks) >= RAG_RERANK_TRIGGER_MIN_CANDIDATES=6`. En dessous, aucun reranking (l'ordre RRF
  est conservé).

---

## 2. Providers

`RAG_RERANK_PROVIDER=stub` par défaut.

| Provider | Mécanisme | Modèle | Notes |
|---|---|---|---|
| `stub` (défaut) | heuristique déterministe (bonus 0.05 par terme de la question présent) | aucun | pas de dépendance ML |
| `cross_encoder` | `sentence_transformers.CrossEncoder` | `RAG_RERANK_MODEL_NAME=BAAI/bge-reranker-base` | device `RAG_RERANK_DEVICE=cuda`, batch 16, singleton lazy |

Le provider `cross_encoder` est **activable par flag + provider**. En cas de dépendance ou CUDA
absente, il **retombe** sur le stub — repli tracé par `RerankInfo.fallback_used=True` et un
`logger.warning`.

**Seules ces deux valeurs sont acceptées** (casse et espaces indifférents). Toute autre valeur
— `local`, `crossencoder` mal orthographié… — est une **erreur de configuration** :

- le **system check `rag_core.E004` refuse le démarrage** quand `RAG_RERANK_ENABLED=1`
  (si le reranking est désactivé, la valeur est inerte et ne bloque pas) ;
- si le code est atteint malgré tout, le stub tourne mais le repli est **signalé**
  (`fallback_used=True`, `error` citant la valeur, `logger.warning`).

Le log de synthèse trace `requested_provider` **et** `effective_provider` : sans le premier,
un `provider=stub` serait indistinguable d'un stub légitimement configuré — c'était le défaut
d'observabilité de la dette D1.

---

## 3. Pool et top_k

- Pool de reranking : `RAG_RERANK_POOL_K` (défaut **20**).
- Garde de sortie : `RAG_RERANK_TOP_K` (défaut **4**).

---

## 3bis. L'ordre du reranker fait autorité jusqu'à la troncature

Un reranker ne sert à rien si son classement est réordonné en aval. C'est pourtant ce qui
se produisait : `clean_candidates(reranked)` était appelé entre le rerank et
`reranked[:rerank_take]`, et retriait par `score` — le score de **retrieval antérieur**.

Mesuré sur 66 cas du corpus Mission (stub, hybride actif) : le reranker était appelé
**66 fois sur 66**, et son ordre était **écrasé dans 49 cas (74 %)**. Il consommait du
temps — et un GPU en `cross_encoder` — pour un résultat jeté.

**Règle.** Après le rerank, seul `hygiene.dedupe_preserving_order` intervient : il retire
les invalides et les doublons **sans réordonner** (déduplication par première occurrence —
dans une liste rerankée, la première est la meilleure). Toute nouvelle étape de tri
postérieure doit prendre `rerank_score` comme clé primaire.

Ordre canonique après rerank :

```
rerank_score DESC → score retrieval DESC → document_id ASC → chunk_index ASC → chunk_id ASC
```

Les trois dernières clés viennent de [`domain/ranking.py`](../../backend/apps/rag_core/domain/ranking.py) :
elles garantissent que deux candidats au même `rerank_score` — cas fréquent avec le stub,
dont la grille est très discrète (`score + 0,05 × nb de termes`) — sont ordonnés de façon
déterministe.

**Les deux providers partagent ce contrat.** Le stub et le cross-encoder passent par le
même aval : corriger l'un sans l'autre n'aurait rien réglé.

Les scores restent distincts et lisibles dans l'audit : `score` (retrieval), `fusion_score`
(RRF) et `rerank_score` coexistent — aucun n'écrase l'autre.

---

## 4. Observabilité

Un objet `RerankInfo` est persisté dans l'audit (`RagQueryLog.rerank`) : provider utilisé,
`fallback_used`, tailles de pool. Labels de statut possibles :

- **désactivé** (`RAG_RERANK_ENABLED=0`) — cas nominal.
- **non déclenché** — activé mais pool < 6 candidats.
- **appliqué (stub)** / **appliqué (cross_encoder)**.
- **repli stub** — cross_encoder demandé, dépendance/CUDA absente → stub.

Deux journaux complètent l'audit, côté orchestrateur :

- `retrieval.rerank applied pool=… rerank_out=… post_clean=… take=…` — le cas nominal ;
- `retrieval.rerank fallback: aucun candidat après nettoyage …` — **avertissement**. Le
  rerank n'a rien rendu d'exploitable et l'ordre retrieval est conservé
  (`reranked[:take] or selected`). Ce repli existait déjà mais était **muet** : c'est lui
  qui a masqué le défaut du §3bis, où le nettoyage anéantissait tout le pool. Un
  avertissement ici signale un rerank qui ne sert à rien.

Voir l'audit de requête dans [politiques-llm.md](politiques-llm.md) §7 et l'API
[../05-api/reference-endpoints.md](../05-api/reference-endpoints.md).

---

## 6. Reranking de passages via l'AI Gateway — *en service, staging et production*

Chemin **distinct** du reranking local décrit ci-dessus : aucun modèle n'est chargé dans le
backend RagATM. Le pool admis est soumis à l'**AI Gateway** (`POST /v1/rerank`), qui sert un
cross-encoder multilingue.

- **Désactivé par défaut partout** : `RAG_RERANKER_ENABLED=0`. Une production qui
  déploierait ce code sans poser la variable n'active rien.
- ⚠️ **En service. Latence qualifiée le 22/08/2026, qualité toujours non qualifiée.**
  Le drapeau vaut `1` en staging **et en production**. Les compteurs d'exploitation relevés ce jour-là :
  staging 946 appels / 944 succès / 18 replis / 1 dépassement de délai ; production
  29 appels / 29 succès / 0 repli. Le **coût de latence** a été mesuré et **accepté**
  (221,9 ms au p95 en production à un flux, sous la porte de 250 ms) — voir
  la documentation d'exploitation (hors périmètre de cette édition).
  Le **recall reste non mesuré** : le chiffre historique (60/113) vient du même banc
  invalide que l'ancienne objection de latence. L'identité de la
  configuration en service est préenregistrée dans
  `backend/benchmarks/calibration/retrieval/target-pipeline-prereg-{staging,production}.json`.
- **Adresse obligatoire quand le drapeau est actif** : `RAG_RERANKER_ENABLED=1` avec
  `RAG_RERANKER_BASE_URL` vide est refusé au démarrage (`rag_core.E009`). Sous
  `fail_open_rrf`, cette configuration rendrait l'ordre RRF à chaque requête — une
  baseline servie sous l'étiquette « rerankée ».
- **Exclusif du reranking local** : `RAG_RERANK_ENABLED` et `RAG_RERANKER_ENABLED`
  ne peuvent pas être actifs en même temps — le **system check `rag_core.E008`
  refuse le démarrage**. Activés ensemble, la chaîne deviendrait *fusion RRF →
  reranker Gateway → diversification → reranker historique → sélection* : le second
  réécrirait le classement du premier, celui qui fait autorité par contrat. Le coût
  n'est pas une panne, c'est une **mesure fausse** — une campagne croirait qualifier
  la Gateway et mesurerait la composition des deux. Aucun cas d'exploitation ne
  demande cette composition.
- **Un seul appel HTTP par question**, jamais un par passage.
- **Découplage** : RagATM ne connaît que le contrat de la Gateway et un nom **logique**
  (`rerank-default`). Ni dépôt Hugging Face, ni révision, ni implémentation ou port du
  backend n'apparaissent dans le code produit — un test l'interdit en inspectant la source.
- **Tri pur par pertinence** : l'ordre RRF n'est pas mélangé au score de pertinence. Le
  mélange a été mesuré comme strictement moins bon, et de façon monotone.

### Qualité du signal — mesurée le 04/09/2026 (*staging, 38 cas*)

Première mesure du **score** lui-même, et non seulement de sa latence. Question posée :
le score cross-encoder sépare-t-il le passage attendu des autres passages du pool ?

| | |
|---|---|
| ROC-AUC gold / non-gold | **0,9918** |
| PR-AUC (base aléatoire 0,0250) | **0,7468** |
| Recouvrement des distributions (40 bacs) | **0,051** |
| Passage attendu en rang 1 | **22/38 avant rerank → 33/38 après** |
| Passage attendu dans le top 5 | **38/38** |

**Ce que cela qualifie** : l'**ordonnancement à l'intérieur d'une question**. Le reranker
améliore le placement du passage attendu — +11 cas passent en tête, 14 remontent, 2 descendent.

**Ce que cela ne qualifie pas** :

- **le score absolu**. Il n'est pas calibré d'une question à l'autre : le passage attendu
  descend jusqu'à −0,49 (tout en restant rang 2) quand un passage non attendu monte à 6,65.
  Aucun seuil absolu ne sépare proprement. Toute règle d'admission bâtie sur ce score devrait
  être **relative** (marge, rang) — mais aucune n'a été conçue ni essayée dans ce lot.
- **l'abstention**. Les 38 cas sont tous des questions **dont la réponse existe**. La
  capacité du reranker à reconnaître qu'aucun passage ne convient reste **non mesurée**.
- **le recall du pipeline**. Sur ces 39 cas, 38 passages attendus étaient présents dans le
  pool de 40 ; le 39ᵉ n'y est jamais entré — défaut de rappel amont, pas du reranker.

Artefacts, méthode et limites : `backend/benchmarks/calibration/passage-relevance-signal-feasibility/`
(sceau schéma v3, 1 520 paires, métriques recalculables par script).

### Abstention — le signal au niveau QUESTION (*04/09/2026, staging, 73 questions*)

La section précédente mesure l'ordonnancement **dans** une question. Celle-ci répond à
l'autre question : le reranker voit-il qu'une question n'a **aucun** bon passage ?

38 questions répondables contre 35 questions dont l'absence de réponse a été vérifiée
(8 catégories, split calibration du dataset v4).

| signal | ROC-AUC | recouvrement |
|---|---:|---:|
| **`top1_score`** — score brut du meilleur passage | **0,9812** | **0,055** |
| `top1 − médiane(top-k)` | 0,9316 | 0,213 |
| `marge top1 − top2` | 0,8662 | 0,347 |

Médiane du `top1_score` : **+4,03** pour une question répondable, **−2,32** pour une question
sans réponse — plus de six logits d'écart.

**Contre-intuitif, et vérifié** : c'est le score **absolu** qui sépare le mieux, pas la
structure relative. La marge top1−top2 fait six fois pire en recouvrement, et elle *induit en
erreur* sur les questions hors-corpus : un top1 très détaché peut simplement signifier que
tout le reste est encore plus hors-sujet.

**La limite est nette et d'un seul type.** Sept catégories sur huit se séparent parfaitement
(`out_of_corpus`, `unsatisfiable`, `ambiguous_entity`, `place_org_confusion`,
`absent_temporality`, `lexical_lure`). **`false_premise` résiste** — ROC-AUC 0,8737,
recouvrement 0,184, médiane du top1 positive. Un cross-encoder note la pertinence
**thématique** ; il n'a aucun moyen de vérifier qu'une prémisse est vraie. Les négatifs qui
montent le plus haut sont des questions dont le sujet est bien dans le corpus et dont la
fausseté tient à un quantificateur : « toujours », « consistently », « complètement résolu ».
`too_vague` n'a qu'un cas : catégorie non couverte.

**Aucun seuil n'a été cherché, aucune règle d'abstention n'existe.** Artefacts, méthode et
limites : `backend/benchmarks/calibration/question-level-negative-relevance-feasibility/`.

#### Validé sur données indépendantes — *holdout v4, 04/09/2026*

Ces chiffres venaient d'un jeu de **calibration**, celui-là même qui avait servi à choisir le
signal. Ils ont été rejoués une fois sur le **holdout v4** — 174 questions scellées le
05/08/2026, jamais ouvertes, jamais utilisées pour sélectionner quoi que ce soit.

| | calibration | **holdout** |
|---|---:|---:|
| ROC-AUC `top1_score` | 0,9812 | **0,9714** |
| recouvrement | 0,055 | **0,175** |
| écart des médianes | 6,35 | **6,11** |

**Le signal survit.** Les quatre critères pré-enregistrés passent, et **aucune des huit
catégories négatives ne s'effondre** — la plus faible tient à 0,935 sur 4 cas.

Trois résultats changent la lecture de la section précédente :

- **`false_premise` s'est atténué** : 0,8737 sur 5 cas devient **0,9545 sur 12**. La valeur
  de calibration était bruitée. Le mécanisme reste vrai — thème contre vérité — mais la
  catégorie n'est plus le point noir qu'on croyait ;
- **`too_vague` est exploitable** : **0,9488 sur 18 cas**, contre un seul cas en calibration.
  L'inconnue est levée ;
- **le point faible s'est déplacé.** Le recouvrement triple non parce que les négatifs
  montent — leur maximum baisse — mais parce que **22 % des positifs** tombent sous le
  meilleur négatif. Les questions de **dénombrement** (« combien… ») sont particulièrement
  mal notées : le passage contient les éléments sans ressembler à une réponse.

**Le holdout v4 est désormais consommé** et ne peut plus servir. Tout seuil futur devra être
validé sur des données à créer. Artefacts :
`backend/benchmarks/calibration/holdout-v4-one-shot-validation/`.

### Invariant d'ordre du pool

Après la fusion, l'étape d'hygiène **re-trie** le pool par `score` brut : la liste `chunks`
n'est **pas** dans l'ordre RRF. Un `chunks[:N]` naïf soumettrait donc un autre sous-ensemble
— défaut mesuré en staging : 2 questions sur 63 recevaient le bon pool et la couverture
tombait *sous* la baseline.

`services/query/diversify.py` exporte pour cela `rrf_sort_key`, la clé canonique, et
`rerank_gateway.select_rrf_pool` l'impose. C'est le seul chemin d'accès au pool.

### Plafond de concurrence — la contrainte dimensionnante

La Gateway **sérialise** le reranking : la latence de l'appel est proportionnelle au
nombre de requêtes simultanées. Mesuré le 22/08/2026 (pool 40) :

| flux simultanés | 1 | 2 | 4 | 8 |
|---|---:|---:|---:|---:|
| p95 de l'appel (ms) | 147 | **261** | 524 | 1 063 |

- **la porte de 250 ms est franchie dès DEUX requêtes simultanées** ;
- débit plafond **≈ 7,7 reranks/s** (pool 40), **partagé entre staging et production**
  qui tournent sur le même hôte ;
- le timeout client de 2 s est atteint vers **15 requêtes simultanées** ; au-delà, sous
  `fail_open_rrf`, la réponse est une **baseline servie sous l'étiquette « rerankée »**.

Le coût est linéaire en taille de pool (~3,2 ms par document) : `RAG_RERANKER_POOL_K=20`
le divise par deux. L'effet de cette réduction sur la **qualité** n'est pas mesuré.

Reproduire : `python manage.py bench_rerank_latency --appels 200 --concurrence 2`.

### Politique d'échec

`fail_open_rrf` par défaut : si la Gateway est indisponible, l'ordre **RRF canonique** est
conservé et la requête aboutit. Le repli n'est **jamais silencieux** —
`rag_reranker_fallback_total`, journal `WARNING`, et `info.fallback` dans la trace. La
variante `fail_closed` fait remonter l'erreur ; elle sert aux qualifications, où un repli
mélangerait baseline et reranking dans les mêmes chiffres.

> Le fail-open n'est sûr **que si le repli est visible**. La surface de lecture externe est
> décrite dans la documentation d'exploitation (hors périmètre de cette édition)
> § « Compteurs d'exploitation ».

### Plafond documentaire

`RAG_RERANKER_MAX_PER_GROUP=3` s'applique **uniquement quand le reranking réussit**. Drapeau
éteint, repli ou échec : le plafond historique est intact. Relâcher le plafond n'est utile
que si ce qu'il laisse entrer est bien classé.

### Deux chemins, deux traces nommées, deux legs

Dans `query_rag`, la trace du reranking Gateway (`gateway_rerank_info`, un `dict`)
et celle du reranking historique (`legacy_rerank_info`, un `RerankInfo`) portaient
le **même nom** dans la même portée : la seconde affectation écrasait la première.
Le nom dit désormais lequel des deux chemins il décrit.

`RagQueryLog.rerank` reste réservé au chemin **historique** — des lecteurs y attendent
sa forme (`n_in`, `n_out`, `effective_provider`). Le chemin Gateway est tracé dans le
leg `rerank_gateway` de `RagQueryLog.retrieval` : état, cause d'échec, repli, durée,
modèle, et **mouvements de rang** RRF → reranking. Voir
[../03-backend/retrieval-observabilite.md](../03-backend/retrieval-observabilite.md).

> Jusqu'au 22/08/2026, ce leg n'existait pas et l'appel qui aurait dû l'écrire était
> neutralisé par un `hasattr` portant sur une méthode absente. L'audit affirmait
> `rerank: not_requested` sur des requêtes réellement rerankées — les 29 appels de
> production ne laissaient **aucune** trace exploitable.

### Observabilité et confidentialité

Compteurs à cardinalité bornée (`attempted`, `success`, `failure`, `fallback`, `timeout`,
`invalid_response`, `documents_total`, `duration_ms_total`), lisibles **hors du processus**
sur `/ops/metrics`. **Jamais** de texte de question, de passage ou d'extrait dans les
journaux ni dans l'export.

### Gouvernance de configuration

Les sept réglages sont déclarés au [registre](../10-reference/configuration.md) et
gouvernés par le manifeste de
parité staging ↔ production :

| Variable | Catégorie | Parité |
|---|---|---|
| `RAG_RERANKER_ENABLED` | divergence de qualification | autorisée et **documentée** (staging qualifie, production attend) |
| `RAG_RERANKER_BASE_URL` | topologie d'environnement | autorisée (instance et port distincts) |
| `RAG_RERANKER_MODEL_LOGICAL` | identité de pipeline | exigée identique |
| `RAG_RERANKER_POOL_K` | identité de pipeline | exigée identique |
| `RAG_RERANKER_MAX_PER_GROUP` | identité de pipeline | exigée identique |
| `RAG_RERANKER_FAILURE_POLICY` | conduite en cas de panne | exigée identique |
| `RAG_RERANKER_TIMEOUT_SECONDS` | budget à effet fonctionnel | exigée identique |

### Provenance

`rerank_gateway.provenance()` lit par HTTP l'environnement, le SHA de la Gateway, le modèle,
sa révision et la santé du reranking — sans SSH. La fonction ne lève jamais : une métadonnée
absente ne doit pas coûter une requête utilisateur.

---

## Références d'implémentation

- `backend/apps/rag_core/services/rerank.py`
- `backend/apps/rag_core/checks_rerank.py` (`rag_core.E004`, `rag_core.E008`, `rag_core.E009`)
- `backend/apps/rag_core/services/rerank_gateway.py` (chemin AI Gateway)
- `backend/apps/rag_core/services/ops_counters.py` (compteurs partagés entre workers)
- `backend/config/urls.py` (`/ops/metrics`)
- `backend/apps/rag_core/services/query/diversify.py` (`rrf_sort_key`)
- `backend/apps/rag_core/services/query/orchestrator.py`
- `backend/apps/rag_core/domain/policies.py`
- `backend/apps/audit/models.py` (`RagQueryLog.rerank`)
- `backend/apps/rag_core/management/commands/bench_rerank_latency.py`
- `backend/config/settings/base.py`
