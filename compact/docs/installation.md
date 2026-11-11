# Installation

## 1. Prérequis

- Linux x86_64, Docker ≥ 24 avec Compose v2 (testé : Docker 29.6.0, Compose v5.2.0).
- Mémoire : 14 Go de RAM au total sur le poste de qualification ; la pile occupe ~8 Go en
  charge (reranker TEI ≈ 3,8 Go, Ollama ≈ 2 Go avec le 1.5 B et l'encodeur résidents, services
  Django/Celery ≈ 1,5 Go). En dessous de 10 Go disponibles, réduire `COMPACT_MEM_*` et attendre
  des échecs mémoire sur le reranker.
- Disque : ~3 Go d'images + ~4 Go de poids de modèles + données. Le poste de qualification
  n'avait que 8 Go libres : la construction des images a dû être suivie d'un nettoyage du
  cache de build (`docker builder prune`) avant l'approvisionnement des modèles.
- Aucun GPU requis pour le profil CPU `compact-generation-8`. Le profil 12 Go est décrit mais
  **non qualifié** (voir [limites-et-capacites.md](limites-et-capacites.md)).
- Accès Internet **uniquement** pour la phase d'approvisionnement (images de base, dépendances
  Python/npm, modèles). L'exécution nominale n'en a pas besoin et les réseaux internes l'interdisent.

Il n'existe **pas** d'installation « en une commande » : l'approvisionnement des modèles et la
génération des secrets sont des étapes explicites et préalables.

## 2. Préflight

```bash
cd ragatm-open/compact   # le kit vit dans le dépôt ragatm-open ; cloner ai-gateway-open à côté de ragatm-open
scripts/preflight.sh
```

Les contextes de build `backend` et `frontend` sont résolus depuis `RAGATM_OPEN_DIR` (défaut `..` : la racine de ce dépôt) ;
le contexte `ai-gateway` depuis `AI_GATEWAY_OPEN_DIR` (défaut `../../ai-gateway-open` : un clone de
[ai-gateway-open](https://github.com/ragatm-open/ai-gateway-open) placé à côté de `ragatm-open/`). Pour une autre disposition, poser ces
variables dans `.env`. Le Pool n'est pas requis (déploiement **Direct**) ; il peut être ajouté séparément.

Vérifie Docker/Compose, RAM, disque, le port du frontend (défaut `127.0.0.1:18080`), la présence
et les permissions de `.env`, l'absence de valeurs de démonstration.

## 3. Secrets

```bash
scripts/gen-secrets.sh          # crée .env (0600) depuis .env.example
```

Génère `DJANGO_SECRET_KEY`, `DB_PASSWORD`, `REDIS_PASSWORD`, `RAG_OPS_METRICS_TOKEN` et les URL
Redis correspondantes. Le script refuse d'écraser un `.env` existant (`--force` pour régénérer,
ce qui impose de recréer la base et Redis). Aucun secret n'est présent dans les images, les
Dockerfile, les arguments de build ni `.env.example`.

## 4. Construction des images

```bash
docker compose build            # backend, frontend (Next.js), ai-gateway
```

Les images de base et tierces sont épinglées par digest (`compose.yml`) : `pgvector/pgvector:pg16`,
`redis:7.2-alpine`, `ollama/ollama` 0.30.8, TEI CPU 1.8. Les poids des modèles ne sont
jamais dans une image.

## 5. Approvisionnement des modèles

Voir [approvisionnement-modeles.md](approvisionnement-modeles.md) :

```bash
scripts/provision-models.sh     # granite-embedding:278m, qwen2.5:1.5b-instruct, bge-reranker-v2-m3
```

## 6. Démarrage et premier compte

```bash
scripts/up.sh
RAGATM_ADMIN_EMAIL=admin@example.org RAGATM_ADMIN_PASSWORD='<mot de passe ≥ 12 caractères>' scripts/bootstrap-admin.sh
```

Ouvrir `http://localhost:18080/login`. La première connexion enrôle obligatoirement un second
facteur TOTP (application d'authentification) ; aucun compte ne peut s'en passer.

Les cookies posés par le backend portent l'attribut `Secure` (profil `prod`) : les navigateurs
courants les acceptent sur `http://localhost` et `http://127.0.0.1` (origines dites de confiance).
Derrière un reverse-proxy TLS, renseigner `FRONTEND_APP_ORIGIN` et `DJANGO_CSRF_TRUSTED_ORIGINS`.

## 7. Parcours réellement testé

Le parcours de qualification exécuté par `scripts/e2e_qualification.py` (via le proxy Next,
exactement comme un navigateur) : préflight → secrets → build → approvisionnement → démarrage →
authentification + TOTP → bootstrap → création d'espace → import de 8 documents synthétiques
(TXT, MD, PDF 14 pages, DOCX) → ingestion asynchrone par le worker → embeddings réels à identité
vérifiée (`fr-en-retrieval`) → recherche hybride → reranking réel (TEI) → génération réelle
(`compact-generation-8`) → flux SSE → citations → suppression/réimport → isolation inter-espaces
et inter-utilisateurs. Résultats : dossier de qualification interne (non distribué).

## Exécution non privilégiée des conteneurs

Les images `backend`, `worker`, `beat` s'exécutent sous l'utilisateur `app` (UID/GID 10001), `frontend` sous `node`
(UID 1000), `ai-gateway` sous `appuser`. Le volume `media` est créé avec la bonne propriété lors d'une première
installation. Pour une installation antérieure (volume `media` appartenant à root), réattribuer une fois :

```bash
docker compose run --rm --user 0 --no-deps --entrypoint sh backend -c 'chown -R 10001:10001 /data/media'
```
