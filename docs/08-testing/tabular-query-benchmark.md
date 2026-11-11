# Benchmark du moteur tabulaire de production

Objectif : définir le protocole de mesure du **moteur tabulaire déterministe**
(`apps.rag_core.tabular`) — jeu de données, métriques obligatoires, et la règle
d'indépendance qui rend la mesure valable.
Public : développeurs backend / benchmark.

> **Statut** : *protocole défini* — le moteur est *implémenté* et l'exécuteur de
> référence l'est aussi, mais **aucune baseline n'a été exécutée**. Aucun chiffre
> d'exactitude, de latence ou de taux de refus n'existe à ce jour. La baseline est
> **planifiée**. Cette page ne contient volontairement **aucune valeur mesurée**.

Amont : benchmark-excel-tabular-aggregation.md
(protocole de vérité terrain, schéma des cas) ·
benchmark-stable-ground-truth.md ·
[strategie.md](strategie.md).
Moteur mesuré : [../03-backend/tabular-query-architecture.md](../03-backend/tabular-query-architecture.md).

---

## 1. Ce qui est mesuré

La question du benchmark n'est pas « la bonne source a-t-elle été retrouvée ? »
(`retrieval_lookup`) mais **« le bon résultat a-t-il été calculé ? »**. Un moteur peut
retrouver la bonne table et se tromper de somme : la métrique doit porter sur le
**résultat**, pas sur le rang d'un chunk.

Deux systèmes sont comparés sur le même cas :

| Rôle | Module | Nature |
|---|---|---|
| Système évalué | `apps.rag_core.tabular` (production) | plan → SQL DuckDB paramétré → résultat |
| Vérité terrain | `apps.rag_core.benchmark.tabular_reference_executor` | calcul pur en Python, sans SQL |

---

## 2. Règle d'indépendance — **impérative**

> **`tabular_reference_executor` ne doit JAMAIS être importé par du code de production.**
> Symétriquement, le moteur de production ne doit jamais être importé par l'exécuteur de
> référence ou ses dépendances.

La raison n'est pas l'hygiène de dépendances, c'est la **validité de la mesure** : si le
moteur et sa vérité terrain partageaient le même code, un défaut commun serait invisible
au benchmark, qui le reproduirait des deux côtés. Le verdict serait alors « les deux
chemins sont d'accord », ce qui ne dit rien sur la justesse.

C'est pourquoi la duplication est **délibérée et assumée** :

| Production | Référence | Ce que l'indépendance protège |
|---|---|---|
| `tabular/types.py` | `benchmark/tabular_types.py` | normalisation, `Decimal`, dates, identifiants |
| `tabular/executor.py` (DuckDB) | `tabular_reference_executor.py` (Python pur) | l'agrégation elle-même |
| `tabular/catalog.py` | `tabular_reference_executor.load_table()` | lecture du classeur |

Les deux chemins lisent le **fichier source**, jamais les chunks d'ingestion : une
agrégation calculée sur des chunks mesurerait le découpage.

### Vérification

Le contrôle est un `grep` : aucune occurrence de `benchmark.tabular_reference_executor`
(ni plus largement `benchmark.tabular_*`) ne doit apparaître sous
`backend/apps/rag_core/tabular/` ni sous `backend/api/`.

```bash
grep -rn "benchmark\.tabular" backend/apps/rag_core/tabular/ backend/api/
# attendu : aucune correspondance de code (seule une mention en docstring est admise)
```

Un test verrouillant cette propriété est **planifié**.

---

## 3. Jeu de données

Format : **JSONL**, une ligne = un cas, sérialisée par
`tabular_aggregation_schema.dump_case()` (`sort_keys=True`, `ensure_ascii=False`,
séparateurs compacts). La sérialisation est canonique : elle ne dépend pas de l'ordre
d'écriture des clés.

Le schéma des cas est celui décrit dans
benchmark-excel-tabular-aggregation.md
(`TABULAR_AGGREGATION_SCHEMA_VERSION = 1`). Structure d'un cas :

| Bloc | Contenu |
|---|---|
| Identité | `schema_version`, `case_id`, `rag_space`, `question`, `task_type`, `operation`, `difficulty`, `human_validation_status` |
| `ground_truth.document_ref` | `content_sha256`, `filename` — identité **logique**, jamais un UUID d'ingestion |
| `ground_truth.table_locator` | `sheet_name`, `table_range`, `header_rows`, `data_row_start`, `data_row_end` |
| `ground_truth.selection` | filtres normalisés (colonne, opérateur, valeur) |
| `ground_truth.aggregation` | fonction et colonne cible |
| `ground_truth.row_population_policy` | lignes incluses, libellés exclus, lignes vides |
| `ground_truth.missing_value_policy` | traitement des valeurs manquantes |
| `ground_truth.expected_result` | `type`, `value`, `comparison`, `tolerance` éventuelle |
| `ground_truth.aggregation_evidence` | notamment `selected_row_count` |

Contraintes du schéma, déjà outillées :

- `assert_no_uuid()` — aucun UUID d'ingestion ne doit entrer dans un cas : l'identité est
  logique (`sha256` + locator), sinon le jeu de données devient dépendant d'une base
  précise ;
- `validate_case()` — refus si `schema_version` ne correspond pas ;
- un cas dont la valeur existe déjà dans une ligne `TOTAL` est **refusé**
  (`materialized_aggregate_lookup`) : le classer en agrégation surestimerait la capacité
  de calcul.

**Données sensibles** : les artefacts réels (valeurs de cellules, questions dérivées de
documents réels) restent **hors Git**, sous `benchmarks/datasets/`. Seuls le code, les
schémas et cette page sont versionnés. Les fixtures de test sont **synthétiques**.

---

## 4. Métriques obligatoires

Aucun rapport de benchmark tabulaire n'est recevable sans **l'ensemble** de ces métriques.
Publier l'exactitude seule serait trompeur : un moteur qui refuse 90 % des cas et calcule
juste sur les 10 % restants afficherait une excellente exactitude.

### Résultat

| Métrique | Définition |
|---|---|
| `cases` | nombre de cas exécutés |
| `correct` | cas dont le **résultat** est correct selon la comparaison déclarée par le cas |
| `accuracy` | `correct / cases`, en `Decimal` |
| `by_comparison` | ventilation `total`/`correct` par type de comparaison |

La comparaison appliquée est **celle que le cas déclare**, jamais une autre choisie au
moment de l'évaluation : `exact_decimal`, `absolute_tolerance`, `relative_tolerance`,
`set`, `ordered`, ou l'identité de ligne pour les cas `argmin`/`argmax`.

### Couverture et refus

| Métrique | Définition |
|---|---|
| taux de réponse | cas répondus `ok` / cas exécutés |
| taux de clarification | cas terminés en `clarification_required`, ventilé par code (`ambiguous_column`, `ambiguous_table`, `duplicate_column_name`, `ambiguous_document`) |
| taux de refus explicite | cas terminés en `unsupported` / `error`, ventilé par code |
| refus par le planner | part de `planner_could_not_build_plan` — mesure la couverture linguistique du planner déterministe, à distinguer d'une erreur de calcul |
| dépassements de limites | cas `limit_exceeded`, ventilés par code |

### Sûreté — la métrique critique

| Métrique | Définition | Seuil |
|---|---|---|
| **réponses fausses silencieuses** | cas répondus `ok` avec un résultat **incorrect** | c'est le mode d'échec que le moteur existe pour empêcher |
| **provenance incohérente** | cas répondus `ok` dont la preuve ne décrit pas l'exécution réelle | comptée comme un **échec**, même si la valeur est juste |

Un résultat numériquement correct accompagné d'une provenance fausse est pire qu'une
erreur visible : il est vérifiable en apparence. Il compte donc comme un échec.

Contrôles de cohérence de preuve à appliquer sur chaque cas `ok` :
`evidence.selected_row_count` cohérent avec `aggregation_evidence.selected_row_count` du
cas ; `content_sha256` identique à celui du `document_ref` ; `sheet_name` et `data_range`
correspondant au `table_locator` ; `recalculated_by_ragatm` toujours `false`.

### Déterminisme

| Métrique | Définition |
|---|---|
| stabilité du `plan_hash` | un même cas rejoué doit produire le **même** `plan_hash` |
| stabilité du résultat | le résultat et le SQL compilé doivent être identiques d'une exécution à l'autre |

La propriété est déjà couverte unitairement
(`test_resultat_plan_et_hash_stables_sur_cinq_executions`) ; le benchmark la vérifie à
l'échelle du jeu de données.

### Performance

| Métrique | Définition |
|---|---|
| durée par cas | `duration_ms` de `TabularQueryExecution` |
| distribution | médiane et queue haute sur le jeu complet |
| cas en `query_timeout` | dépassements de `TABULAR_MAX_QUERY_SECONDS` |

**Aucune valeur de latence n'est publiée à ce jour** : la baseline n'a pas été exécutée.

---

## 4 bis. Résolution documentaire canonique

Un cas désigne son document par son **`content_sha256`**, jamais par son nom.

| Rôle | Valeur |
|---|---|
| **Identité** | `content_sha256` |
| **Diagnostic** | `filename` |
| **Résolution locale** | `document_id` runtime, dérivé du hachage |

Résoudre par le nom laissait un dataset pourtant correctement figé s'exécuter contre le
mauvais classeur au premier doublon, renommage ou collision de nom — la propriété que le
gel du dataset est censé garantir n'était pas tenue au moment de l'exécution.

Trois refus, **sans repli silencieux sur le nom** :

| Code | Situation |
|---|---|
| `missing_content_sha256` | le cas ne porte pas de hachage : identité non vérifiable |
| `document_not_found` | aucune correspondance — **même si** un fichier porte le bon nom |
| `duplicate_content_sha256` | plusieurs correspondances : lequel le dataset visait-il ? |

Un échec de résolution devient un statut `error` portant son code et sa trace, jamais une
exécution contre un document approchant.

Chaque exécution journalise `document_resolution` : document et hachage **attendus**,
document et hachage **résolus**, méthode employée, et un indicateur `renamed` — un
renommage reste légitime, mais doit être **visible**.

> **Exception déclarative, et non repli** : les cas qui ne nomment aucun document
> (`document=""`) mesurent justement la sélection *par le moteur* depuis la question. Ils
> ne portent donc pas de hachage et conservent la voie naturelle. La distinction est
> portée par le cas lui-même, pas par un rattrapage à l'exécution.

## 5. Protocole d'exécution

1. **Prérequis** — `TABULAR_QUERY_ENABLED=1` sur l'environnement de mesure uniquement
   (la documentation d'exploitation (hors périmètre de cette édition)).
   `TABULAR_LLM_PLANNER_ENABLED` et `TABULAR_NARRATION_ENABLED` restent à `0` : on mesure
   le moteur déterministe.
2. **Vérité terrain** — chaque cas est calculé par `tabular_reference_executor`, sans
   passer par le moteur de production. Toute ambiguïté (colonnes homonymes, colonne de
   type mixte, ex æquo en tête d'un `argmax`) est un **rejet du cas**, pas un arbitrage :
   un benchmark qui devine produit des mesures indéfendables.
3. **Système évalué** — chaque cas est soumis au moteur, soit par question
   (`answer_question`), soit par plan (`execute_validated_plan`). Les deux chemins
   subissent les **mêmes** validations.
4. **Verdict** — `tabular_metrics.evaluate()` applique la comparaison déclarée par le cas ;
   `aggregate_scores()` agrège.
5. **Rapport** — les métriques du §4 **en totalité**, plus la version de schéma de plan
   (`1.0`), la version de schéma de cas (`1`), et les valeurs de configuration effectives
   des bornes `TABULAR_MAX_*`.

Un rapport qui omet le taux de refus ou les réponses fausses silencieuses n'est pas un
rapport de benchmark.

---

## 6. Ce qui reste à faire

| Élément | Statut |
|---|---|
| Protocole, schéma de cas, exécuteur de référence, métriques | *implémenté* |
| Moteur de production | *implémenté*, activable par flag (défaut `0`) |
| Test verrouillant l'indépendance production / référence | **planifié** |
| Jeu de données officiel du moteur de production | **planifié** |
| Holdout | **planifié** |
| **Baseline chiffrée** | **planifié — aucune exécution à ce jour** |
| Câblage CI | **non branché** |

---

## Références d'implémentation

- `backend/apps/rag_core/benchmark/tabular_reference_executor.py` (vérité terrain, calcul pur)
- `backend/apps/rag_core/benchmark/tabular_aggregation_schema.py` (schéma des cas, `dump_case`, `assert_no_uuid`)
- `backend/apps/rag_core/benchmark/tabular_metrics.py` (`evaluate`, `aggregate_scores`)
- `backend/apps/rag_core/benchmark/tabular_types.py` (typage **indépendant** de la production)
- `backend/apps/rag_core/benchmark/tabular_case_generator.py`, `tabular_inventory.py`
- `backend/apps/rag_core/tabular/` (moteur mesuré)
- Tests : `tests/tests_root/rag_core/test_tabular_{engine,reference_executor,metrics,types,double_computation,case_generator,excel_loading,aggregation_schema,inventory}.py`
