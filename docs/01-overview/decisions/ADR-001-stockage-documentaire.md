# ADR-001 — Stockage documentaire RagATM

- **Date** : 2026-07-16
- **Commit analysé** : `e8d6ed2` (branche `main`)
- **Auteur** : audit de code (le code exécuté fait foi)

## Statut

**Accepté** — le **volume local persistant** est le stockage documentaire actif de RagATM.
MinIO / S3 est **écarté** et retiré des configurations actives.

## Contexte

RagATM stocke les documents sources via deux `FileField` Django :

| Modèle | Champ | `upload_to` |
|---|---|---|
| `rag_core.Document` | `storage_file` | `rag_docs/<rag_space_id>/<document_id>/<filename>` |
| `corpus.DomainCorpusDocument` | `storage_file` | `corpus_docs/<corpus_id>/<document_id>/<filename>` |

Ces fichiers sont les **originaux** : ils ne sont pas reconstructibles et sont nécessaires à
la réindexation. Tout le reste (chunks, embeddings) vit en base.

L'infrastructure suggérait pourtant un stockage objet : un service `minio`, un `minio-init`
créant un bucket, un volume `minio_data`, des variables `MINIO_*`. Cette ambiguïté a
directement induit la documentation en erreur et fait perdre du temps au lot sauvegarde.

## Options étudiées

### Option A — Volume local persistant (FileSystemStorage)

| Critère | Évaluation |
|---|---|
| Simplicité | **favorable** — c'est le comportement actuel, zéro intégration |
| Maturité dans le projet | **favorable** — en production, éprouvé |
| Performances | **favorable** — accès disque direct, pas de réseau |
| Sauvegarde / restauration | **favorable** — couvert et **prouvé** par le lot 3 (14 995 fichiers restaurés, checksums exhaustifs) |
| Partage backend ↔ workers | **favorable** — volume Docker nommé monté sur `backend`, `worker`, `worker_analytics` |
| Multi-instance / multi-hôte | **défavorable** — impose un hôte unique (ou un partage réseau) |
| Reprise sur un autre hôte | neutre — via la restauration de sauvegarde |
| Permissions / exposition | **favorable** — aucun alias Nginx ; téléchargement par endpoint Django autorisé |
| Chemins divergents | **défavorable** — risque réel (constaté, cf. §Conséquences) |
| Capacité / monitoring | neutre — dépend du disque hôte, à surveiller |

### Option B — MinIO / S3

| Critère | Évaluation |
|---|---|
| Besoin réel démontré | **bloquant** — aucun : pas de multi-instance, pas de multi-hôte, pas de besoin CDN |
| Coût d'intégration | **défavorable** — `django-storages` + `boto3` **absents** des dépendances : tout est à faire |
| Compatibilité `FileField` | favorable (django-storages est mature) |
| Migration des fichiers existants | **défavorable** — outil idempotent + vérification + coexistence à construire |
| URLs signées / contrôle d'accès | neutre — remplacerait un mécanisme Django **déjà sûr et en place** |
| Isolation buckets staging/prod | neutre — faisable, mais ajoute des credentials à gérer |
| Indisponibilité MinIO | **défavorable** — nouveau **service critique** : sans lui, plus d'upload ni de téléchargement |
| Sauvegarde / restauration objet | **défavorable** — le dispositif du lot 3 (prouvé) serait à réécrire (`mc mirror`, ETags) |
| Cohérence base ↔ objet | défavorable — mêmes problèmes qu'en local, sans gain |
| Exploitation supplémentaire | **défavorable** — TLS, credentials, CORS, rétention, monitoring d'un service de plus |

## Décision

**Conserver le volume local persistant** comme unique stockage documentaire actif.
**Retirer MinIO** des configurations actives de RagATM (service, `minio-init`, volume, ports,
variables), sans supprimer aucun volume sur les hôtes.

## Justification

1. **Aucun besoin réel démontré.** RagATM est déployé sur un hôte applicatif unique par
   environnement ; backend et workers partagent déjà le volume. Le multi-instance n'est ni
   prévu ni contraint.
2. **MinIO n'est pas utilisé — c'est une preuve, pas une opinion** : aucune dépendance
   `django-storages`/`boto3`, **zéro** lecture de `MINIO_*`/`AWS_*` dans le code backend, et
   les `FileField` écrivent sur `MEDIA_ROOT`. Le service tourne à vide.
3. **Coût net négatif.** MinIO ajouterait un service critique, une migration, une réécriture
   du dispositif de sauvegarde (déjà validé), sans bénéfice immédiat.
4. **La sécurité est déjà correcte en local** : pas d'alias `/media/` dans Nginx, téléchargement
   via endpoint Django avec contrôle de permissions, `Content-Disposition: attachment`,
   `X-Content-Type-Options: nosniff`, CSP sandbox. Passer à des URLs présignées remplacerait
   un mécanisme sûr par un autre à sécuriser.
5. **La sauvegarde locale est prouvée** (lot 3) : restauration réelle de 14 995 fichiers,
   100 % des checksums concordants, cohérence base → fichiers vérifiée.
6. **La configuration morte nuit activement** : elle a fait croire à un stockage objet et
   pollué la documentation. Une architecture suggérée mais non câblée est un piège.

## Conséquences positives

- Une **seule** stratégie de stockage, cohérente entre code, compose, sauvegarde et doc.
- Moins de services (RAM, disque, surface d'attaque, credentials en moins).
- Le dispositif de sauvegarde/restauration reste valide **sans modification**.
- Suppression d'une source d'erreur documentaire durable.

## Conséquences négatives

- **Hôte unique** : le stockage est lié à la machine. Un scale-out horizontal du backend
  imposerait un partage réseau (NFS) ou une bascule S3 — à réévaluer le moment venu.
- La **capacité disque** devient un point de surveillance explicite (croissance des médias).
- La reprise sur un autre hôte passe par la restauration de sauvegarde (RTO 4 h), pas par un
  simple repointage.

## Risques

| Risque | Portée | Mitigation |
|---|---|---|
| **Chemins divergents** entre backend/worker/staging/prod | **avéré** — un staging ne définissait pas `MEDIA_ROOT` : Django écrivait dans `/backend/media` (couche conteneur) au lieu du volume `/app/backend/media` → documents perdus au redéploiement et **worker incapable de lire** le fichier écrit par le backend | `MEDIA_ROOT` **forcé** dans `environment:` de tous les services montant le volume + défaut `prod.py` aligné + **test anti-divergence** |
| Saturation disque | moyen | surveillance (cf. la documentation d'exploitation), croissance des médias |
| Perte de l'hôte | élevé | sauvegarde quotidienne + restauration testée (lot 3) |
| Fichiers orphelins | faible | commande d'audit `audit_media_storage` (rapport seul) |

## Conditions de réévaluation

Rouvrir cette décision si **l'une** de ces conditions apparaît :

1. **Multi-instance backend** (plusieurs hôtes servant la même base) → le volume local ne
   peut plus être partagé sans NFS.
2. **Volumétrie** dépassant durablement la capacité du disque hôte, ou coût de stockage
   devenant significatif.
3. **Exigence de réplication géographique** ou de durabilité supérieure à celle d'un disque
   unique + sauvegarde quotidienne.
4. **Besoin de distribution directe** des fichiers (CDN, URLs signées consommées par des
   tiers).
5. RPO exigé **< 24 h** sur les fichiers (le stockage objet répliqué deviendrait pertinent).

## Plan de migration éventuel (si l'option B redevenait justifiée)

Non implémenté. Esquisse, à instruire dans un lot dédié :

1. Ajouter `django-storages` + `boto3` **verrouillés** ; configurer `STORAGES` (média privé
   séparé du statique).
2. Créer des buckets **distincts par environnement**, jamais publics, credentials séparés.
3. Écrire `migrate_media_to_object_storage` : idempotent, `--dry-run`, checksum
   source/destination, reprise, journal, **aucun effacement local automatique**.
4. **Coexistence** : copier et vérifier, puis basculer la configuration, valider, et seulement
   ensuite envisager la suppression locale (lot séparé).
5. Adapter le dispositif de sauvegarde (`mc mirror`, comptage d'objets, restauration vers
   bucket temporaire, cohérence base → objets).
6. Définir le **mode dégradé** (MinIO indisponible : upload refusé proprement, aucun fichier
   silencieusement perdu).
7. Garder le retour au `FileSystemStorage` possible et **testé** avant toute suppression des
   fichiers locaux.

## Références d'implémentation

- `backend/config/settings/base.py`, `prod.py` (`MEDIA_ROOT`, `MEDIA_URL`)
- `backend/apps/rag_core/models/document.py`, `backend/apps/corpus/models/document.py` (`FileField`)
- `backend/api/v1/documents/views_read.py` (téléchargement autorisé, en-têtes de sécurité)
- `deploy/docker/docker-compose.yml` + overrides (volume `media`)
- `scripts/backup/` (sauvegarde des médias), `docs/07-operations/sauvegarde-restauration.md`
