# Capacités documentaires effectives

*Statut : **implémenté**, lecture seule, réservé au superuser actif.*

L'écran d'administration **« Capacités documentaires »** (`/admin-core/document-capabilities`)
répond à la question : *si un utilisateur importe maintenant un fichier de tel type, quels
traitements RagATM appliquera-t-il réellement, y compris aux images, tableaux, graphiques et
schémas intégrés, et quels services AI Gateway interviendront ?*

Il **observe** ; il ne pilote rien. Aucun flag, aucune liste d'espaces, aucun modèle, aucun
document n'est modifié ; la consultation ne déclenche ni ingestion, ni OCR, ni inférence. Les
seules interactions externes sont les sondes `GET /health/*` d'AI Gateway.

## Sources de vérité

| Information | Source | Remarque |
|---|---|---|
| Ce que RagATM sait faire | registre déclaratif `services/capabilities/registry.py` | décrit, ne dit jamais « actif » |
| Ce qui est actif ici | accesseurs `ingest/extraction/config.py` et résolveurs de portée existants (`audio_admitted_for`, `email_admitted_for`, `pdf_vector_regions_enabled_for`, `pdf_generic_tables_decision`, `structured.check_scope`, `curve_shadow.shadow_refusal`, `orchestration.scope_gate`) | aucune règle d'autorisation réécrite |
| Portée par RagSpace | listes résolues en base (compteurs : listés, existants, admis) | aucun UUID exposé en vue globale |
| Configuration des workers | empreintes publiées par chaque worker (`runtime.py`) | comparées **par consommateur** |
| Santé des services | lectures `GET /health/runtime`, `/v1/models`, `/health/vision`, `/health/asr` (`gateway_health.py`), **seulement si `CAPABILITIES_GATEWAY_PROBES_ENABLED=1`** | délai 3 s, cache 30 s ; `/health/ready|embeddings|rerank|structured-extract` interdites (elles calculent) |
| Fournisseur d'embeddings | champs `embedding_provider`/`embedding_model` de chaque RagSpace | jamais le défaut global de `/api/v1/capabilities/` |

## Modèle d'état

Chaque capacité porte des dimensions **séparées** :

- `state` : `ACTIVE_GLOBAL` · `ACTIVE_SCOPED` (n espaces) · `ACTIVE_CONDITIONAL` (actif partout, exécuté
  si le contenu le justifie : image présente, page scannée…) · `DISABLED` · `UNSUPPORTED` · `UNAVAILABLE`
  (autorisé mais service ou dépendance indisponible) · `INCONSISTENT` (liste vide alors que le flag est
  allumé, ou divergence entre consommateurs) · `UNKNOWN` (porte illisible — jamais traduit en actif ni
  en désactivé) ;
- `runtime_available` : AVAILABLE / DEGRADED / UNAVAILABLE / UNKNOWN (service Gateway, rôle exécutant
  observé, dépendance locale) ;
- `output_indexed` (`INDEXED` / `INTERNAL_ONLY` / `NONE`) et `output_searchable` (indexé **et** actif) ;
- `numeric_evidence_class` (classes réelles de `numeric_evidence.py`) et `numeric_evidence_reliable` :
  une sortie peut être recherchable **sans** être une preuve chiffrée (Vision, OCR, ASR, StructX non qualifié) ;
- `qualification_status` + `qualification_evidence` : donnée datée (audit de référence), distincte de l'activation.

Une capacité active n'est pas déclarée en panne parce qu'un document courant ne contient pas
d'image : le déclencheur de contenu est une dimension à part.

## Parité de configuration des workers

Au démarrage (`worker_ready`, branché dans `config/celery.py`) puis toutes les 60 s, chaque worker
publie dans Redis (`ragatm:capfp:<env>:<rôle>:<instance>`, TTL 1 h) : environnement, rôle (dérivé du
nom de nœud `--hostname=<rôle>@%h`, suffixe d'environnement `-staging`/`-prod` ignoré), identité de processus, SHA déployé, version de schéma, et un
**condensé** de chaque réglage de capacité chargé (jamais la valeur, jamais un secret).

L'API compare, pour chaque flag, les valeurs des seuls **rôles consommateurs** (déclarés dans le
registre) :

- `CONFIGURATION_MISMATCH` seulement si deux consommateurs frais divergent ;
- une différence chez un non-consommateur est comptée (`ignored_non_consumer_differences`) mais n'alerte
  pas — exemples établis par l'audit 30A : `TABULAR_QUERY_ENABLED` (consommé par le seul backend),
  `DOCUMENT_AUDIO_GLOBAL_ENABLED` dans des conteneurs non recréés qui ne le lisent pas ;
- une empreinte de plus de 180 s est **périmée** (`WORKER_STATE = STALE`) et n'est plus l'état actuel ;
  une instance périmée dont un pair du même rôle est frais (conteneur recréé) est ignorée ;
- sans Redis : `UNKNOWN`, jamais « sain ».

## Services AI Gateway

Statut par service : AVAILABLE / DEGRADED / UNAVAILABLE / UNKNOWN. Une santé 200 n'est **pas** une
qualification : pour StructX, l'écran affiche `experimental`, `qualified_for_production` et la
qualification par tâche lus dans la sonde ; pour Vision, la disponibilité de la tâche `describe` est
distincte de `semantic_enabled` (information, pas une panne). Chaque capacité indique si elle appelle
la Gateway (`DIRECT_GATEWAY`), si cela dépend de l'espace (`DEPENDS_ON_RAGSPACE` : embeddings), ou si
elle est locale (`LOCAL_ONLY` : OCR Tesseract, graphiques Office, tableaux natifs, Excel), et **à quel
moment** (ingestion, enrichissement différé, question).

## Porte des sondes

*Statut : **activable par flag**, fermé par défaut.* Les quatre lectures ci-dessus sont décrites
non inférentielles par le contrat du pool, mais **aucune attestation sur le code Gateway
réellement déployé** n'a pu être produite. Or elles ne sont pas réservées à cet écran :
l'accueil de la console (`/admin-core`, `GET /api/v1/admin/core-config/health/`) les déclenche
aussi, dès son ouverture. `CAPABILITIES_GATEWAY_PROBES_ENABLED` (défaut `0`) ferme donc le seul
point d'entrée, `gateway_health.snapshot()` : aucun appel réseau, aucun cache lu ni écrit, tous les
services `UNKNOWN` avec le motif `probes_disabled`, `probes_enabled: false` dans la réponse. Les
capacités restent évaluées (portes, portées, parité des workers) ; seule la santé Gateway est
inconnue. À rouvrir (`1`) environnement par environnement, après attestation des handlers.

**Preuve.** Porte fermée, les quatre appels de la page
`/admin-core` (`health/`, `effective/`, `audit/`, `platform/workspace-quota-defaults/`) et l'écran
« Capacités documentaires » (`refresh=0|1`, dix cycles simulant un onglet ouvert) n'émettent **aucun**
appel réseau, mesuré aux trois frontières (`requests`, `urllib`, socket TCP). Les services Gateway y
sont `UNKNOWN`/`probes_disabled` et aucune capacité adossée à la Gateway n'est affichée disponible
(`runtime_available ≠ AVAILABLE`). Porte ouverte et Gateway en panne : une lecture par route au
plus (aucun retry), jamais une route inférentielle, aucun statut `AVAILABLE`.

**`/health/embeddings` n'est pas une sonde.** Seul `embedding_reference` la lit : c'est la
référence d'identité qui valide le `pipeline_id` de chaque réponse `POST /v1/embeddings`
(fail-closed, cache 300 s **par processus**, relecture forcée sur désaccord). Elle n'est déclenchée
que par une vraie demande d'embedding — jamais par la console, la liveness Docker (`/api/health/`)
ni une tâche planifiée. Elle calcule bien un embedding côté Gateway (≈ 0,2–2 s observés en
production le 08/11/2026) : la retirer affaiblirait le contrôle d'intégrité des embeddings
gouvernés ; elle est donc **conservée**. Le préflight `gateway_endpoints_preflight` reste sans
appel réseau sauf `--probe` explicite (geste opérateur, lit `/health/runtime`).

## Disponibilité déclarée et disponibilité vérifiée

Désormais, les embeddings et le reclassement sont en **DECLARED** :
publiés par un inventaire (`/v1/models`, section `rerank` de `/health/runtime`), **inférence non
vérifiée**. StructX est **UNKNOWN** (aucune route non inférentielle ne publie son état ; sa
qualification reste celle du registre). Vision et ASR restent sondés par leurs routes de santé,
non inférentielles. Un statut DECLARED n'est jamais affiché comme « testé ».

## API

`GET /api/v1/admin/core-config/document-capabilities/` — voir
[référence des endpoints](../05-api/reference-endpoints.md). Réponse partielle tolérée : une sonde en
échec marque seulement les capacités qui en dépendent.

## Enregistrer une nouvelle capacité

1. Ajouter un `CapabilitySpec` (ou un `FormatSpec`) dans `registry.py` : identifiant, format, catégorie
   (colonne de la matrice), libellé et description en français, porte (`gate`) et portée (`scope`)
   éventuelle, flags lus, rôle exécutant et rôles consommateurs, dépendance Gateway, sortie, citation,
   classe de preuve, qualification et limites.
2. Si la porte ou la portée est nouvelle, l'ajouter dans `resolver.GATES` / `resolver.SCOPES` en
   **appelant l'accesseur ou le résolveur existant** — jamais en recopiant sa règle.
3. `tests/tests_root/unit/capabilities/test_capability_registry.py` échoue tant qu'un extracteur, un
   détecteur structuré, un flag `*_ENABLED` de capacité, une porte ou un service Gateway n'est pas classé
   (ou explicitement exclu dans `EXCLUDED_FLAGS`).

## Limites

- Les empreintes reflètent la configuration au chargement du processus ; un worker arrêté apparaît
  périmé puis disparaît (TTL).
- La qualification est déclarative (référence d'audit) ; elle ne mesure pas la qualité courante.
- La vue par RagSpace est consultative : elle évalue les portes pour cet espace sans lire ses documents.
- Rollback : retirer le lien de navigation ne protège pas l'endpoint ; restaurer l'artefact précédent.
  Les clés Redis expirent seules ; aucune migration.

## Références d'implémentation

- `backend/apps/rag_core/services/capabilities/{registry,resolver,runtime,gateway_health}.py`
- `backend/api/v1/admin_core_config/views.py` (`DocumentCapabilitiesView`, `_gateway_health_summary`)
- `backend/config/celery.py` (branchement `worker_ready`)
- `frontend/src/app/admin-core/document-capabilities/`
- Porte des sondes : `gateway_health.probes_enabled()` / `disabled_snapshot()`, réglage `CAPABILITIES_GATEWAY_PROBES_ENABLED`
- Tests : `tests/tests_root/unit/capabilities/` (dont `test_gateway_probes_gate.py`), `tests/tests_root/http_api/test_admin_document_capabilities.py`, `tests/tests_root/http_api/test_admin_gateway_probes_gate_http.py` (espions réseau sur les vraies vues), `tests/tests_root/llm_router/test_probes_gate_business_paths.py` (embeddings métier, préflight Pool, liveness)
- Audit de conception :
