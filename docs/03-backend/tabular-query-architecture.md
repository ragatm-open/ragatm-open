# Moteur tabulaire déterministe — architecture

Objectif : décrire le pipeline **réel** qui transforme une question en français en un
résultat chiffré calculé sur un classeur `.xlsx` ou un `.csv`, sans qu'aucun LLM ne
produise ni SQL, ni code, ni valeur.
Public : développeurs backend. Source de vérité = le code.

> **Statut** : *implémenté*, **activable par flag** — `TABULAR_QUERY_ENABLED` vaut **`0`
> par défaut**. Tant que le flag est à `0`, le comportement historique est strictement
> conservé : la garde `excel_guard` du chat refuse les questions analytiques sur classeur
> (voir [chat-generation.md §2](chat-generation.md)). Le planner LLM
> (`TABULAR_LLM_PLANNER_ENABLED`) et la narration (`TABULAR_NARRATION_ENABLED`) sont
> **non branchés** (défaut `0`).

Valeurs et flags : [feature-flags.md](../10-reference/feature-flags.md) ·
[variables-environnement.md](../10-reference/variables-environnement.md).
Sécurité et frontière de confiance : [tabular-query-security.md](tabular-query-security.md).
Opérations et typage : [tabular-query-operations.md](tabular-query-operations.md).

---

## 1. Le principe

Le mode d'échec que ce moteur existe pour empêcher est précis : **une réponse
numériquement plausible et fausse**. Un LLM à qui l'on donne le top-k des chunks d'un
classeur peut produire un total qui a l'air juste ; il mesure alors le découpage, pas le
document.

Trois conséquences structurent tout le reste :

1. **Le calcul est fait par le moteur**, jamais par un modèle. Le LLM ne produit ni SQL,
   ni Python, ni le résultat.
2. **Les données viennent du fichier source**, jamais des chunks d'ingestion.
3. **Un refus explicite vaut mieux qu'une approximation.** Chaque code d'erreur de
   `errors.py` est un endroit où le moteur a choisi de ne pas deviner.

---

## 2. Le pipeline

Orchestré par `service.answer_question()`. Aucune étape n'est court-circuitable : chaque
fonction en aval prend le **type** produit par l'étape précédente, jamais un `dict`.

```
question
  → document accessible        (permissions.accessible_documents)
  → octets du fichier source   (service._read_document_bytes)
  → catalogue                  (catalog.build_xlsx_catalog / build_csv_catalog)
  → feuille / table            (table_selector.select_table)
  → colonnes                   (column_resolver.resolve_column)
  → plan                       (planner.build_plan)
  → validation STRUCTURELLE    (plan_schema.parse_plan)
  → validation SÉMANTIQUE      (plan_validator.validate)
  → compilation                (plan_compiler.compile_plan)
  → exécution                  (executor.execute)
  → provenance                 (provenance.build_evidence)
  → résultat                   (result_formatter)
```

| Étape | Module | Ce qu'elle garantit |
|---|---|---|
| Périmètre | `tabular/permissions.py` | Filtrage **à la source** : un document hors périmètre n'est jamais dans le jeu de résultats. |
| Lecture | `tabular/service.py` | Le chemin vient du champ `storage_file` du modèle, jamais d'une entrée utilisateur. |
| Catalogue | `tabular/catalog.py` | Structure et données lues **dans le fichier**, jamais dans les chunks. |
| Table | `tabular/table_selector.py` | Une table élue, ou `clarification_required`. Jamais « la première ». |
| Colonne | `tabular/column_resolver.py` | Une colonne élue avec dominance, ou `clarification_required`. |
| Plan | `tabular/planner.py` | Planner **déterministe** (règles + catalogue). Aucun LLM. |
| Structure | `tabular/plan_schema.py` | Schéma Pydantic **fermé** (`extra="forbid"`), listes `Literal`. |
| Sémantique | `tabular/plan_validator.py` | Existence, appartenance, compatibilité de type, bornes. |
| Compilation | `tabular/plan_compiler.py` | SQL constant + paramètres séparés. Alias dérivés d'entiers. |
| Exécution | `tabular/executor.py` | DuckDB `:memory:`, `enable_external_access=false`, lignes injectées depuis Python. |
| Preuve | `tabular/provenance.py` | Provenance **dérivée de l'exécution réelle**, jamais rédigée. |
| Sortie | `tabular/result_formatter.py` | Résultat structuré = la vérité. Nombres en chaîne décimale. |

### Pourquoi ce découpage

La séparation n'est pas cosmétique : elle rend chaque propriété **vérifiable
isolément**.

- `plan_schema` ne calcule rien et n'accède à rien — il *décrit et refuse*. C'est ce qui
  permet de tester la frontière de confiance sans base ni fichier.
- `plan_compiler` ne lit aucun fichier, n'ouvre aucune connexion, ne connaît aucun
  chemin : il transforme une structure en `(sql, params)`. On peut donc prouver
  l'absence d'injection sans exécuter quoi que ce soit.
- `executor` n'accepte pas de chaîne SQL de l'extérieur : sa signature prend un
  `CompiledQuery`. Il n'existe pas d'API par laquelle du SQL arbitraire entrerait.
- La validation **structurelle** (Pydantic) est séparée de la validation **sémantique**
  (base + catalogue) parce que la seconde a besoin de la base et que la première doit
  pouvoir s'exécuter sans elle.

Un plan construit par le planner interne **repasse toujours** par
`plan_schema.parse_plan()`. C'est ce qui rend la frontière vérifiable — et ce qui fera
qu'un plan LLM n'aura aucun privilège le jour où il sera activé.

---

## 3. Le plan tabulaire

Représentation intermédiaire **versionnée** — `PLAN_SCHEMA_VERSION = "1.0"`. Seule cette
valeur est acceptée : aucune inférence, aucune rétro-compatibilité implicite. Un plan
d'une autre version est refusé (`invalid_plan_schema_version`).

### Structure

| Champ | Type | Règle |
|---|---|---|
| `schema_version` | `"1.0"` | valeur exacte, sinon refus |
| `operation` | `Literal[...]` | liste fermée (§ [opérations](tabular-query-operations.md)) |
| `source` | `{document_id, sheet_id, table_id}` | **source unique** — aucune jointure |
| `target` | `{column_id}` ou absent | exigé par les opérations mesurantes, interdit sinon |
| `filters` | liste de `{column_id, operator, value/values}` | plafonné par `TABULAR_MAX_FILTERS` |
| `order_by` | liste de `{column_id, direction}` | interdit sur une opération scalaire ; une colonne ne peut être triée deux fois |
| `select_columns` | liste de `column_id` | vide = toutes les colonnes de la table |
| `limit` | entier ≥ 0 | exigé et non nul pour `top_n`/`bottom_n` |

Toutes les colonnes sont désignées par **`column_id` (UUID du catalogue)**, jamais par un
nom. Toutes les sous-structures sont `extra="forbid"` et `frozen=True`.

### Exemple réel

Somme du chiffre d'affaires pour la région Ouest, au-delà d'un seuil :

```json
{
  "schema_version": "1.0",
  "operation": "sum",
  "source": {
    "document_id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
    "sheet_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
    "table_id": "6ba7b811-9dad-11d1-80b4-00c04fd430c8"
  },
  "target": { "column_id": "9f8c2a14-7d3e-4a51-9c60-1e2b3c4d5e6f" },
  "filters": [
    { "column_id": "3d1e5f22-4a6b-4c8d-9e0f-1a2b3c4d5e6f",
      "operator": "equals", "value": "Ouest" },
    { "column_id": "9f8c2a14-7d3e-4a51-9c60-1e2b3c4d5e6f",
      "operator": "greater_than", "value": "1000" }
  ],
  "order_by": [],
  "select_columns": [],
  "limit": null
}
```

### Hash de plan

`TabularPlan.canonical_dict()` produit une représentation canonique (clés triées, UUID et
`Decimal` sérialisés explicitement) dont `plan_hash()` prend le SHA-256. Deux plans
sémantiquement identiques produisent le **même hash**, quels que soient l'ordre
d'écriture des clés et la machine. C'est la base de la reproductibilité et de la
comparaison d'exécutions.

---

## 4. Le catalogue

`tabular/catalog.py` inventorie les tables d'un document et charge leurs lignes typées.
Les deux responsabilités partagent la même vérité : **structure et données viennent du
fichier source**.

| Niveau | Contenu |
|---|---|
| `DocumentCatalog` | `document_id`, `content_sha256`, `filename`, tables, avertissements |
| `CatalogTable` | `sheet_name`, `data_range`, `header_range`, `row_count`, `column_count`, `has_formulas`, `has_uncached_formula` |
| `CatalogColumn` | `column_id` (UUID), `original_name`, `normalized_name`, `position`, `inferred_type`, `unit`, `null_ratio` |

### Identifiants dérivés, jamais aléatoires

Les `sheet_id`, `table_id` et `column_id` sont **dérivés** par `uuid5` à partir du
`content_sha256` du document, du nom de feuille, de la position et du nom normalisé de la
colonne — jamais tirés au hasard.

La raison est le déterminisme : un `uuid4()` produirait des identifiants différents à
chaque reconstruction du catalogue, donc un `plan_hash` différent pour le même plan
sémantique. Les plans ne seraient ni reproductibles ni rejouables, et la propriété de
déterminisme exigée du moteur serait fausse.

La dérivation incluant le hash du document, deux versions d'un même fichier produisent des
identifiants **distincts** : l'invalidation est automatique — un plan écrit sur l'ancienne
version ne peut pas désigner par accident une colonne de la nouvelle.

Détails de construction :

- **XLSX** — double lecture assumée : `data_only=True` donne les valeurs mises en cache,
  `data_only=False` révèle les cellules **porteuses** de formule. Sans la seconde, une
  formule non recalculée serait indiscernable d'une cellule vide.
- La ligne d'en-tête est la première ligne majoritairement textuelle suivie d'au moins
  une ligne de données, cherchée dans les **20 premières lignes**.
- **CSV** — une feuille, une table ; le délimiteur est **détecté** (`csv.Sniffer` sur
  `,` `;` tabulation `|`), jamais supposé. BOM UTF-8 et repli CP1252 sont signalés
  (`csv_utf8_bom`, `csv_encoding_fallback_cp1252`).
- Les **lignes de synthèse** (`total`, `sous-total`, `cumul`, `moyenne`, `somme`,
  `grand total`…) sont écartées du corps : les inclure dans une somme compterait deux
  fois les mêmes montants.
- Le type de colonne est inféré sur un échantillon de **200 lignes**
  (`TYPE_SAMPLE_ROWS`).

---

## 5. Modèle de données et invalidation

Migration : `backend/apps/rag_core/migrations/0032_tabulardocumentprofile_tabularqueryexecution_and_more.py`.

| Modèle | Rôle |
|---|---|
| `TabularDocumentProfile` | Profil tabulaire d'un document, lié à son `content_sha256`. Statuts `ready` / `stale` / `failed`, champ `detail` (jamais un silence). |
| `TabularTable` | Une table détectée : feuille, `data_range`, `header_range`, comptes, état des formules **au niveau table**. |
| `TabularColumn` | Une colonne : nom d'origine, nom normalisé, position, type inféré, unité, ratio de nuls. |
| `TabularQueryExecution` | Trace d'exécution append-only : `plan_hash`, `evidence_hash`, opération, plan et preuve tels qu'exécutés, durée. |

**Invalidation par `content_sha256`.** Une contrainte d'unicité
`uniq_tabular_profile_per_document_hash` interdit deux profils pour le même couple
(document, hash) : ce seraient deux vérités concurrentes. Si le fichier est remplacé, le
hash change, le profil existant ne correspond plus — il est reconstruit, jamais réutilisé
« au cas où » (code `catalog_stale`).

### Pourquoi les lignes ne sont pas stockées

Recopier les lignes en base créerait une **seconde source de vérité**, qui divergerait du
fichier au premier remplacement — exactement le défaut que `content_sha256` sert à
détecter. Les données sont relues depuis le document source à chaque requête.

Dans ce lot, le catalogue lui-même est **reconstruit à chaque requête**
(`service._build_catalog`). C'est plus lent, mais il ne peut structurellement pas être
périmé : la persistance sert l'inventaire, l'invalidation et l'auditabilité — pas le
chemin de calcul.

La trace d'exécution est en revanche best-effort : `_persist_execution` journalise un
échec sans invalider le calcul. Un défaut de traçabilité ne doit jamais transformer un
résultat juste en erreur.

---

## 6. Résultat et provenance

`provenance.build_evidence()` construit la preuve **à partir du plan exécuté et de son
résultat**. Elle n'est jamais reconstruite après coup, jamais rédigée par un LLM. Une
réponse numériquement correcte accompagnée d'une provenance fausse est comptée comme un
échec — elle est pire qu'une erreur visible, parce qu'elle est vérifiable en apparence.

Champs exposés : `document_id`, `filename`, `content_sha256`, `sheet_name`, `table_id`,
`data_range`, `header_range`, `operation`, `target_column`, `target_column_id`,
`result_unit`, `filters`, `selected_row_count`, `table_row_count`, `plan_hash`,
`evidence_hash`, `row_references`, `formulas`. Détail :
[tabular-query-operations.md §6](tabular-query-operations.md).

`selected_row_count` est **recompté** par le moteur sur les mêmes filtres
(`executor.count_selected`), jamais estimé ni dérivé du résultat : une somme ne dit pas
combien de lignes l'ont produite.

Côté sortie, le **résultat structuré est la vérité**. Les nombres sortent en chaîne
décimale (`"1284350.00"`) et non en flottant JSON : un flottant perd des décimales sur
les grands montants en traversant les clients. Les alias internes (`c0`, `c1`) ne sortent
jamais de l'API — le client voit les noms d'origine du classeur.

---

## 7. API

| Route | Méthode | Objet |
|---|---|---|
| `/api/v1/tabular/query/` | `POST` | Question (`question`) **ou** plan (`plan`) sur un `rag_space_id` |
| `/api/v1/tabular/capabilities/` | `GET` | Opérations supportées, opérations différées, opérateurs, version de schéma, état des flags |

La réponse porte **toujours** un statut explicite :

| Statut | HTTP | Sens |
|---|---|---|
| `ok` | 200 | Résultat calculé |
| `clarification_required` | 200 | Question légitime, information manquante — ce n'est pas une erreur du client |
| `unsupported` | 422 | Reconnu mais hors périmètre (dont `tabular_query_disabled`) |
| `limit_exceeded` | 413 | Une borne de ressources a été atteinte |
| `forbidden` | 403 | Hors périmètre d'accès |
| `error` | 400 | Refus typé du moteur |

Aucune erreur n'est convertie en résultat vide : « 0 » et « je ne sais pas » sont deux
réponses différentes, et les confondre est le défaut le plus grave ici. Un défaut
inattendu produit un `500` `internal_error` journalisé avec sa trace, sans détail interne.

`execute_validated_plan()` (plan fourni tel quel — usage benchmark et futur planner LLM)
applique **les mêmes contrôles, dans le même ordre, avec les mêmes refus**. Un plan
externe n'ouvre aucun raccourci.

---

## 7bis. Préflight volumétrique et ingestion en masse

Statut : *implémenté et mesuré*.

### Refus précoce

Un document hors limite est refusé **avant** toute lecture complète, normalisation ou
transfert. Le scan s'arrête à `max_rows + 1` : une ligne de plus que la limite suffit à
prouver le dépassement, et rien au-delà n'apporte d'information.

| Cas | Avant | Après |
|---|---|---|
| CSV 1 000 000 lignes, limite 100 000 | plusieurs minutes | **0,11 s** |
| CSV 100 001 lignes | plusieurs secondes | 0,06 s |

Codes : `input_row_limit_exceeded`, `input_column_limit_exceeded`,
`input_size_limit_exceeded`, `uncompressed_size_limit_exceeded`. Un refus précoce ne
produit **aucune evidence** de calcul.

Deux pièges sont traités explicitement :

- **CSV** — le comptage passe par `csv.reader`, jamais par un décompte de `\n`. Une
  cellule peut contenir un saut de ligne entre guillemets ; compter les octets
  surestimerait et refuserait un document valide.
- **XLSX** — la dimension déclarée ne sert qu'à **refuser vite**. Elle n'accepte jamais
  et n'établit jamais un compte : un classeur déclarant `A1:B5` tout en contenant
  1 500 lignes est refusé par le scan borné. Même piège que celui que
  `reset_dimensions()` corrige à l'ingestion.

### Ingestion en masse — stratégies mesurées

| Stratégie | Débit | Retenue |
|---|---|---|
| `executemany` (chemin d'origine) | 1 193 l/s | référence |
| `executemany` par lots de 10 k | 995 l/s | non — plus lent |
| `INSERT` multi-`VALUES` (5 k) | 1 701 l/s | non — 1,4× seulement |
| `conn.append` | — | non — exige pandas |
| **CSV canonique → `read_csv` natif** | **142 415 l/s** | **retenue** |
| `read_csv` sur le fichier source | 1 582 694 l/s | **non** — changerait les résultats |

La dernière ligne mérite l'explication. Le lecteur natif appliqué directement au fichier
source serait onze fois plus rapide encore, mais son *casting* court-circuiterait la
normalisation du moteur — séparateurs français, pourcentages en fraction, devises, refus
des formats ambigus. Le gain se paierait en résultats différents, ce qui est exclu.

La stratégie retenue préserve toute la sémantique : Python normalise via `types.coerce`,
écrit un CSV **canonique** (décimales en forme `f`, dates ISO), et DuckDB le relit avec
des types **explicites** (`columns={...}`, aucune auto-détection). Sommes vérifiées
bit-exactes à 10 k, 100 k et 500 k lignes.

Débit de bout en bout : **116 000 à 180 000 lignes/s**.

### Sécurité du pont — pourquoi le compromis est tenable

`read_csv` exige `enable_external_access`, le verrou central du moteur. Trois propriétés
rendent son ouverture temporaire acceptable, toutes vérifiées par test :

1. **une seule instruction** s'exécute dans cet état — un `CREATE TABLE` dont le texte
   est une constante du module et le chemin un **paramètre lié** vers un fichier
   temporaire écrit par nous en `0600` ; ni glob, ni URL, ni entrée utilisateur ;
2. le verrou est **refermé avant** que le plan de l'utilisateur ne s'exécute ;
3. il est **irréversible** — DuckDB refuse `SET enable_external_access = true` sur une
   base en cours (`InvalidInputException`).

La troisième est décisive : sans elle, refermer ne servirait à rien puisque le plan
pourrait rouvrir. Après fermeture, `read_csv`, `read_parquet`, `COPY TO`, `ATTACH`,
`INSTALL` et `LOAD` échouent tous en `PermissionException`, connexion encore ouverte.

En dessous de `CSV_BRIDGE_MIN_ROWS` (5 000), le chemin historique reste utilisé : le
coût fixe du pont y dépasserait son gain, et le verrou est alors posé d'emblée.

## 8. Dépendances

Trois dépendances **cœur** (et non un extra) : le moteur est importé par `apps.chat`
(routage) et par l'API. Un extra oublié ne dégraderait pas le service, il casserait le
démarrage. Toutes trois sont sous flag à l'exécution (`TABULAR_QUERY_ENABLED=0` par
défaut), mais présentes à l'import.

| Paquet | Contrainte | Rôle | Si absent |
|---|---|---|---|
| `duckdb` | `>=1.1,<2.0` | Moteur analytique embarqué, **en mémoire**. Aucun accès disque : les lignes sont insérées depuis Python et `enable_external_access=false` coupe `read_csv`/`read_parquet`/`ATTACH`/`COPY`/`INSTALL`. Wheel autonome, aucune dépendance système. | Le moteur ne démarre pas — c'est pourquoi c'est une dépendance cœur. |
| `pydantic` | `>=2.7,<3.0` | Validation **structurelle** du plan (listes fermées `Literal`, `extra="forbid"`). C'est la frontière de confiance : sans elle, un plan malformé atteindrait le compilateur. | Le moteur ne démarre pas. Épinglé explicitement plutôt que laissé au hasard d'un transitif — une dépendance de sécurité ne doit pas dépendre d'un autre paquet. |
| `rapidfuzz` | `>=3.9,<4.0` | **Signal secondaire** de résolution de colonnes, plafonné sous le niveau lexical : il départage, il n'élit jamais seul une colonne ambiguë. | Repli automatique sur `difflib` (`column_resolver._fuzzy_ratio`) : la résolution est **dégradée**, pas cassée. |

La borne majeure de `duckdb` n'est pas décorative : son API Python change entre majeures,
et le moteur en dépend directement (paramétrage, types `DECIMAL`, `interrupt()`).

**Non introduits, délibérément** : `pyarrow` et `polars`. Aucune représentation Parquet
persistée n'est nécessaire dans ce lot — les lignes sont relues du fichier source et
passées en mémoire à DuckDB. Introduire un format intermédiaire créerait une seconde
source de vérité à invalider, pour un gain nul à cette échelle.

## Références d'implémentation

- `backend/apps/rag_core/tabular/service.py` (orchestration, ordre imposé)
- `backend/apps/rag_core/tabular/plan_schema.py` (schéma versionné, listes fermées)
- `backend/apps/rag_core/tabular/plan_validator.py` (validation sémantique)
- `backend/apps/rag_core/tabular/plan_compiler.py` (SQL constant + paramètres)
- `backend/apps/rag_core/tabular/executor.py` (DuckDB confiné)
- `backend/apps/rag_core/tabular/catalog.py` (inventaire, chargement typé)
- `backend/apps/rag_core/tabular/{table_selector,column_resolver,planner}.py`
- `backend/apps/rag_core/tabular/{types,provenance,result_formatter,errors,permissions,config}.py`
- `backend/apps/rag_core/models/tabular.py`
- `backend/apps/rag_core/migrations/0032_tabulardocumentprofile_tabularqueryexecution_and_more.py`
- `backend/api/v1/tabular/views.py`, `backend/api/v1/urls.py`
- `backend/config/settings/base.py` (bloc `TABULAR_*`)
- Tests : `tests/tests_root/rag_core/test_tabular_engine.py`, `tests/tests_root/rag_core/test_tabular_integration.py`
