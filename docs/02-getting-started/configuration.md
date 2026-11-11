# Configuration minimale

Objectif : les quelques variables nécessaires pour faire tourner RagATM en local. Public :
développeuses et développeurs. Le code (`config/settings/`) fait foi. La référence
exhaustive vit dans [../10-reference/variables-environnement.md](../10-reference/variables-environnement.md)
et [../10-reference/feature-flags.md](../10-reference/feature-flags.md) — cette page ne
reprend que le strict nécessaire.

## Sélection de l'environnement

- `DJANGO_ENV=dev` sélectionne les settings de développement (`config.settings.dev`,
  `DEBUG=True`, `ALLOWED_HOSTS=["*"]` toléré). `up.sh` l'exporte automatiquement.
- En dev, le fichier **`.env`** à la racine du dépôt est **chargé automatiquement**. Hors
  dev, il faut `LOAD_DOTENV=1` pour le charger (staging/prod passent par `--env-file`).
- Point de départ : `cp .env.example .env`, puis renseigner les valeurs locales. Le
  `.env.example` ne contient **aucun secret réel** (placeholders `change-me`).

## Base de données

Défauts de code (alignés sur le compose dev) :

| Variable | Défaut code | En dev |
|---|---|---|
| `DB_NAME` | `ragatm` | `ragatm` |
| `DB_USER` | `ragatm` | `ragatm` |
| `DB_PASSWORD` | `ragatm` | placeholder dev |
| `DB_HOST` | `localhost` | `127.0.0.1` (exporté par `up.sh`) |
| `DB_PORT` | `5432` | `65432` (port publié par le compose dev) |

`up.sh` force `DB_HOST=127.0.0.1` et `DB_PORT=65432` pour coller au Postgres dockerisé,
en priorité sur le `.env`.

## Provider d'embeddings

- Défaut **opérationnel** (`.env.example`) : `EMBEDDINGS_PROVIDER=ollama` (routé via l'AI
  Gateway, dimension **768**, modèle `nomic-embed-text`).
- Défaut **de code** : `stub`. Le `stub` est l'**escape hatch tests/CI** (embeddings
  déterministes, sans réseau) — pratique pour un dev hors-ligne, mais le RAG est alors
  **dégradé** (pas de vraie sémantique).
- **La production refuse `stub` (et la valeur vide) au démarrage** (`config/settings/prod.py`).

Pour un dev entièrement hors-ligne, `EMBEDDINGS_PROVIDER=stub` suffit ; pour des réponses
réalistes, utiliser `ollama` via la Gateway. La dimension `RAG_EMBEDDING_DIM=768` doit
correspondre à la colonne pgvector (sinon `rag_core.E001`, cf. [depannage.md](depannage.md)).

## AI Gateway en dev

L'IA de RagATM passe **obligatoirement par l'AI Gateway** (directe ou via
AI Gateway Pool) : `AI_GATEWAY_ENABLED=1` route la
génération vers `AI_GATEWAY_BASE_URL` (p. ex. `http://ai-gateway:8100`). L'appel Ollama
direct est interdit.

Pour un dev **sans** Gateway (tests, poste sans accès à une Gateway), le défaut de code
`AI_GATEWAY_ENABLED=0` déclenche le system check `llm_router.E001`. L'échappatoire est
`ALLOW_AI_GATEWAY_DISABLED_FOR_TESTS=1`, qui dégrade E001/E003 en simple avertissement —
**réservé aux tests, jamais en environnement réel**. Détails et politique complète :
[../06-ai/ai-gateway.md](../06-ai/ai-gateway.md).

Il n'existe aucun provider externe ni repli : si la Gateway échoue, l'erreur est contrôlée.
`RAG_LLM_PROVIDER` (défaut `ollama`) ne désigne que le chemin direct de développement : Gateway
active, il est sans effet ; Gateway éteinte en environnement officiel, `llm_router.E003` refuse le démarrage.

## Frontend et proxy

| Variable | Rôle | Valeur dev |
|---|---|---|
| `FRONTEND_APP_ORIGIN` | Source **unique** des liens email (invitation / reset). `up.sh` l'aligne sur le port front. | `http://127.0.0.1:6300` |
| `BACKEND_BASE_URL` | Cible du proxy SSR Next.js → backend (seule var applicative du front). | `http://127.0.0.1:6811` |

`up.sh` exporte aussi `DJANGO_CSRF_TRUSTED_ORIGINS` avec les origines dev (front + back)
pour que l'authentification par cookie + CSRF fonctionne.

## Récapitulatif — `.env` minimal en dev

```env
DJANGO_ENV=dev
DJANGO_SECRET_KEY=change-me
DB_NAME=ragatm
DB_USER=ragatm
DB_PASSWORD=change-me
# Hors-ligne / tests :
EMBEDDINGS_PROVIDER=stub
# ou, pour un RAG réaliste via la Gateway :
# EMBEDDINGS_PROVIDER=ollama
# AI_GATEWAY_ENABLED=1
# AI_GATEWAY_BASE_URL=http://ai-gateway:8100
```

`up.sh` fournit déjà `DB_HOST`/`DB_PORT`/`FRONTEND_APP_ORIGIN`/`BACKEND_BASE_URL`, donc en
pratique un `.env` peut se limiter à `DJANGO_SECRET_KEY` et au choix du provider
d'embeddings.

Tables complètes : [../10-reference/variables-environnement.md](../10-reference/variables-environnement.md).
Flags : [../10-reference/feature-flags.md](../10-reference/feature-flags.md).

## Références d'implémentation

- `backend/config/settings/base.py`
- `backend/config/settings/dev.py`
- `backend/config/settings/prod.py`
- `backend/apps/llm_router/checks.py`
- `backend/apps/rag_core/apps.py`
- `.env.example`
- `scripts/dev/up.sh`
