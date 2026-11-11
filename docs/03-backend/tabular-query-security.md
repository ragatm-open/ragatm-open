# Moteur tabulaire — frontière de confiance et sécurité

Objectif : décrire les propriétés de sécurité **réelles** du moteur tabulaire, les cas
adversariaux couverts et les refus attendus. Public : développeurs backend, ops,
revue de sécurité. Le CODE fait foi.

> **Statut** : *implémenté*, **activable par flag** — `TABULAR_QUERY_ENABLED` vaut **`0`
> par défaut**. Flag à `0` : la route répond `unsupported` / `tabular_query_disabled` et
> le refus historique de la garde `excel_guard` reste inchangé.

Architecture d'ensemble : [tabular-query-architecture.md](tabular-query-architecture.md).
Politique générale : la documentation d'exploitation (hors périmètre de cette édition) ·
[permissions-isolation.md](permissions-isolation.md).

---

## 1. Les trois propriétés

La sécurité du moteur ne repose pas sur du filtrage ou de l'échappement, mais sur trois
propriétés **structurelles** : les surfaces d'attaque sont absentes par construction
plutôt que neutralisées après coup.

### Propriété 1 — aucun texte utilisateur n'atteint le SQL

Le plan désigne les colonnes par **`column_id` (UUID du catalogue)**, jamais par un nom
tapé par quelqu'un. Le compilateur traduit chaque `column_id` en un **alias positionnel
qu'il génère lui-même** : `column_alias(position)` → `c0`, `c1`, … dérivé d'un **entier**
(la position dans le catalogue).

Le texte SQL est assemblé exclusivement à partir de :

- littéraux écrits dans `plan_compiler.py` (`SELECT`, `WHERE`, `SUM`…) ;
- alias générés par le moteur ;
- marqueurs de paramètre `?`.

Les valeurs voyagent **séparément**, dans une liste de paramètres passée à DuckDB.

Conséquence pratique : une colonne réellement nommée `"; DROP TABLE t; --"` dans un
classeur est compilée en `c3`. **Il n'y a rien à neutraliser** — et donc aucun oubli
d'échappement possible. Un identifiant de colonne inconnu ne « ressemble » à rien : il
est absent du catalogue, donc refusé (`column_not_in_table`).

### Propriété 2 — schéma fermé

`plan_schema.py` ne calcule rien et n'accède à rien : il *décrit et refuse*.

- **Listes `Literal`** — opérations, opérateurs de filtre, directions de tri. Pydantic
  refuse tout le reste, y compris `"sum(x); DROP TABLE"` : ce n'est pas « une opération
  malveillante », c'est simplement une opération **inconnue**.
- **`extra="forbid"` partout** — un champ non prévu fait **échouer** la validation au
  lieu d'être ignoré. C'est ce qui empêche un plan de transporter une charge utile que le
  compilateur ignorerait mais qu'un composant ultérieur lirait.
- **Valeurs scalaires étroites** — `ScalarValue = str | int | float | bool | Decimal |
  None`. Pas de `dict`, pas de liste imbriquée, pas d'objet : rien qui puisse porter une
  structure.
- **Version exacte** — seule `"1.0"` est acceptée, sans rétro-compatibilité implicite.
- **Modèles `frozen=True`** — un plan validé ne peut plus être modifié entre la
  validation et l'exécution.

### Propriété 3 — DuckDB ne voit jamais un fichier

Les lignes sont fournies **depuis Python**, par insertion paramétrée dans une table
temporaire. Il n'y a donc aucun chemin à valider, aucun `read_csv`, aucun `read_parquet`,
aucun `ATTACH` utile : la surface « lire un fichier arbitraire » **n'existe pas**.

S'y ajoutent des verrous explicites — une propriété non testée n'est pas une propriété :

| Verrou | Effet |
|---|---|
| connexion `:memory:` | créée puis fermée **par requête**, jamais partagée |
| `enable_external_access = false` | coupe d'un coup système de fichiers, réseau, `ATTACH` vers un fichier, `COPY … TO` et installation d'extensions |
| `autoinstall_known_extensions = false` | pas d'installation automatique |
| `autoload_known_extensions = false` | pas de chargement automatique |
| `memory_limit` | borné par `TABULAR_MAX_MEMORY_MB` |
| `threads = 1` | exécution mono-thread |
| une seule instruction par requête | le compilateur n'en produit jamais deux |
| délai maximal | armé côté Python, `conn.interrupt()` au dépassement |

`executor.execute()` n'accepte **pas** de chaîne SQL de l'extérieur : sa signature prend
un `CompiledQuery`. Le DDL de création de table est lui aussi entièrement sous contrôle
du moteur — alias issus de `column_alias`, types issus de la table fermée
`_DUCKDB_TYPES`. Aucun nom d'origine ne participe.

---

## 2. Cas adversariaux et refus attendus

Tous les cas ci-dessous sont **prouvés par les tests** de
`tests/tests_root/rag_core/test_tabular_engine.py`.

### Champs hors schéma (charge utile transportée par le plan)

| Champ injecté dans le plan | Résultat |
|---|---|
| `{"sql": "SELECT 1"}` | refus `invalid_plan_schema` |
| `{"expression": "__import__('os').system('id')"}` | refus `invalid_plan_schema` |
| `{"path": "/etc/passwd"}` | refus `invalid_plan_schema` |
| `{"function": "read_csv"}` | refus `invalid_plan_schema` |
| `{"connection": "attach"}` | refus `invalid_plan_schema` |

Le champ n'est **pas ignoré** : il fait échouer la validation. L'ignorer le laisserait
disponible pour un composant en aval.

### Opérations et opérateurs hors liste

| Entrée | Résultat |
|---|---|
| `operation: "os.system"` | `unsupported_operation` |
| `operation: "median"` / `"join"` / `"group_by"` | `unsupported_operation` (reconnue, **différée**) |
| `operator: "; DROP TABLE t; --"` | `unsupported_filter_operator` |
| `schema_version: "9.9"` | `invalid_plan_schema_version` |
| `limit: -5` | refus (`Field(ge=0)`) |
| `column_id` absent de la table | `column_not_in_table` |

Une opération différée est nommée explicitement pour pouvoir être refusée avec un
message utile (« hors périmètre ») plutôt qu'un « opération inconnue » — et pour garantir
qu'elle ne sera jamais traitée par approximation.

### Valeurs adversariales dans un filtre

Ces charges sont des **valeurs légitimes du point de vue du schéma** : une cellule peut
réellement contenir ce texte. Elles sont passées en paramètre, jamais concaténées.

| Charge | Comportement observé |
|---|---|
| `Ouest'; DROP TABLE t; --` | passée en paramètre ; 0 ligne correspondante ; table intacte |
| `' OR 1=1 --` | idem |
| `'; INSTALL httpfs; --` | idem — et `enable_external_access=false` interdit toute installation |
| `'; ATTACH '/tmp/x.db'; --` | idem — `ATTACH` vers un fichier est coupé |
| `'; COPY t TO '/tmp/out.csv'; --` | idem — l'écriture externe est coupée |
| `../../etc/passwd` | idem — aucun chemin n'est jamais interprété |

Les invariants vérifiés à chaque fois : la charge figure dans `query.params`, **pas** dans
`query.sql` ; `query.sql` ne contient **aucun `;`** (une seule instruction) ; et un
comptage global ultérieur retrouve la table complète et intacte.

### Nom de colonne hostile dans le classeur

Une colonne nommée `"; DROP TABLE t; --"` en position 3 se compile en `c3`. Le SQL produit
pour une somme est littéralement `SELECT SUM(c3) AS value FROM t`.

### Exécution de code

Il n'existe **aucun chemin** par lequel du Python soit évalué : ni `eval`, ni `exec`, ni
champ `expression` accepté (`expression_forbidden` existe comme code, et le schéma refuse
le champ en amont). Le LLM, s'il est un jour autorisé à produire un plan, produit **ce
JSON et rien d'autre** : jamais du SQL, jamais du Python, jamais un nom de colonne
appliqué tel quel.

---

## 3. Permissions, isolation, anti-énumération

Aucune règle d'autorisation n'est réinventée : le moteur passe par
`require_rag_space_access`, la **même porte** que le chat et les documents. Un second
système d'autorisation serait un second endroit où se tromper, et les deux divergeraient
au premier changement.

**Filtrage à la source.** `permissions.accessible_documents()` vérifie l'accès au
RagSpace **avant** toute requête sur `Document`, puis restreint le queryset au RagSpace
autorisé et exclut les documents `deleted`. Un document hors périmètre n'est pas
« trouvé puis rejeté » : il **n'existe pas** pour cette requête.

**Anti-énumération.** Un document inaccessible et un document inexistant produisent la
**même réponse**. Le moteur ne peut donc pas servir d'oracle d'existence. Deux
conséquences dans le code :

- `permissions.require_access` traduit un `PermissionDenied` en message volontairement
  générique (« aucun accès à cet espace ») : jamais de confirmation de l'existence d'un
  RagSpace. Les **deux** exceptions `PermissionDenied` (celle de Django et celle de DRF)
  sont attrapées : n'en attraper qu'une laisserait l'autre remonter en `500` — une erreur
  serveur là où la réponse correcte est un refus d'accès.
- Dans `plan_validator.validate`, l'**ordre des contrôles n'est pas indifférent** : les
  vérifications d'existence et de périmètre passent **avant** les contrôles de type. Un
  utilisateur sans accès reçoit le même refus, qu'il ait ou non demandé une somme sur une
  colonne texte — sinon le message d'erreur deviendrait un oracle révélant la structure
  d'un document auquel il n'a pas droit.
- Un plan désignant un autre document que celui du catalogue chargé produit
  `permission_denied`, pas une erreur de structure.

**Aucun chemin utilisateur.** Le chemin du fichier vient exclusivement du champ
`storage_file` du modèle `Document` ; les octets sont lus via le stockage Django. Seules
les extensions
`.xlsx`, `.csv` et `.tsv` sont interrogeables (`unsupported_file_format`).

**Journalisation.** Les refus sont journalisés au niveau `info` avec leur code et leur
statut ; un défaut inattendu est journalisé avec sa trace mais la réponse HTTP ne porte
**aucun détail interne**.

---

## 4. Limites de ressources

Toute limite atteinte produit un **code explicite**, jamais une troncature silencieuse.
Les bornes sont lues **une seule fois** par requête (`config.current_limits()`) et
transmises à la validation, à la compilation et à l'exécution : le plan est validé sous
exactement les mêmes bornes que celles sous lesquelles il s'exécute.

| Variable | Défaut | Borne | Code de dépassement |
|---|---|---|---|
| `TABULAR_MAX_ROWS_SCANNED` | `200000` | lignes chargées et parcourues | `too_many_rows_scanned` |
| `TABULAR_MAX_COLUMNS` | `256` | colonnes de la table | `too_many_columns` |
| `TABULAR_MAX_FILTERS` | `16` | filtres d'un plan | `too_many_filters` |
| `TABULAR_MAX_RESULT_ROWS` | `1000` | lignes rendues | `too_many_result_rows` |
| `TABULAR_MAX_GROUPS` | `1000` | groupes (opération `group_by` **différée**) | `too_many_groups` |
| `TABULAR_MAX_QUERY_SECONDS` | `10` | durée d'exécution DuckDB | `query_timeout` |
| `TABULAR_MAX_MEMORY_MB` | `512` | `memory_limit` DuckDB | `memory_exceeded` |
| `TABULAR_MAX_CANDIDATE_TABLES` | `50` | tables candidates à la sélection | `too_many_candidate_tables` |
| `TABULAR_MAX_CANDIDATE_COLUMNS` | `512` | colonnes candidates à la résolution | `too_many_candidate_columns` |

La limite d'un plan est **toujours** bornée par le plafond serveur : `compile_plan`
applique `min(plan.limit, max_result_rows)`. Un plan ne peut pas demander plus de lignes
que la configuration n'en autorise, même si la validation sémantique était contournée.

Détail des variables : [variables-environnement.md](../10-reference/variables-environnement.md).

---

## 5. Ce que le moteur ne fait jamais

- Il ne **recalcule jamais** un classeur Excel. Une formule sans valeur en cache fait
  refuser les opérations de calcul (`formula_result_unavailable`).
- Il ne convertit **jamais** une valeur invalide en zéro, en chaîne vide ou en date par
  défaut. Zéro est une valeur, pas un aveu d'ignorance.
- Il ne **tranche jamais** une ambiguïté par position (première table, première colonne,
  première ligne d'un ex æquo) : il demande une clarification.
- Il ne convertit **jamais** une erreur en résultat vide.
- Il n'exécute **aucun** SQL, Python ou expression provenant de l'extérieur.

---

## Références d'implémentation

- `backend/apps/rag_core/tabular/plan_schema.py` (schéma fermé, `extra="forbid"`, `Literal`)
- `backend/apps/rag_core/tabular/plan_compiler.py` (`column_alias`, `_OPERATOR_SQL`, `_AGGREGATE_SQL`)
- `backend/apps/rag_core/tabular/executor.py` (`_connect`, `_ddl_create_table`, `_Timeout`)
- `backend/apps/rag_core/tabular/plan_validator.py` (ordre des contrôles)
- `backend/apps/rag_core/tabular/permissions.py` (filtrage à la source)
- `backend/apps/rag_core/tabular/errors.py` (codes stables)
- `backend/apps/rag_core/tabular/config.py` (bornes lues une seule fois)
- `backend/apps/workspaces/services/access.py` (`require_rag_space_access`)
- `backend/api/v1/tabular/views.py` (mapping statut → HTTP)
- `backend/config/settings/base.py` (bloc `TABULAR_*`)
- Tests : `tests/tests_root/rag_core/test_tabular_engine.py`, `tests/tests_root/rag_core/test_tabular_integration.py`
