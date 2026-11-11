# Ingestion des courriels et de leurs pièces jointes (.eml)

*Statut : **activable par flag**, fermé par défaut ; **disponibilité générale** par
`DOCUMENT_EMAIL_GLOBAL_ENABLED`, sinon borné par espace.*
Valeurs et défauts : [feature-flags](../10-reference/feature-flags.md),
[variables d'environnement](../10-reference/variables-environnement.md). Limites :
[limites connues](../10-reference/limites-connues.md).

Un courriel MIME (`.eml`) est ingéré comme **un document** : son
en-tête (objet, expéditeur, date), son corps, et — si l'espace l'autorise — le **contenu de
ses pièces jointes**, découpé par les extracteurs existants. Le contenu d'une pièce jointe est
retrouvable comme n'importe quel document, et chaque citation dit **de quel courriel** et
**de quelle pièce** elle vient, puis où dans la pièce (page, diapositive, feuille/plage).

## Architecture

```text
.eml ──► validate_and_classify (type réel, porte de format)
            ──► check_email_admission (porte d'ESPACE, au dépôt)
            ──► EmlExtractor                       (processus d'extraction isolé)
                  ├─ en-tête + corps (sélection qualitative html > texte > rtf)
                  └─ pièces jointes ──► validate_and_classify (chaque pièce)
                                    ──► extracteur du registre (PDF, DOCX, PPTX, XLSX, CSV,
                                         TXT/MD, HTML, images, courriel imbriqué…)
            ──► indexation : rail email (corps) + `_rail_specs` de chaque pièce
            ──► publication ATOMIQUE existante (fragments + embeddings du RagSpace)
```

**Décision** : une pièce jointe est un **sous-document logique** du courriel, pas un
`Document` persistant. Ses fragments appartiennent au document courriel et portent leur
provenance. L'alternative (documents enfants) a été écartée :

| Critère | Sous-document logique (retenu) | Document enfant |
|---|---|---|
| Contrainte `(rag_space, sha256)` | intacte : le même PDF joint à deux courriels donne deux jeux de fragments, chacun rattaché à son message | collision → contrainte globale à modifier |
| Droits | ceux du courriel, par construction | à recopier et maintenir |
| Suppression | cascade des fragments du courriel | cascade à écrire, risque d'orphelins |
| Réindexation | publication atomique existante (tout ou rien) | multi-documents, non atomique |
| Relance Celery | idempotente (aucune création) | dédoublonnage à garantir |
| Stockage | aucun fichier en plus | un fichier par pièce |

Contrepartie : une pièce n'a pas d'aperçu ni de téléchargement propres (le courriel parent
se télécharge, sous ses droits), et le **moteur de calcul tabulaire** ne s'applique pas à un
classeur joint (il lit le fichier source d'un `Document`).

## Formats

| Format | Parseur | Corps | Pièces |
|---|---|---|---|
| `.eml` | bibliothèque standard `email` (politique `default`) | `text/html` / `text/plain` (`multipart/alternative`) | `multipart/mixed`, ressources `multipart/related` (inline, CID), `message/rfc822` |

`.eml` : en-têtes RFC 5322 décodés (RFC 2047), noms RFC 2231, base64 / quoted-printable,
charsets MIME (charset inconnu → décodage tolérant **signalé** `eml_charset_fallback`).
Le texte et le HTML d'un même corps sont deux **candidats** : un seul est indexé. Le HTML
est nettoyé par `htmltext` (script/style/iframe supprimés avec leur contenu) ; aucune
ressource distante n'est chargée, aucun lien suivi. Détection : extension `.eml` **et**
bloc d'en-têtes RFC 5322 (`eml_signature`) — un `.txt` au contenu de courriel reste du texte.
Avant ce lot, un `.eml` était indexé comme **texte brut** (en-têtes et base64 compris) ;
porte fermée, il est désormais **refusé** (`eml_ingestion_disabled`).

## Pièces jointes

Chaque pièce passe par les **mêmes primitives qu'un dépôt** : `validate_and_classify`
(type réel par contenu, taille, refus explicites, portes de format de l'environnement),
puis l'extracteur du registre. Un format fermé sur l'environnement **reste fermé** dans un
courriel. Formats à worker dédié (IFC, DWG) : `UNSUPPORTED`
(`email_attachment_requires_dedicated_worker`). Archives : jamais décompressées.

Traitement **séquentiel**, dans le processus d'extraction du courriel (même budget de
temps : aucune nouvelle pièce n'est ouverte au-delà de 60 % du budget,
`email_time_budget_exhausted`). Bornes : nombre, taille unitaire, volume cumulé (partagé
avec les imbriqués), profondeur. Les octets d'une pièce vivent dans des `AttachmentPayload`
**transitoires**, jamais persistés, jamais portés par l'inventaire (`EmailAttachment`).

| Statut | Sens |
|---|---|
| `INDEXED` | fragments indexés, interrogeables |
| `PARTIALLY_INDEXED` | extraction partielle (troncature, pages perdues), fragments indexés |
| `UNSUPPORTED` | format non pris en charge (archive, objet OLE, worker dédié…) |
| `REJECTED` | refus de sécurité (exécutable, macros, type incohérent) |
| `NO_EXTRACTABLE_TEXT` | pièce **lue** mais sans texte exploitable : image sans texte (`ocr_no_text` sur une image : logo, visuel décoratif), fichier vide (`empty_document`), fichier lu sans contenu (au moins une page). Ni une erreur, ni un contenu indexé |
| `FAILED` | vraie erreur : fichier corrompu ou illisible (`email_attachment_unreadable`, `image_corrupted`…), scan PDF sans texte (`ocr_no_text` sur un PDF), délai dépassé, dépendance absente, aucun fragment retenu |
| `SKIPPED_LIMIT` | limite atteinte (nombre, taille, volume, profondeur, temps, cycle) |
| `DUPLICATE_IN_EMAIL` | mêmes octets qu'une autre pièce du message : indexée une fois, `same_as` pointe l'autre |
| `NOT_INGESTED` | ingestion des pièces fermée pour cet espace (inventaire seul) |

Une pièce n'est **jamais** dite interrogeable sur la foi de son nom : le statut `INDEXED`
est posé à l'indexation, d'après les fragments **réellement retenus**.

**Courriels imbriqués** : un message transféré (`message/rfc822` ou
fichier `.eml` joint) est ouvert tant que sa profondeur ≤
`DOCUMENT_EMAIL_MAX_NESTING_DEPTH` ; ses pièces sont traitées sous le **même** budget. Les
cycles (empreinte d'un ancêtre) sont refusés (`email_cycle_detected`). Le corps imbriqué
est indexé avec la provenance de sa pièce.

**Erreurs partielles** : aucune pièce ne fait échouer le courriel. `Document.status` reste
dans ses valeurs (`READY`/`FAILED`) ; `meta.extraction.email_status` vaut `complete` ou
`partial` — partiel seulement si une pièce est `REJECTED`, `FAILED`, `SKIPPED_LIMIT` ou
`PARTIALLY_INDEXED`. Un format volontairement non pris en charge (`UNSUPPORTED`, ex. GIF) et une
pièce sans texte (`NO_EXTRACTABLE_TEXT`) sont signalés à part et **ne rendent pas** un courriel
partiel. Les courriels ingérés avant cette évolution sont **normalisés à la lecture** par
l'API (`FAILED` + `ocr_no_text` sur une image, ou `empty_document` → `NO_EXTRACTABLE_TEXT`,
`email_status` recalculé) : aucune donnée modifiée, aucune réindexation. Échec du parseur : `email_parse_failed`.

## Provenance, indexation et citations

Fragments du corps : `email_part = "body"`. Fragments d'une pièce : rails de **leur**
format (`_rail_specs`, comme le même fichier déposé seul), plus :

| Clé `Chunk.meta` | Valeur |
|---|---|
| `email_part` | `attachment` |
| `email_attachment_index` / `email_attachment_path` | rang dans la liste plate / position dans l'arbre (`2`, `2.1`) |
| `email_attachment_filename` / `email_attachment_format` / `email_attachment_sha256` | nom sûr, format détecté, empreinte |
| `email_subject` / `email_nested_subject` / `email_nested_depth` | objet du parent, objet du message imbriqué, profondeur |
| `page_number`, `slide_number`, `sheet_name`, `cell_range`… | repère interne de la pièce, inchangé |

Chaque fragment de pièce est préfixé de « Pièce jointe : *nom* / Email : *objet* » ; une
section « Pièces jointes du message » nomme les pièces et leur statut (relation
interrogeable). Identité stable d'une pièce : courriel + chemin + empreinte — jamais le nom
seul (deux `rapport.pdf` différents restent distincts).

Citations (`project_source_meta`) : objet du courriel, nom et chemin de la pièce, puis le
repère interne. **Jamais** d'adresse ni de destinataire. Frontend : « Email : … »,
« Pièce jointe : … », puis la pastille de page/diapositive/feuille ; le lien ouvre le
courriel **sans** ancrage de page (le courriel n'a pas de pages).

**Preuve numérique** : la provenance de l'extracteur est conservée telle quelle — un texte
OCR reste OCR, une description reste non probante, une cellule Excel suit la politique
Excel. Le conteneur courriel ne rehausse jamais un statut de preuve.

## Droits, suppression, réindexation, doublons

- **Droits** : les fragments d'une pièce appartiennent au courriel ; toute vérification
  (retrieval, citation, aperçu, téléchargement, suppression) est celle du courriel.
  Aucune métadonnée de l'expéditeur n'attribue de droit RAGatm.
- **Suppression** du courriel ou de l'espace : cascade des fragments ; aucune pièce n'est
  stockée à part, donc rien d'orphelin.
- **Réindexation** : ré-extraction complète puis publication **atomique** existante ; un
  échec laisse l'index précédent publié. Idempotente (mêmes fragments, mêmes statuts).
- **Doublons** : même courriel déposé deux fois → dédoublonnage documentaire habituel (pas
  de second arbre). Même pièce dans deux courriels → deux provenances. Mêmes octets deux
  fois dans un courriel → `DUPLICATE_IN_EMAIL`.

## Activation

| Réglage | Rôle |
|---|---|
| `DOCUMENT_EML_ENABLED` | porte de **format** (environnement) |
| `DOCUMENT_EMAIL_GLOBAL_ENABLED` | **disponibilité générale** : tout RagSpace admet les courriels. Aucun droit élargi : droit d'écriture sur l'espace, quotas et validation toujours exigés ; identité d'espace obligatoire (le corpus métier n'est jamais concerné) |
| `DOCUMENT_EMAIL_RAGSPACE_IDS` | porte d'**espace** quand le mode général est fermé : UUID complets ; vide = aucun ; une entrée invalide = aucun |
| `DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED` | contenu des pièces (dans les espaces admis) |
| `DOCUMENT_EMAIL_MAX_*` | bornes (nombre, taille, volume, profondeur) |

Le dépôt d'un courriel hors espace admis est refusé (`email_ingestion_not_enabled_for_space`,
400). La réindexation d'un courriel **déjà** ingéré n'est pas refusée ; ses pièces ne sont
ingérées que si l'espace est admis. Un courriel est **toujours asynchrone**. Le corpus métier
(sans espace) n'ingère jamais de pièce jointe.

**Rollback** : fermer `DOCUMENT_EMAIL_GLOBAL_ENABLED` (retour au périmètre de la liste), retirer
l'espace de `DOCUMENT_EMAIL_RAGSPACE_IDS` (nouveaux dépôts refusés)
ou fermer `DOCUMENT_EMAIL_ATTACHMENT_INGEST_ENABLED` (inventaire seul), puis recréer
`backend`, `worker`, `worker_reindex`, `worker_visual`. Les fragments déjà publiés restent
jusqu'à réindexation ou suppression du courriel.

## Limites

Pas de description Vision ni de StructX pour les pièces jointes (OCR seulement pour les
images) ; pas d'antivirus (aucune chaîne n'existe : protections passives et refus de formats
seulement) ; pas de calcul tabulaire sur un classeur joint ; pas d'aperçu propre à une pièce ;
signatures S/MIME et pièces par référence web non lues (`UNSUPPORTED`). Voir
[limites connues](../10-reference/limites-connues.md).

## Corps courts (corps courts)

Le seuil de 40 caractères visibles départage les représentations d'un même message (HTML,
texte, RTF). Il ne fait plus disparaître un message laconique entier : un corps court mais
lisible est retenu en dernier recours, avec l'avertissement `msg_body_short`. Un corps vide
reste `msg_body_missing`.

## Références d'implémentation

- `backend/apps/rag_core/services/ingest/extraction/email_extractor.py` — rail commun, pièces, statuts.
- `backend/apps/rag_core/services/ingest/extraction/eml_email.py`, `eml_signature.py` — MIME.
- `backend/apps/rag_core/services/ingest/extraction/validation.py` — `check_email_admission`.
- `backend/apps/rag_core/services/ingest/indexing.py` — `_rail_specs`, `_email_attachment_specs`, `_finalize_email_attachments`.
- `backend/apps/rag_core/services/query/source_meta.py` — projection des citations.
- `frontend/src/app/chat/_lib/sources.ts`, `frontend/src/app/documents/_lib/types.ts`.
- Tests : `tests/tests_root/unit/extraction/test_email_27a.py`, `tests/tests_root/http_api/test_email_ingestion_27a.py`, `tests/frontend/lib/sourcesEmail.test.ts`.
