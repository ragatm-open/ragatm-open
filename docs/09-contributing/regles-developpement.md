# Règles de développement

Conventions pour contribuer au code et à la documentation de RagATM. Public : contributeurs.
Le CODE est la source de vérité ; cette page décrit comment le garder cohérent.

Voir aussi [../08-testing/strategie.md](../08-testing/strategie.md),
[roadmap.md](roadmap.md), [../10-reference/commandes.md](../10-reference/commandes.md).

---

## Organisation du code

### Backend (Django + DRF)

- **Apps** sous `backend/apps/` : `accounts`, `workspaces`, `rag_core`, `chat`, `corpus`,
  `audit`, `llm_router` (+ apps « plain package » hors `INSTALLED_APPS` : `actions`, `help`).
  Les apps `documents` et `billing` sont des placeholders vides — ne pas y ajouter de logique
  documentaire (elle vit dans `api/v1/documents/`).
- **Couche HTTP** : tout l'API v1 est sous `backend/api/v1/<domaine>/`, distincte des apps.
  Les URLs utilisent des **view factories paresseuses** (import de la vue dans la fonction)
  pour éviter les cycles d'import — respecter ce motif.
- **Découpage en couches** dans `apps/rag_core` : `services/` (orchestration), `domain/`
  (règles pures : retrieval, ranking, chunking, policies, coverage), `repositories/` (SQL/DB),
  `core/` (façade RagATM_Core). Garder les règles métier pures dans `domain/`, l'I/O DB dans
  `repositories/`, l'orchestration dans `services/`. Les façades `query_facade.py`/
  `ingest_facade.py` sont des shims de compat — ne pas y ajouter de logique.
- **Isolation multi-tenant** : toute requête RAG filtre par `rag_space_id` (dupliqué sur
  `Chunk`/`Summary` comme verrou anti-fuite). Passer par les primitives
  `apps/workspaces/services/access.py` (`require_workspace_access`, `require_rag_space_access`).
  Anti-énumération : renvoyer **404** (pas 403) sur absence d'accès en lecture. Voir
  [../03-backend/permissions-isolation.md](../03-backend/permissions-isolation.md).

### Frontend (Next.js App Router)

- UI sous `frontend/src/app/` ; code privé de route via convention `_components/`/`_lib/`.
- Le navigateur parle toujours au **proxy same-origin** `/api/[...path]` ; seule var
  applicative : `BACKEND_BASE_URL` (server-only). Pas de `NEXT_PUBLIC_*`. Voir
  [../04-frontend/frontend.md](../04-frontend/frontend.md).

---

## Migrations & pgvector

- Migrations Django classiques. La colonne d'embeddings est un `VectorField(dimensions=768)`
  **figé** par la migration `0001` (+ `CREATE EXTENSION vector`).
- **La dimension est gelée** : la changer impose une nouvelle migration **et** un réembedding
  complet (`manage.py reembed_documents --rag-space <uuid> --sync`). Le system check
  `rag_core.E001` refuse le démarrage en cas de divergence `RAG_EMBEDDING_DIM` ↔ colonne.
- Index : HNSW cosine (`0014`), GIN FTS `simple` (`0014`) + français (`0023`).

---

## Discipline de test

- **Suite backend unique** (`tests/tests_root`) : lancer `make test-backend-all`.
- Les tests forcent `EMBEDDINGS_PROVIDER=stub` (déterministe, non sémantique) et
  `AI_GATEWAY_ENABLED=0` + `ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=1` (checks IA verts sans
  réseau). La recherche vectorielle est donc validée sur son **contrat technique**, pas sur
  la pertinence sémantique — les cas no-answer reposent sur des espaces vides/isolation.
- **Toute nouvelle fonctionnalité ou correctif ajoute des tests** dans l'arbre pertinent.
  PostgreSQL + pgvector est requis pour les suites `integration/`/`http_api/`/`rag_core/`.
  `APPEND_SLASH=False` → toutes les URLs finissent par `/`.
- Frontend : Vitest + Testing Library (`make test-frontend`). Détails et commandes exactes :
  [../08-testing/strategie.md](../08-testing/strategie.md).

---

## Règles de documentation

> **Policy — doc à jour à chaque commit.** Tout changement de code modifiant un comportement,
> une API, une variable d'environnement, un flag, une commande, un port ou un flux **doit être
> répercuté dans `docs/` dans le même commit**. Appliquée par le hook git pre-commit
> (`.githooks/pre-commit`, activation `make hooks`) et par la garde CI
> (`.github/workflows/docs-check.yml`). Cf. `CLAUDE.md`.
>
> La régénération automatique de prose n'étant pas fiable, l'automatisation garantit la
> **cohérence** (liens/références) et **rappelle** la mise à jour ; la rédaction reste manuelle.

- **Français** partout, terminologie canonique stable (workspace, RagSpace, chunk, retrieval,
  answerability, AI Gateway, corpus métier, no-answer…).
- **Le code prévaut.** Ne jamais présenter une fonctionnalité future/partielle comme
  disponible : utiliser les étiquettes de statut (implémenté / activé par défaut / activable
  par flag / expérimental / benchmarké / planifié / non branché).
- **Référence à source unique** : les valeurs (variables d'env, flags, ports, commandes,
  limites) vivent dans `docs/10-reference/`. Les autres docs **lient** vers ces pages plutôt
  que de dupliquer les tableaux.
- **Aucun secret** : noms de variables et placeholders seulement, jamais de valeur réelle.
- Chaque doc technique majeure se termine par une section `## Références d'implémentation`
  listant les chemins repo-relatifs.
- Liens **relatifs** valides ; vérifier avant de committer avec :

  ```bash
  python scripts/check_docs.py            # liens cassés (bloquant) + orphelins/doublons (avertissements)
  python scripts/check_docs.py --strict   # les avertissements deviennent bloquants
  ```

  Le script signale les liens Markdown cassés, les fichiers/chemins de code référencés
  inexistants, les documents orphelins (non liés) et les titres H1 en doublon. Le dossier
  `docs/archive/` (figé) est exclu par défaut (`--include-archive` pour l'inclure).

---

## Commits & CI

- La CI (`.github/workflows/ci.yml`) exécute `make test-backend-all` (suite **standard**) + vitest +
  `tsc --noEmit`, sur chaque push et chaque PR.
- `docs-check.yml` rejoue `scripts/check_docs.py` (cohérence documentaire) et
  `rag-benchmark.yml` bloque les PR touchant le retrieval si le compare régresse
  (decision-k=5, filtré par chemins). Les deux avaient été **désactivés côté GitHub** ; ils
  ont été **réactivés**, et `rag-benchmark.yml` réparé — il lui manquait les variables de la
  policy AI Gateway, apparue après lui, et `manage.py check` échouait donc au démarrage.
- Les workflows de benchmark sémantique ont été **supprimés** : jamais réussis, ils
  visaient un runner self-hosted `[ollama]` qui n'existe pas. Voir
  [../08-testing/strategie.md](../08-testing/strategie.md).
- **Activez le hook git une fois par clone** : `make hooks` (installe `.githooks/pre-commit`,
  qui vérifie la doc et rappelle la policy « doc à jour à chaque commit »).
- ⚠️ La CI **n'applique pas** ruff/black/mypy/`manage.py check` : lancer ces contrôles
  localement avant de committer (voir [../10-reference/commandes.md](../10-reference/commandes.md)).
- Mettre à jour une **baseline de benchmark** uniquement en local, jamais depuis la CI, et
  justifier le changement dans la PR (raison, commit source, métriques avant/après).

### ⛔ `gh pr edit` et `gh pr merge` sont interdits sur ce dépôt

Les deux commandes **échouent silencieusement** : elles renvoient l'erreur GraphQL
« Projects (classic) is being deprecated » (`repository.pullRequest.projectCards`) et
**n'appliquent pas la modification**, tout en paraissant réussir. Elles sont bloquées par une
règle `permissions.deny` dans `.claude/settings.json` (versionné).

Un `gh pr edit --base main` ainsi avalé a laissé une PR empilée ciblant la branche de sa
parente ; le `gh pr merge` suivant a fusionné le lot **dans la mauvaise branche**. GitHub
interdisant de rouvrir une PR fusionnée, il a fallu recréer une PR de remplacement.

Utiliser l'**API REST**, qui n'emprunte pas le chemin GraphQL fautif :

```bash
gh api repos/:owner/:repo/pulls/<n> -X PATCH -f base=main          # re-cibler la base
gh api repos/:owner/:repo/pulls/<n> -X PATCH --input -             # modifier le corps (JSON sur stdin)
gh api repos/:owner/:repo/pulls/<n>/merge -X PUT -f merge_method=merge
```

**Toujours vérifier la base juste avant de fusionner** — `gh api repos/:owner/:repo/pulls/<n> -q '.base.ref'`
— et ne fusionner que si elle vaut bien la cible attendue.

---

## Références d'implémentation

- `backend/apps/`, `backend/api/v1/`, `backend/apps/workspaces/services/access.py`
- `backend/apps/rag_core/{services,domain,repositories,core}/`
- `Makefile`, `backend/pyproject.toml`, `.github/workflows/`
- `frontend/src/app/`
