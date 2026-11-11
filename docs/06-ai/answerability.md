# Answerability — décider de répondre ou non

Objectif : clarifier la distinction critique entre le mécanisme **live** de décision « répondre ou
non » (seuil de score) et le module **déterministe** d'answerability P1.8 (benchmark uniquement,
non branché). Public : développeurs backend, exploitants, contributeurs benchmark.

C'est la distinction la plus souvent confondue du projet. Il existe **deux mécanismes séparés**,
plus une garde déterministe additionnelle (Excel). Un seul est exécuté en production.

---

## 1. Mécanisme LIVE (production) — seuil de score `should_answer`

**Statut : implémenté, activé.** C'est le SEUL mécanisme exécuté sur le chemin chat.

Défini dans `apps/rag_core/domain/policies.py`, appelé par l'orchestrateur de retrieval
(`services/query/orchestrator.py::query_rag`).

`RagPolicy` construit depuis les settings :

| Setting | Défaut | Rôle |
|---|---|---|
| `RAG_MIN_SOURCES` | `1` | nombre minimal de chunks |
| `RAG_MIN_TEXT_RANK` | `0.0` | seuil lexical (ts_rank) |
| `RAG_MIN_SCORE` | `0.20` | seuil vectoriel / score générique |

`should_answer(chunks, policy)` (strict) évalue, par priorité : (1) `len(chunks) >= min_sources` ;
puis si `best_text > 0` → `best_text >= min_text_rank` ; sinon si `best_vec > 0` →
`best_vec >= min_score` ; sinon `best_any >= min_score`. Le mode explicatif abaisse ces seuils
(`RAG_EXPLAIN_MIN_*`).

### Refus dur uniquement si le pool est VIDE

- **No-answer réel** (`status=no_answer`, « Je ne sais pas. ») **uniquement si**
  `strict_refusal AND not chunks` — c'est-à-dire **aucun chunk**. Côté chat : message fixe
  « Pas assez d'informations dans vos documents. », **aucun appel LLM**.
- Si `should_answer=False` mais qu'il **reste des chunks** → on ne refuse **pas** : la génération a
  lieu en mode **`partial`** (préambule « réponds avec ce qui est disponible, indique les limites »).
- **`low_confidence`** (`query/lowconf.py`, `RAG_LOWCONF_ENABLED=True`) : `best_score <
  min_score * RAG_LOWCONF_FACTOR(1.0)` et ≥ `LOWCONF_MIN_SOURCES(1)` → préambule prudent.

Labels de policy renvoyés : `ok | low_confidence | partial | explain | compare` (+ `no_answer`,
`no_rag_space`, `error`). La génération LLM finale est faite **en aval** de `query_rag` par le chat,
à partir du `prompt` retourné (voir [../03-backend/chat-generation.md](../03-backend/chat-generation.md)
et [politiques-llm.md](politiques-llm.md)).

### 1bis. Sur-abstention : le refus qui n'était PAS une décision d'answerability

**Statut : implémenté, activé par défaut.**

Un troisième cas existe, qu'aucun des mécanismes ci-dessus ne couvre : le **modèle** s'abstient
alors que la politique, elle, avait décidé de répondre (`status=ok`, sources transmises). Mesuré
en dev le 25/07/2026 : « Quelles sont les principales différences entre les architectures
de reporting de Tessaly et de Valteria ? » → `policy=ok`, 8 sources, 1 644 ms, réponse « Je ne sais pas. ».
Le même fonds, le même modèle et la même journée répondaient en 10 points structurés à une
question `policy=explain`. La cause n'était donc ni le corpus ni le retrieval : **la question
comparative recevait le gabarit factuel** (« réponse courte et directe », « si tu n'es pas sûr :
Je ne sais pas »).

> Depuis le 30/07/2026, cette consigne de concision absolue **n'existe plus** : la consigne de
> format du gabarit par défaut est devenue adaptative (cf.
> [politiques-llm.md §3](politiques-llm.md)). Le premier correctif ci-dessous (politique `compare`)
> reste néanmoins nécessaire : il apporte la **structure** comparative, que la seule consigne de
> longueur ne fournit pas.

Deux correctifs, indépendants :

1. **Politique `compare`** — `detect_comparative_intent` (`services/query/heuristics.py`) exige
   un **signal comparatif fort** : une intention comparative explicite **ET** deux objets
   coordonnés identifiables. Elle déclenche le préambule comparatif
   (`RAG_COMPARE_PROMPT_PREAMBLE`, défaut `DEFAULT_COMPARE_PREAMBLE`) : dimensions réellement
   couvertes, comparaison point par point, ressemblances **vs** différences, ce qui manque dit
   explicitement, **jamais** de comparaison inventée quand une seule entité est documentée.
   **Précédence** : `low_confidence` > `partial` > `compare` > `explain` — l'état des *preuves*
   prime toujours sur la *forme* de la question. Voir
   [politique comparative](politique-comparative.md) pour les motifs, la doctrine des cas
   limites et l'expansion déterministe de requêtes.
2. **Contrat de réponse** — une abstention ne revendique plus aucune citation (voir
   [politiques-llm.md §6](politiques-llm.md) et
   `apps/rag_core/services/answer_contract.py`). C'est ce qui rend la sur-abstention
   **observable** au lieu d'être maquillée en « 8 sources citées ✓ ».

Ce que ces correctifs ne font **pas** : relancer la génération. Une relance automatique après
abstention est **évaluée, non implémentée** — cf.
[relance-anti-sur-abstention.md](relance-anti-sur-abstention.md).

La politique `compare` **ne contourne aucune garde** : la garde analytique Excel (§3) neutralise
le prompt avant tout appel LLM, y compris sur une question comparative.

---

## 2. Module answerability déterministe P1.8 — benchmark uniquement

**Statut : implémenté, benchmarké, NON branché au chemin live** (outil d'évaluation offline).

Défini dans `apps/rag_core/benchmark/answerability.py`. Son seul usage hors de son module est la
commande `manage.py rag_benchmark` (`--answerability-strategy`) et `benchmark/runner.py`. Vérifié :
aucune référence dans `orchestrator.py`, `chat_service.py`, ni `core/facade.py`. **Il n'intervient
pas dans la réponse utilisateur.**

Principe : `pertinence retrieval ≠ answerability`. À partir de (question, chunks texte+score), il
décide si les preuves suffisent — déterministe, explicable, sérialisable, **zéro appel LLM**.

### Signaux (`evidence_signals`, sur le top-k, défaut 5)

- `retrieval_score` (meilleur score), `score_margin` (écart top1-top2).
- **weighted_coverage** (signal central) : couverture de preuve pondérée par l'**IDF**. Termes de
  contenu = tokens hors stopwords, len≥3. Poids IDF lissé borné [1.0, 5.0]. Couverture UNION (un
  terme couvert s'il apparaît dans n'importe quel chunk, sous-chaîne ou préfixe ≥6 car.).
  `weighted_coverage = Σ IDF(couverts) / Σ IDF(tous)`.
- `lexical_overlap` = n_couverts / n_termes.
- **structured_value** : si la question demande un type de valeur (ipv4/port/version/file/ref), la
  valeur doit co-occurrer dans **un** chunk avec le terme entité le plus discriminant couvert.
- **identifiers_covered** : identifiants forts de la question (REF-2026-04, Series A-1, ports,
  `config.yaml`, migrations…) tous présents dans les preuves.

### Décision (`decide`), 4 stratégies

| Stratégie | Règle |
|---|---|
| `none` | toujours answerable (baseline) |
| `retrieval_threshold` | `retrieval_score >= min_score` (défaut 0.76) — baseline « seuil seul » |
| `evidence_rules` | preset `strict/balanced/permissive` (défaut **balanced** : score_floor=0.60, coverage_min=0.60), ordre de refus déterministe |
| `composite` | score pondéré `retrieval 0.20 + coverage 0.45 + value 0.20 + identifier 0.15`, seuil défaut 0.5 (CLI 0.70) |

Sortie `AnswerabilityDecision{answerable, confidence, reason, signals}` sérialisable (version `"1"`).
`build_corpus_df` construit la document-frequency pour l'IDF, alimentée par le benchmark, **pas**
par le live.

### Résultats et statut

Prêt à brancher mais **pas encore câblé** au chemin de réponse. Benchmarké (P1.6–P1.8) : la
précision no-answer passe de **0.13** (baseline seuil seul) à **0.63** (evidence/composite), mais
reste **sous les cibles de promotion** — d'où le maintien hors chemin live. Détails et cibles :
../08-testing/benchmark-rag.md.

---

## 3. Garde analytique Excel — refus déterministe séparé

Mécanisme de refus **distinct** des deux précédents (`apps/chat/services/excel_guard.py`). Sur une
question analytique portant sur un classeur (somme, total, liste exhaustive), le LLM ne verrait que
le top-k, pas le classeur entier : la garde **neutralise le prompt** (`prompt=None`) et refuse
**avant** le LLM. Deux points d'application : `guard_for_request` (avant retrieval) et
`guard_after_retrieval` (si un classeur est retrouvé — point unique partagé sync/stream). Audit
léger via `emit_action_event` (`action.blocked`).

### Intentions bloquantes

`exhaustive_aggregation`, `exhaustive_count`, `exhaustive_enumeration`, `ranking`, `grouping`,
`global_comparison`, **`local_comparison`**.

`local_comparison` a été ajouté à l'ensemble bloquant (correctif **M1**) : l'intention existait
mais ne bloquait rien, si bien qu'une question comme « Compare le total 2025 et le total 2024 dans
le tableur » retombait **en silence** vers le RAG vectoriel — lequel ne voit que le top-k. Deux
familles de formulations sont désormais interceptées :

| Famille | Exemple | Intention |
|---|---|---|
| Comparaison explicite | « Compare le total 2025 et le total 2024 » | `local_comparison` |
| Comparaison implicite | « Le total 2025 dépasse-t-il celui de 2024 ? » | `local_comparison` |
| Delta / variation | « Quelle est l'évolution du CA entre 2024 et 2025 ? » | `exhaustive_aggregation` |

Une comparaison reste **autorisée** si un marqueur de lecture la rachète (« compare le montant de
**la ligne** Atlas et celui de **la ligne** Orion », « la **cellule** B12 ») : elle ne porte alors
que sur des faits retrouvés, pas sur un calcul.

---

## 3bis. Policy d'admission par le score du reranker — *implémentée en mode FANTÔME*

État : **DESIGN CANDIDATE**, câblée en **mode fantôme**. Elle calcule et se journalise ;
**aucune décision n'atteint l'utilisateur**.

| | |
|---|---|
| drapeau | `RAG_ADMISSION_SHADOW_ENABLED` — **`false` par défaut, OFF partout** |
| module | `apps.rag_core.domain.admission_shadow` (pur : ni Django, ni base, ni réseau) |
| trace | `retrieval_trace.admission_shadow`, dans la réponse et dans le résumé persisté |
| coût | **2,25 µs au p95** — la policy relit le score du reranking, sans aucun appel modèle |

**Avec le drapeau ON ou OFF, la réponse servie est identique.** Ce n'est pas une intention :
un test compare les deux exécutions champ par champ — statut, sources, rangs, scores, prompt,
trace hors `admission_shadow` — avec un reranker déterministe, et vérifie qu'il n'est appelé
qu'une fois de part et d'autre.

**Ce qui n'est pas évaluable est nommé, jamais rabattu sur une décision.** Le reranker tourne
en `fail_open_rrf` : un repli rend l'ordre RRF, dont les scores ne sont pas des logits
cross-encoder. Ces cas rendent `not_evaluated` avec un motif (`reranker_fallback`,
`reranker_unavailable`, `no_candidates`, `no_top1_score`). Fabriquer un `top1_score` serait
pire qu'une absence de décision.

Rejeu staging (133 cas, development + calibration, 0 erreur) : `ALLOW` 51,9 % · `ABSTAIN`
27,8 % · `UNCERTAIN` **20,3 %** — conforme aux 19–22 % prévus. Sur les positifs : 94,2 %
`ALLOW`, **0 % `ABSTAIN`**, 5,8 % `UNCERTAIN`. Sur les négatifs : 57,8 % `ABSTAIN`, 6,2 %
`ALLOW`.

> **Asymétrie de langue à élucider.** La zone `UNCERTAIN` vaut **29,4 % en français** contre
> **10,8 % en anglais**, et les quatre positifs incertains du rejeu sont tous des questions
> FR. Non anticipé par la conception, qui ne stratifiait pas par langue.

Artefacts : `backend/benchmarks/calibration/admission-shadow-balanced-v1/`.

### Le jeu qui a décidé de l'activation — *scellé, immuable, consommé*

Le taux de faux rejets réel de `balanced-v1` reste **inconnu** : les jeux de conception
n'en contiennent aucun cas limite (leur positif le plus bas est à +1,96, quand le holdout
v4 en portait un à **−4,65**). C'est ce que mesurera le jeu de cas difficiles, scellé le
05/09/2026 :

| | |
|---|---|
| fichier | `backend/benchmarks/calibration/admission-hard-case-dataset/sealed-set.jsonl` |
| empreinte | **`83189d9e74597c99…`** — celle du scellement, inchangée depuis |
| état | **scellé · immuable · consommé** le 05/09/2026 |
| où se lit l'état | `consumption-record.json` — registre externe, append-only |
| taille | **186 cas** — 148 positifs difficiles, 38 négatifs |
| adjudication | 273 verdicts humains appliqués par `review_admission_dataset --apply` |

`balanced-v1` **ne l'a jamais vu** : aucun `top1_score` n'est intervenu dans sa
constitution, ni pour choisir un cas, ni pour trancher le plafond de 5 cas par document.
Il remplace le holdout v4 consommé et **ne s'ouvre qu'une fois**.

Le run est **pré-enregistré** depuis le 05/09/2026 —
`backend/benchmarks/calibration/balanced-v1-one-shot-prereg/`. Y sont figés, avant tout
score : la règle de décision, les six critères bloquants, l'ordre de passage et la
conduite à tenir en cas de panne.

| critère bloquant | seuil | tolérance |
|---|---|---|
| **S1** faux rejets (positifs), Wilson sup. unilatérale 95 % | ≤ 5 % | **au plus 3** sur 148 |
| **S2** faux accepts (négatifs), taux observé | ≤ 15 % | au plus 5 sur 38 |
| **U1** zone `UNCERTAIN` | ≤ 35 % | — |
| **L1** faux rejets par langue · **F1** par famille | ≤ 5 % | 2 langues et 4 familles jugées |
| **T0** validité technique | 186/186 évaluables, aucun repli RRF | — |

`criteria.py` est l'autorité exécutable : les seuils y sont écrits, la borne de Wilson
implémentée, et des tests vérifient qu'elle ne diverge pas du domaine. **ROC-AUC et toute
recherche de seuil ne participent pas au verdict** — les seuils sont gelés.

### Résultat du run one-shot — **NO-GO**, le 05/09/2026

> **`balanced-v1` n'est pas validée.** Le run est techniquement irréprochable (T0 PASS,
> 186/186 évaluables, aucun repli), et **les cinq critères qualité échouent**.

| critère | mesuré | seuil | statut |
|---|---:|---:|---|
| S1 faux rejets (positifs) | **14,86 %** (22/148), Wilson 0,2030 | ≤ 5 % | **FAIL** |
| S2 faux accepts (négatifs) | **26,32 %** (10/38) | ≤ 15 % | **FAIL** |
| U1 zone `UNCERTAIN` | **41,40 %** | ≤ 35 % | **FAIL** |
| L1 par langue | `fr` **21,74 %** · `en` **8,86 %** | ≤ 5 % | **FAIL** |
| F1 par famille | `counting` **27,78 %** · `cross_lingual` **18,84 %** | ≤ 5 % | **FAIL** |

Trois enseignements, tous mesurés :

- **Le signal vaut 0,63, pas 0,97.** ROC-AUC 0,6261 sur des positifs difficiles, contre
  0,9714 sur le holdout v4. Les mesures antérieures n'étaient pas fausses : leurs positifs
  étaient faciles.
- **Le dénombrement s'effondre.** `counting` : médiane de score −0,025, plus d'un quart des
  questions « combien… » légitimes rejetées. Une campagne antérieure l'avait annoncé sur dix cas ; soixante-douze
  le confirment. À l'inverse `aggregation` (2,5 %) et `multi_passage` (0 %) passent.
- **La langue de la question pèse un facteur 2,5.** Et l'hypothèse directionnelle lors de la qualification
  est **infirmée** : `fr→en` est la meilleure direction (7,4 %), `en→fr` la pire (26,2 %).

Aucun seuil n'a été déplacé, aucun cas écarté. Le jeu est **consommé** depuis le
05/09/2026 et ne sera pas rejoué. Toute hypothèse issue de ces observations devra être
évaluée sur un autre jeu, construit et scellé de la même façon.

#### Où se lit l'état d'un jeu scellé

Un jeu scellé est **immuable octet pour octet** : son empreinte est la preuve, et une
preuve qui bouge n'en est plus une. L'état du cycle de vie — consommé ou non, par quel
run, avec quel résultat — n'appartient donc pas au jeu mais à un **registre externe
append-only**, `consumption-record.json`.

L'en-tête de `sealed-set.jsonl` porte encore `CONSUMED: false` : c'est la **photographie
de l'instant du scellement**, conservée telle quelle, et non l'état courant. Trois tests
verrouillent la convention — l'empreinte attendue, la cohérence du registre, et un test
négatif qui échoue si le fichier scellé est modifié.

Artefacts : `backend/benchmarks/calibration/balanced-v1-one-shot-validation/`.

### Pourquoi elle a échoué — *diagnostic, 05/09/2026*

Le diagnostic post-hoc ne rejoue rien : il relit les résultats persistés, les
métadonnées du jeu et les passages source. Il ne propose aucun seuil.

**Les deux modes d'erreur sont expliqués, et deux hypothèses tombent.**

- **20 des 22 faux rejets sont des comptages** — mais ce n'est pas le comptage qui
  échoue. Ce qui sépare les comptages rejetés des comptages acceptés est la
  **minceur lexicale de la question** : chevauchement médian avec le support
  **0,083** contre **0,600**. La séparation se fait *à l'intérieur* de la famille
  (40,0 % contre 12,5 % de faux rejets, p = 0,016).
- **L'hypothèse du multi-passage est réfutée** : `multi_passage` exige **deux**
  supports et produit **0 %** de faux rejet, quand `counting` n'en exige qu'un et
  en produit 27,8 %. L'hypothèse du calcul tombe aussi : `aggregation` demande une
  opération arithmétique et passe à 97,5 %.
- **L'effet français n'existe pas.** Il disparaît dès qu'on contrôle la famille :
  p = 0,694 en comptage monolingue, p = 1,000 hors comptage. L'écart brut
  (p = 0,037) venait de la composition — 55,1 % des positifs FR sont des comptages
  contre 43,0 % des EN.
- **Les 10 faux accepts sont expliqués** : 7 prémisses fausses dont la fausseté
  tient à un quantificateur absolu (« a **déjà** été **entièrement**… »), et
  3 questions trop vagues. Le score suit le vocabulaire partagé — ρ = **+0,813**
  sur les prémisses fausses.
- **Le test de bruit lors de la qualification ne prouvait rien** : perturbation médiane **0,0000**
  pour une distance médiane au seuil de **1,543**. Ces 18 paires ne doivent pas
  être citées comme preuve de robustesse.

**Ce que `top1_score` mesure**, au terme du diagnostic : ni la pertinence
thématique, ni la suffisance de preuve, mais **l'appariement question–passage**.
Il monte avec le vocabulaire partagé même quand la proposition est fausse,
s'effondre quand la question est courte même quand la preuve est complète, et
ignore le nombre de passages nécessaires. Le défaut de fond est d'avoir pris une
mesure d'**ordre** pour une mesure **absolue** : un score de reranking n'a pas de
zéro, et rien ne garantit qu'une même valeur signifie la même chose pour une
question de trois mots et pour une question de trente.

**Ce que le diagnostic ne peut pas établir.** Une version antérieure n'a persisté qu'un scalaire
par cas — ni candidats, ni rangs, ni scores intermédiaires — et n'a laissé aucune
ligne `rag_query_log`. La part respective d'un défaut de **rappel** et d'un défaut
d'**ordonnancement**, la contribution du **corpus métier** et la responsabilité
propre du **reranker** restent **non prouvables**, définitivement : le jeu est
consommé. Tout run futur doit **persister la trace des candidats**.

Artefacts : `backend/benchmarks/calibration/admission-failure-diagnosis/`.

Après quatre campagnes (`passage-relevance` → `question-level-negative-relevance` →
`holdout-v4-one-shot-validation` → `admission-policy-design`), le signal
**`top1_score`** — score cross-encoder du meilleur passage, au niveau QUESTION — a été validé
une fois sur un holdout scellé (ROC-AUC 0,9714) et sert de base à une policy à trois états.

### La policy candidate

| | |
|---|---|
| **`ABSTAIN`** | `top1_score ≤ −1,92` |
| **`ALLOW`** | `top1_score ≥ +1,60` |
| **`UNCERTAIN`** | entre les deux — comportement **non décidé** |

Mesures (development = ajustement, calibration = contrôle faible) : **0 faux rejet**,
faux accepts 5,7–6,9 %, **précision d'abstention 1,000**, rappel d'abstention 0,48–0,63,
zone `UNCERTAIN` 19–22 %. Insensible à ±0,25 logit, soit 25 fois le bruit du reranker.

### Trois choses à ne pas oublier en la lisant

- **Les seuils sont ancrés sur la distribution des NÉGATIFS, pas des positifs.** La queue
  basse des positifs n'est pas stable d'un jeu à l'autre — minimum +1,96 / −0,02 / **−4,65**
  selon le split — alors que les négatifs le sont. Ancrer sur les positifs aurait produit un
  seuil qui rejette massivement ailleurs.
- **Le « 0 faux rejet » est un artefact des jeux disponibles**, qui ne contiennent pas de
  positifs faibles. Sur des données réalistes le taux ne sera pas nul.
- **La zone `UNCERTAIN` est dominée par les négatifs** — sous `balanced`, 24 négatifs contre
  3 positifs sur les jeux de conception ; 23 contre 4 au rejeu staging. C'est ce qui rend
  viable un second étage : une requête sur cinq, dominée par les `false_premise`, seule
  catégorie que le reranker ne tranche pas.

### Ce qui bloque l'activation

Deux familles ne sont pas couvertes : **`too_vague`** (N=2) et les **positifs faibles**
(dénombrement, réponses dispersées, cross-lingue faible). Elles ne bloquent ni la conception
ni un mode fantôme, mais **bloquent l'activation** : activer sans avoir mesuré les faux
rejets sur des positifs faibles reviendrait à refuser de répondre à des questions qui ont une
réponse, sans savoir combien. Une campagne de labellisation est demandée — familles et
volumes dans `backend/benchmarks/calibration/admission-policy-design/report.md` §M.

Artefacts de conception : `backend/benchmarks/calibration/admission-policy-design/`.

## 4. Récapitulatif

| Mécanisme | Fichier | Statut | Sur le chemin live ? |
|---|---|---|---|
| Seuil `should_answer` (RAG_MIN_SCORE) | `domain/policies.py` | implémenté, activé | **oui** |
| Answerability déterministe P1.8 (IDF) | `benchmark/answerability.py` | implémenté, benchmarké | **non** (offline) |
| Garde analytique Excel | `chat/services/excel_guard.py` | implémenté, activé | oui (refus séparé) |
| Politique `compare` (anti sur-abstention) | `services/query/{heuristics,prompt_builder}.py` | implémenté, activé | **oui** |
| Contrat de réponse (abstention / citations) | `services/answer_contract.py` | implémenté, activé | **oui** |
| Relance après abstention | — | **évalué, non implémenté** | non ([rapport](relance-anti-sur-abstention.md)) |

---

## Références d'implémentation

- `backend/apps/rag_core/domain/policies.py`
- `backend/apps/rag_core/services/query/{orchestrator,lowconf,heuristics,prompt_builder}.py`
- `backend/apps/rag_core/services/answer_contract.py`
- `backend/apps/chat/services/chat_formatting.py`
- `backend/apps/rag_core/benchmark/answerability.py`
- `backend/apps/rag_core/benchmark/runner.py`
- `backend/apps/rag_core/management/commands/rag_benchmark.py`
- `backend/apps/chat/services/excel_guard.py`
- `backend/config/settings/base.py`
