# Premier lancement

Objectif : de la stack qui démarre à une première question répondue. Public : nouvel
utilisateur ou développeur qui valide son installation. Le code fait foi. Prérequis :
[developpement-local.md](developpement-local.md) et [configuration.md](configuration.md).

Si vous avez lancé `./scripts/dev/up.sh` sur une base vide, le superutilisateur, le
workspace et le RagSpace de test sont **déjà créés** (étape bootstrap du script). Les
sections ci-dessous décrivent chaque étape pour la comprendre ou la refaire à la main.

## 1. Créer un superutilisateur

Deux voies équivalentes :

```bash
# Option A — script dev idempotent (crée ou réinitialise l'admin dev)
./scripts/dev/set_admin.sh

# Option B — commande Django standard
cd backend
source ../.venv/bin/activate
python manage.py createsuperuser
```

`set_admin.sh` crée ou réinitialise un admin de développement (email / mot de passe
surchargeables par variables d'env ; le mot de passe n'est jamais affiché ni passé en
argument) et refuse tout signal prod/staging avant écriture.

> **Propriétaire global RagATM = `is_superuser`.** Le superutilisateur détient la
> permission `IsRagATMOwner` : il gère notamment le corpus métier partagé. Un compte
> applicatif normal n'a pas ce rôle. Voir
> la documentation d'exploitation (hors périmètre de cette édition).

## 2. Obtenir un workspace et un RagSpace

Après connexion, l'endpoint de bootstrap crée automatiquement l'environnement minimal
d'un utilisateur :

```
POST /api/v1/bootstrap/
```

Comportement (idempotent) :

- si l'utilisateur n'a **aucun** workspace → création d'un workspace (rôle `owner`) et
  d'un RagSpace « Default » ;
- s'il a déjà un workspace sans RagSpace → création d'un RagSpace « Default » ;
- sinon → renvoie le workspace et le RagSpace existants les plus anciens.

La réponse a une forme stable, pensée pour être appelée automatiquement après login. En
ligne de commande, l'équivalent utilisé par `up.sh` est :

```bash
python manage.py bootstrap_workspace --email <email> --workspace WorkspaceTest --ragspace Default
```

## 3. Vérifier la santé de la stack

```bash
curl http://127.0.0.1:6811/api/health/
```

`GET /api/health/` est une **liveness technique** (utilisée par le healthcheck Docker et
le reverse-proxy) : elle exige un en-tête `Host` autorisé. Une réponse `200` confirme que
le backend répond.

## 4. Téléverser un premier document

Depuis le frontend (`http://127.0.0.1:6300/`), aller sur **`/documents`** et téléverser un
fichier. Formats acceptés : TXT, Markdown, PDF, DOCX, CSV, HTML, images, XLSX. L'endpoint
sous-jacent est :

```
POST /api/v1/documents/upload/
```

Limites par défaut : 25 Mo par fichier, 500 pages pour un PDF, 30 s de timeout
d'extraction. En dev, l'ingestion est **synchrone par défaut** (ni `RAG_INGEST_ASYNC_DEFAULT`
ni `CELERY_TASK_ALWAYS_EAGER` ne sont activés, et le compose dev ne lance pas de worker).

### À quoi ressemble « prêt »

Un document passe par les statuts techniques `PENDING → INGESTING → READY` (ou `FAILED`).
Le document est exploitable par le RAG lorsqu'il est en **`READY`** : le texte a été
extrait, découpé en chunks et vectorisé. Le frontend suit ce statut par polling. Un
document qui reste bloqué en `PENDING` ou qui tombe en `FAILED` est traité dans
[depannage.md](depannage.md).

## 5. Poser une première question

Depuis le frontend, aller sur **`/chat`**, sélectionner le RagSpace et poser une question
portant sur le document téléversé. L'endpoint est :

```
POST /api/v1/chat/
```

La réponse cite les sources retrouvées (retrieval hybride vectoriel + FTS). Si aucun
document `READY` pertinent n'est trouvé, le système renvoie une non-réponse contrôlée
plutôt qu'une réponse inventée — voir [../06-ai/answerability.md](../06-ai/answerability.md)
et le fonctionnement du pipeline dans
[../03-backend/pipeline-retrieval.md](../03-backend/pipeline-retrieval.md).

## Prochaines étapes

- Problème au démarrage ou à l'ingestion : [depannage.md](depannage.md).
- Endpoints détaillés : [../05-api/reference-endpoints.md](../05-api/reference-endpoints.md).
- Authentification (cookie HttpOnly + CSRF) : [../05-api/authentification.md](../05-api/authentification.md).

## Références d'implémentation

- `backend/config/urls.py` (healthcheck `api/health/`)
- `backend/api/v1/bootstrap/views.py`
- `backend/apps/workspaces/management/commands/bootstrap_workspace.py`
- `backend/api/v1/documents/views_write.py`
- `backend/api/v1/chat/views.py`
- `backend/apps/rag_core/models/document.py` (statuts `PENDING/INGESTING/READY/FAILED`)
- `scripts/dev/set_admin.sh`
- `frontend/src/app/documents/`, `frontend/src/app/chat/`
