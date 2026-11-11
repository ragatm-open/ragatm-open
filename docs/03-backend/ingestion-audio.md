# Ingestion audio — transcription ASR AI Gateway

*Activable par flag* (`DOCUMENT_AUDIO_INGEST_ENABLED`, fermé par défaut), puis **soit** borné par
espace (`DOCUMENT_AUDIO_RAGSPACE_IDS`, vide = aucun), **soit** en disponibilité générale
(`DOCUMENT_AUDIO_GLOBAL_ENABLED=1` : tout RagSpace, y compris créé ensuite ;
droits, quotas et plafond inchangés). Décision d'architecture :
[qualification interne, option A] — seule la **transcription intégrale horodatée** est
indexée ; aucune synthèse n'est générée ni indexée en V1.

Valeurs et réglages : [10-reference/feature-flags.md](../10-reference/feature-flags.md),
[10-reference/configuration.md](../10-reference/configuration.md),
[10-reference/limites-connues.md](../10-reference/limites-connues.md).

## Architecture

```text
dépôt  POST /api/v1/documents/upload-audio/   (≤ 600 000 000 octets, audio SEUL)
  └─ signature du conteneur (extension ET premiers octets) · admission espace · quota
  └─ Document PENDING, meta.audio.stage = queued, fichier enregistré PAR BLOCS
ingest_document_task (file ingest_audio)
  └─ reindex_existing_document : aucune transcription prête
       → AudioTranscript (preparing) + 1re étape ; document RENDU à PENDING
audio_transcription_step (file ingest_audio, concurrence 1, étapes courtes re-planifiées)
  ├─ préparation : ffprobe (décodable, piste audio, pas de vidéo, durée ≤ 12 h)
  │                découpe en parties ≤ 90 min, coupe sur un silence si possible
  ├─ par partie, SÉQUENTIELLEMENT : FLAC 16 kHz mono (≤ 192 Mio) → POST Gateway →
  │   relevés espacés (wait_ms=0) → résultat PERSISTÉ → DELETE du job Gateway
  └─ assemblage : segments décalés de l'offset de leur partie, monotonie vérifiée
       → AudioTranscript ready → ingest_document_task (ingestion STANDARD)
ingest_document_task
  └─ reindex_existing_document : transcription prête → JSON canonique → extraction
       (AudioTranscriptExtractor) → rail `units` → chunks + embeddings de l'espace → READY
```

Contexte d'accès : l'identité de l'utilisateur à l'origine de la demande est mémorisée à la
planification (`meta.audio.requested_by_user_id`, jamais exposée par l'API) et transmise à
l'ingestion finale ré-enfilée par la chaîne ASR — sans elle, la garde de tâche (owner-scope en
enforcement) la refuserait (`owner_scope: context_missing`, constaté en qualification staging).

Pourquoi le document reste **PENDING** pendant l'ASR : le reaper tient pour morte toute
tentative **INGESTING** plus vieille que le bail (900 s) ; une transcription de plusieurs
heures serait tuée. Les étapes courtes évitent aussi la limite Celery (540 s) et la
visibilité du broker. Un chien de garde (`audio-transcription-watchdog`, 300 s) relance une
chaîne restée inactive (worker redémarré) : l'état vit en base, jamais dans un processus.

## Formats et limites

| Format | Signature exigée | Remarque |
|---|---|---|
| `.mp3` | ID3 ou synchro de trame MPEG | pochette (`attached_pic`) admise |
| `.m4a` | boîte `ftyp` ISO-BMFF : marque majeure M4A/M4B/mp42/isom… **ou** 3GPP/3GPP2 (`3gp4`…`3gp9`, `3g2a`…`3g2c`, enregistreurs Android), **ou** marque majeure non refusée déclarant une marque compatible audio | une piste vidéo est refusée (`audio_contains_video`) ; QuickTime (`qt  `) et flux AAC brut (ADTS) refusés |
| `.wav` | `RIFF…WAVE` | |
| `.flac` | `fLaC` | |
| `.ogg` | `OggS` | Vorbis ou Opus |
| `.webm` | EBML + DocType `webm` | **audio seul** ; Matroska et vidéo refusés |

Hors V1 : `.opus` nu, toute vidéo (`.mp4`, `.mov`, `.mkv`).

Le MIME annoncé par le navigateur (`audio/mp4`, `audio/x-m4a`, `audio/m4a`, `application/mp4`, vide…) n'est **jamais** consulté : le format est constaté sur l'en-tête, et le type enregistré est celui du format constaté. Un refus `unsupported_audio` (415) journalise une description non sensible de l'en-tête (`audio_upload_refused suffix=… signature=…`, logger `rag.audio.upload`: marques ISO-BMFF ou 8 premiers octets, jamais le nom ni le contenu), pour identifier un conteneur légitime encore inconnu (les m4a 3GPP d'Android étaient refusés).

| Limite | Valeur | Où |
|---|---|---|
| Taille au dépôt (audio seulement) | 600 000 000 octets (décimaux) | `DOCUMENT_AUDIO_MAX_UPLOAD_BYTES`, proxy |
| Taille des autres formats | inchangée | `UPLOAD_MAX_FILE_SIZE_MB`, proxy `/api/` |
| Durée d'un enregistrement | 12 h (43 200 s) | `DOCUMENT_AUDIO_MAX_DURATION_SECONDS` |
| Durée d'une partie | 90 min (Gateway : 105 min) | `AUDIO_ASR_PART_MAX_SECONDS`, borné en dur à 100 min |
| Taille d'une partie | 192 Mio (Gateway : 256 Mio) | `AUDIO_ASR_PART_MAX_BYTES`, borné en dur à 244 Mio |
| Jobs ASR en vol | 1 par environnement | admission dans l'orchestrateur |
| Délai d'un job | 4 800 s | `AUDIO_ASR_JOB_DEADLINE_SECONDS` |

Le plafond Gateway par **job** n'est pas un plafond de **document** : un enregistrement plus
long que 90 min est découpé en parties traitées l'une après l'autre. Le réencodage FLAC
16 kHz mono (sans perte pour Whisper, qui travaille à 16 kHz mono) ramène un WAV de 600 Mo
à quelques dizaines de Mo ; il est appliqué à toute partie (préparation déterministe
`audio-prep/2`, horodatage par mot ; la v1 était à la granularité segment et ses transcriptions
sont invalidées — donc retranscrites — à leur prochaine réindexation). La coupe d'une partie se
place dans le silence qui CHEVAUCHE la fenêtre des 10 dernières minutes. Une partie trop
volumineuse après réencodage est coupée en deux, et la taille est contrôlée INDÉPENDAMMENT de la
durée avant tout dépôt.

## Contrat Gateway

Conventions **exactes** du client un projet interne (corps POST non publié dans l'OpenAPI) :
multipart `request` (JSON) **puis** `file` (nom constant `audio`, jamais le nom utilisateur) ;
`{model: "asr-whisper-turbo", language: "auto", timestamps: "word", diarize: true,
mode: "async"}`. **Écart assumé à un projet interne** (`timestamps: "segment"`) : la VAD du Gateway rend
des segments couvrant parfois des dizaines de minutes de silence (constat staging : un segment de
0:59 à 1:28:35 portant trois phrases) ; la capacité `word_timestamps` déclarée par le Gateway
(`segments[].words[{word, start_ms, end_ms}]`, vérifiée le 07/11/2026) permet de redécouper chaque
segment sur ses MOTS — nouvelle pièce après une pause ≥ 1,5 s ou au-delà de 30 s, bornes exactes
des mots, texte = suite des mots. Mots absents ou hors contrat : segment conservé tel quel ; corps streamé avec `Content-Length` exact. Résultat validé *fail closed* :
identifiant du job, modèle logique, empreinte `media.sha256` (= celle de la partie envoyée),
segments stricts (`start < end`, ordonnés) ; libellés de locuteur gardés seulement si la
diarisation est valide (≤ 4). Seule l'erreur `resource_saturated` est rejouée au dépôt
(aucune clé d'idempotence côté Gateway), et jamais si le Gateway publie `retryable: false`. Le
Gateway la rend en `503` sous la forme FastAPI `{"detail": {"code": "resource_saturated",
"retryable": true, "message": "budget GPU insuffisant…"}}` avec `Retry-After` (constaté en
qualification staging) : le code est lu dans `error` OU `detail`. Le GPU étant partagé (chat,
vision, un projet interne, prod et staging), une saturation est une ATTENTE (`waiting_for_asr`, au rythme
du `Retry-After` borné à [5, 300] s), au plus 1 h par partie, puis `asr_saturated`. Un `503` muet
n'est jamais rejoué à l'aveugle. Garde d'environnement : `/health/asr` doit annoncer
l'environnement du déploiement (`APP_ENV`), `allowed_here` et le modèle demandé.

Aucune authentification ASR n'est ajoutée ; les appels partent **du backend** uniquement.

### À travers AI Gateway Pool — *activé en staging (07/11/2026), production en direct*

Le pool relaie la Gateway à l'identique, mais émet ses **propres** refus (`X-AIGP-Error`,
codes en majuscules), rapportés sous `pool_<code>`. Règle : **seule la Gateway peut déclarer
un job mort**.

- **Relecture** (`GET …/{id}`) : un refus du pool n'est **jamais terminal**.
  `404 JOB_AFFINITY_UNKNOWN` (job créé hors du pool, affinité expirée, acheminement modifié en
  cours de job) consomme le budget borné de relectures (`MAX_TRANSPORT_ERRORS`), puis la
  transcription échoue avec `asr_job_tracking_lost`. Le contrat de la Gateway (directe ou
  relayée) est inchangé : `404`/`410` ou code terminal publié = job mort.
- **Dépôt** (`POST`) : aucun refus du pool n'est rejoué — `AFFINITY_STORE_UNAVAILABLE` et
  `JOB_AFFINITY_CONFLICT` surviennent **après** l'acceptation par la Gateway (compensation par
  DELETE ciblé). `resource_saturated` relayé de la Gateway reste la seule attente rejouable.
- **Suppression** : un `DELETE` refusé par le pool n'est pas une suppression confirmée.
- **Réponse perdue au dépôt** (délai de lecture, coupure après envoi — avec ou sans pool) :
  `asr_submit_outcome_unknown`, **jamais redéposé** (présenté `asr_unavailable`). Seul un échec
  de transport prouvé antérieur à l'envoi (connexion refusée, DNS, délai de connexion) reste
  une attente rejouable. Auparavant, toute erreur de transport au dépôt était rejouée comme une
  saturation, au risque d'un second job pour la même partie.
- Changer l'acheminement exige la **vidange** préalable : `python manage.py asr_drain_preflight`
  (lecture seule, `ASR_DRAIN_STATUS=PASS|BLOCKED|UNKNOWN`).

## Transcription persistée

`AudioTranscript` (table `rag_audio_transcript`, CASCADE avec le document) : une ligne par
tentative, au plus une active par document. Elle porte l'état de l'orchestration (parties,
jobs) puis la transcription : `segments` (`segment_index`, `start_ms`, `end_ms`, `text`,
`speaker_id` facultatif, `precision="part"` si le moteur n'a rendu aucun segment valide),
`text`, `language`, `duration_ms`, `model_logical`, `model_revision` (lue sur `/health/asr`),
`prep_version`. Aucun texte n'est résumé, reformulé ni retiré.

## Indexation et citations

Les segments consécutifs sont regroupés en unités ≤ 1 000 caractères (sous le budget de chunk
gouverné) : **une unité = un chunk**, sans recouvrement ; une unité ne franchit jamais une pause
de plus de 5 s et ne couvre jamais plus de 2 min, pour que sa plage reste une citation exploitable. Méta de chaque chunk :
`source_type = AUDIO_ASR_TRANSCRIPT`, `source_is_asr`, `start_ms`, `end_ms` (bornes EXACTES de
ses segments), `transcript_segment_ids`, `asr_model`, `language`, `timestamp_precision`,
`speaker_ids`, `locator = {type: time_range, identifier: "00:12:35 → 00:13:48"}`. Même modèle
d'embedding que l'espace, même retrieval, même reranker. La citation affiche la plage ; il n'y
a pas de lecteur audio en V1 (aucun lien vers l'instant).

## Preuve numérique

Classe **`asr_transcript`**, dans l'ensemble NON VÉRIFIÉ de
[pipeline-retrieval.md](pipeline-retrieval.md) : recherchable et citable, jamais une preuve
chiffrée. Repli *fail-closed* : un chunk d'un document audio dont la méta de provenance est
perdue est classé `asr_transcript` (raison `audio_document_provenance_missing`), jamais
`unknown`. En-tête de source du prompt : « TRANSCRIPTION AUDIO <plage> (reconnaissance vocale
automatique, non vérifiée) ». Un horodatage `HH:MM:SS` dans une réponse est un repère de
lecture, pas une valeur (la forme à deux groupes `14:30` reste contrôlée).

## Droits, quotas, doublons, réindexation, suppression

- Admission (porte de dépôt, `check_audio_admission`) : `DOCUMENT_AUDIO_INGEST_ENABLED` **et**
  (`DOCUMENT_AUDIO_GLOBAL_ENABLED` **ou** espace listé dans `DOCUMENT_AUDIO_RAGSPACE_IDS`). Le mode
  général n'ouvre rien sans le drapeau audio ; seules les valeurs booléennes explicites l'activent
  (absent = fermé ; valeur invalide = démarrage refusé par le registre). Aucune liste d'UUID ne
  remplace le mode général. Capacité : `ingest.audio.space_scoped` = faux en mode général ; chaque
  RagSpace listé porte `audio_upload_enabled`.
- Droits : écriture sur l'espace (owner, admin, member ; viewer refusé `403`, non-membre refusé),
  vérifiés côté backend **avant** la porte audio, comme tout dépôt ; isolation par RagSpace.
- Quotas : l'audio consomme son nombre et ses octets ; parties et transcription ne sont ni
  des documents ni comptées (dérivés, comme les chunks).
- Doublon : dédoublonnage (espace, sha256) inchangé — aucune nouvelle transcription.
- Réindexation : **réutilise** la transcription `ready` de même (empreinte, préparation,
  modèle) ; Whisper n'est relancé que si l'un des trois change. Publication atomique et
  conservation de l'index antérieur en cas d'échec : mécanisme standard.
- Suppression : permise pendant la préparation/transcription (seule exception à la règle
  « pas de suppression d'un document en traitement ») ; les jobs Gateway de CE document sont
  supprimés, la transcription part en cascade, le spool est balayé par le worker. Une étape
  tardive ne recrée rien.

## Statuts et erreurs

`Document.status` reste PENDING → READY | FAILED ; la sous-étape est dans `meta.audio.stage`
(`queued`, `preparing_audio`, `waiting_for_asr`, `transcribing`, `assembling_transcript`,
`indexing`, `failed`), exposée par l'API (`audio`). Codes : `audio_file_too_large`,
`unsupported_audio`, `audio_ingestion_disabled`, `undecodable_audio`, `no_audio_track`,
`audio_contains_video`, `audio_too_long`, `asr_timeout`, `asr_unavailable`, `asr_saturated`,
`asr_failed`, `asr_job_tracking_lost` (suivi du job perdu après un changement d'acheminement),
`transcript_empty`, `workspace_quota_exceeded`. Les refus d'acheminement du pool
(`pool_node_unavailable`, `pool_upstream_*`, `pool_affinity_store_unavailable`…) sont présentés
comme `asr_unavailable` ; le code d'origine reste dans les journaux (`gateway_code=`).

## Exploitation et rollback

- Worker `worker_audio` (image `WITH_AUDIO=1` : ffmpeg/ffprobe), `--queues=ingest_audio`,
  concurrence 1 — voir la documentation d'exploitation.
- Proxy : la route `/api/v1/documents/upload-audio/` exige `client_max_body_size` ≥ 600 Mo ;
  le relais Next (`app/api/[...path]/route.ts`) streame le corps de CETTE route seulement.
- Rollback du mode général : `DOCUMENT_AUDIO_GLOBAL_ENABLED=0` — retour à la liste
  `DOCUMENT_AUDIO_RAGSPACE_IDS` pour les **nouveaux** dépôts ; les transcriptions déjà en cours vont
  à leur terme (préservées) ; rien n'est supprimé.
- Rollback fonctionnel : `DOCUMENT_AUDIO_INGEST_ENABLED=0` — dépôt refusé, chaînes en cours
  annulées EXPLICITEMENT (`audio_ingestion_disabled`, jobs Gateway libérés, rien de publié),
  audios déjà indexés conservés et réindexables.

## Références d'implémentation

- `backend/apps/rag_core/services/ingest/audio/` : `formats.py`, `media.py`, `gateway_asr.py`,
  `transcript.py`, `orchestrator.py`, `drain.py` (préflight de vidange, lecture seule).
- `backend/apps/rag_core/management/commands/asr_drain_preflight.py`.
- `backend/apps/rag_core/models/audio_transcript.py`, migration `0047_audio_transcript`.
- `backend/apps/rag_core/services/ingest/extraction/audio_transcript_{signature,extractor}.py`.
- `backend/apps/rag_core/services/ingest/reindex.py` (branche audio), `tasks.py`
  (`audio_transcription_step`, `audio_transcription_watchdog_task`), `queues.py`.
- `backend/api/v1/documents/views_write.py` (`DocumentAudioUploadView`, suppression),
  `views_common.py` (enregistrement par blocs, champ `audio`).
- `backend/apps/rag_core/services/query/numeric_evidence.py` (`ASR_TRANSCRIPT`, repli,
  `_TIMECODE_RE`), `source_meta.py`, `domain/retrieval.py` (en-tête).
- Tests : `tests/tests_root/unit/audio/test_audio_units.py`,
  `tests/tests_root/unit/audio/test_m4a_container_brands.py` (en-têtes ffmpeg réels),
  `tests/tests_root/http_api/test_audio_ingestion_29b.py`.
