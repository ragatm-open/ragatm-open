# Éligibilité documentaire au retrieval

**Statut : implémenté / activé par défaut** (Lot 2 — levée du NO-GO P0-A).

---

## 1. Invariant

> Un chunk n'est utilisable — retrieval, reranking, citation, contexte LLM, réponse
> finale — que si son document est **`READY`** et si son **RagSpace n'est pas supprimé**.

Ce n'est pas un filtre de confort : c'est un invariant de sécurité. Il n'est pas placé
derrière un feature flag, et il ne doit pas l'être.

## 2. Pourquoi une définition unique

L'audit P0-A a montré que la règle était **réécrite ou omise requête par requête** :
cinq régimes de filtrage coexistaient, et le chemin le plus exposé — le chat — était le
**moins** filtré de tous.

| Chemin | Avant | Après |
|---|---|---|
| Chat — FTS | *aucun filtre* | `READY` + espace vivant |
| Chat — vectoriel | *aucun filtre* | `READY` + espace vivant |
| Corpus métier — FTS et vectoriel | *aucun filtre* | `READY` |
| Leg temporel | `<> FAILED` (laissait passer `PENDING`/`INGESTING`) | `READY` |
| Listings documentaires (3 requêtes) | *aucun filtre* | `READY` + espace vivant |
| Moteur tabulaire | `exclude(document_status="deleted")` — **code mort** | `READY` + espace vivant |
| Analyse globale | `READY` ✅ | inchangé |

Mesuré en base de développement avant correction : **410 chunks** rattachés à un
document `FAILED` — dont un sortait **au rang #1** du leg vectoriel — et **25 847 chunks**
dans des espaces soft-deleted.

La cause n'était pas une clause oubliée : c'était l'**absence de source unique**. Cette
source est désormais `backend/apps/rag_core/domain/eligibility.py`. Toute requête qui
remonte des chunks doit en dériver.

## 3. Le filtre s'applique en SQL, avant `LIMIT`

Filtrer en Python après récupération fausserait le top-k : les candidats inéligibles
consommeraient des places dans la fenêtre, et une question dont les meilleurs chunks
sont inéligibles renverrait moins de résultats qu'elle ne le devrait — voire aucun.
Un test verrouille ce point (`test_eligibility_is_applied_before_limit`).

## 4. Réindexation — stratégie **Option A** (choisie et assumée)

Pendant une réindexation, le document passe `INGESTING` : ses anciens chunks restent en
base mais deviennent **inéligibles**. Il redevient interrogeable au passage `READY`,
après remplacement atomique.

* **Coût accepté** : indisponibilité temporaire du document pendant la réindexation.
* **Bénéfice obtenu** : l'invariant est *trivialement vrai*. Un document que le système
  déclare non exploitable ne peut pas être servi, quel que soit l'état de ses chunks.

L'Option B (versions d'index *publiée* / *en préparation*) serait sémantiquement plus
fine, mais introduirait un état supplémentaire à maintenir cohérent dans **chaque**
requête — c'est-à-dire exactement la classe de défaut que ce lot supprime.

### Conséquence sur le reaper

`recover_stale_reindex_items` ne supprime **aucun chunk**. Un document réellement
interrompu revient `READY` s'il possède un index publié (l'ancien index reste servi),
`FAILED` sinon — un `FAILED` sans chunk servi n'est pas une violation. Le statut et
l'éligibilité ne peuvent plus diverger. Seule une tentative plus ancienne que le seuil du
reaper est récupérée : une ingestion active n'est jamais rendue inéligible
(voir [reindexation-jobs.md](reindexation-jobs.md)).

> **Rappel d'exploitation** : ce reaper dépend de Celery beat, **qui n'est déployé nulle
> part** (cf. [../10-reference/limites-connues.md](../10-reference/limites-connues.md)).
> Sans lui, les documents interrompus restent `INGESTING` indéfiniment — donc inéligibles.
> L'invariant tient ; c'est la *disponibilité* qui souffre, pas la sécurité.

## 5. Extraction partielle — doctrine explicite

`extraction_status = "partial"` reste mappé sur `READY`, donc **éligible**. C'est une
décision assumée, pas un oubli :

* rendre `partial` inéligible retirerait du retrieval du contenu aujourd'hui exploité
  (**194 documents** sur la base de développement) ;
* la nuance n'est pas perdue : elle demeure dans `meta.extraction.extraction_status` et
  remonte en sous-statut dans l'API (`api/v1/documents/views_common.py`).

À réévaluer si un besoin de traçabilité plus strict apparaît — auquel cas introduire un
statut `READY_WITH_WARNINGS` distinct plutôt que d'élargir silencieusement la règle.

## 6. RagSpaces soft-deleted

La barrière est désormais **en SQL** (`s.deleted_at IS NULL`), et non plus portée par la
seule permission HTTP. C'est ce qui la rend efficace pour les appelants internes — tâche
Celery, commande de gestion, façade Core, benchmark — qui ne passent pas par une vue.

Les chunks déjà présents dans des espaces supprimés restent en base mais sont **inertes**.
Leur purge est une opération d'hygiène séparée et explicite :
`python manage.py purge_soft_deleted_ragspaces`.

## 7. Audit de conformité

```bash
python manage.py audit_retrieval_eligibility                    # lisible
python manage.py audit_retrieval_eligibility --json             # exploitable
python manage.py audit_retrieval_eligibility --fail-on-violation  # CI
python manage.py audit_retrieval_eligibility --strict-data      # zéro résidu exigé
```

La commande est **strictement en lecture** et distingue trois niveaux :

| Niveau | Signification | Fait échouer `--fail-on-violation` ? |
|---|---|---|
| **bloquant** | Invariant contredit par les données d'une façon que le code ne rattrape pas (aujourd'hui : fuite d'isolation seule) | oui |
| **résiduel** | Chunks inéligibles présents en base mais **inertes** (exclus par les requêtes) | non — sauf `--strict-data` |
| **informatif** | Signaux de qualité (chunks sans embedding, extraction partielle) | non |

Exploitation courante (contrôles périodiques, incidents, purge des espaces
supprimés) : la documentation d'exploitation (hors périmètre de cette édition).

## 8. Limites connues

* Les **sources persistées** (`ChatMessage.sources`, `CorpusAnalysisJob.result`) restent
  des instantanés figés : un document devenu inéligible reste visible dans l'historique.
  C'est délibéré — on ne réécrit pas les preuves historiques — mais l'API n'expose pas
  encore d'indicateur de validité au moment de la lecture.
* `Document.confidentiality` et `Document.document_status` demeurent des **champs
  inertes**. Le filtre mort qui s'appuyait sur le second a été retiré du moteur tabulaire.

## Références d'implémentation

- `backend/apps/rag_core/domain/eligibility.py` (définition canonique)
- `backend/apps/rag_core/repositories/chunk_repository.py` (FTS + vectoriel)
- `backend/apps/rag_core/repositories/document_repository.py` (listings)
- `backend/apps/rag_core/services/query/temporal_candidates.py` (leg temporel)
- `backend/apps/corpus/services/retrieval.py` (corpus métier)
- `backend/apps/rag_core/tabular/permissions.py` (moteur tabulaire)
- `backend/apps/rag_core/management/commands/audit_retrieval_eligibility.py`
- `tests/tests_root/rag_core/test_retrieval_document_eligibility.py` (matrice D1..D8)
