# Cadence d'import et reprise sur 429

*Statut : **implémenté le 23/08/2026**, frontend uniquement.*
Public : développeurs frontend, exploitants.

Fait suite à l'incident d'import massif du 23/08/2026. Contexte serveur :
[../10-reference/configuration.md](../10-reference/configuration.md) (`DRF_THROTTLE_USER`).

---

## 1. Le défaut

Le plafond de la production est de **120 requêtes/minute et par utilisateur**
(`UserRateThrottle` de Django REST Framework, appliqué à toutes les routes). L'import
de dossier envoyait à concurrence 4 **sans aucune notion de temps**. Mesuré deux fois
dans la même heure, sur le même compte :

```
17:17   110 acceptés en 11 s   puis  135 × HTTP 429
18:06    86 acceptés en  8 s   puis  158 × HTTP 429
```

Trois défauts se cumulaient :

1. **Aucune régulation de débit.** Quatre envois de 3 Ko partent à dix par seconde,
   soit 600/min contre un plafond de 120.
2. **`Retry-After` ignoré.** Le serveur annonçait « revenez dans 21 s » ; le client
   ne lisait pas l'en-tête.
3. **Le 429 traité comme définitif.** Chaque fichier passait en `error`, et le pool
   continuait d'émettre pendant ce temps.

> Réduire la concurrence n'aurait rien corrigé. Le chemin glisser-déposer était déjà
> à concurrence **1** et tout aussi exposé : **la concurrence borne le parallélisme,
> jamais le débit.**

## 2. La correction

Un ordonnanceur unique,
[`_lib/uploadScheduler.ts`](../../frontend/src/app/documents/_lib/uploadScheduler.ts),
qui remplace l'ancien `uploadPool`. Les **quatre** voies d'import y convergent déjà —
dossier, sélection multiple, glisser-déposer, ré-essai manuel — de sorte qu'aucun
chemin ne peut le contourner.

| Mécanisme | Effet |
|---|---|
| **cadence** | un intervalle minimal entre deux **démarrages** |
| **pause globale** | un 429 suspend *tout le lot*, pas seulement son fichier |
| **`Retry-After`** | le délai vient du serveur ; le repli n'est qu'un secours |
| **ré-essai borné** | 4 tentatives par fichier, puis échec explicite |
| **annulation** | requêtes, attentes et timers, tous interruptibles |

### La cadence

```
UPLOAD_MIN_INTERVAL_MS = 750     ≈ 80 démarrages/min = 66 % du plafond serveur
```

C'est une **marge client conservatrice, jamais la source de vérité du quota** : le
serveur seul connaît l'état de son seau glissant. La marge existe parce que l'import
n'est pas seul à consommer le quota — la page émet aussi ses propres requêtes (liste,
statistiques, quota : trois ont été throttlées à côté des uploads pendant l'incident),
et un second onglet doit rester possible.

C'est un intervalle entre **démarrages**, jamais un délai ajouté *après* un envoi.
Cette distinction règle trois cas d'un coup :

| cas | effet |
|---|---|
| 200 fichiers de 3 Ko | la rafale est écrasée — c'est le cas dangereux |
| 20 fichiers de 20 Mo | **aucune pénalité** : l'intervalle est déjà écoulé |
| réseau lent, 20 s/fichier | **aucune pénalité**, même raison |

### La pause globale

```
pauseUntil = max(pauseUntil, now + delai)
```

**Monotone** : une réponse arrivée plus tard avec un délai plus court ne raccourcit
jamais une pause décidée. Les requêtes **déjà en vol ne sont pas annulées** — leurs
octets sont partis, une réponse 2xx peut être en transit, et les interrompre créerait
une ambiguïté (§5). Seuls les *nouveaux* départs sont bloqués : le nombre de 429
subis est donc borné par la concurrence, soit **au plus 3**, contre 135 et 158.

Après le premier 429, l'intervalle est **doublé une fois** pour le reste du lot, et
jamais réduit — l'oscillation coûterait plus qu'elle ne rapporte. L'onglet retrouve sa
cadence nominale quand plus aucun lot ne tourne.

### La course, et sa parade

Un worker pourrait contrôler la pause puis démarrer juste après qu'un autre l'a
ouverte. La parade tient au modèle d'exécution : JavaScript est mono-thread, donc
**toute séquence sans `await` est atomique**. Le dernier contrôle de `pauseUntil` est
collé à l'émission, sans suspension entre les deux. La réservation du créneau de
cadence est synchrone pour la même raison.

## 3. Ce que voit l'utilisateur

Sept états, dont deux nouveaux :

```
queued ──► uploading ──► done
              │
              ├──► waiting_retry ──► uploading      « En attente — réessai dans 14 s »
              │           └────────► error          quota épuisé, message dédié
              ├──► error                            400/401/403/404/409/413/422
              └──► cancelled
```

Un élément en `waiting_retry` **n'est ni un succès ni un échec**. Il n'apparaît pas
dans « Réessayer les erreurs » : le ré-essai manuel doublerait l'automatique.

Le message dit une attente, jamais `API 429` — le code HTTP appartient au journal, pas
à l'utilisateur, qui n'a rien à corriger. Et quand le quota reste saturé après quatre
tentatives, le message le dit explicitement plutôt que d'accuser le fichier.

Le compteur est une **partition** : chaque fichier est dans exactement un seau
(envoyés / en cours / en attente / en échec / annulés). La barre suit `envoyés/total`
et ne recule jamais.

> **Un import de 500 fichiers prend désormais un peu plus de six minutes ; un import
> de 1 000 fichiers (borne actuelle, cf. [quotas-workspace.md](../10-reference/quotas-workspace.md#bornes-dun-import-de-dossier-frontend))
> un peu plus de douze.** C'est assumé : mieux vaut douze minutes avec une progression
> honnête que vingt secondes suivies de centaines de lignes rouges. Relever la borne de
> nombre n'a changé ni la cadence ni la concurrence.

## 4. Portée des ré-essais

**Seul le 429 est rejoué.** C'est le seul statut où le serveur dit que la requête
n'avait rien de fautif *et* fournit lui-même l'échéance.

`502`, `503` et `504` restent terminaux dans cette version : sur un POST ils sont
ambigus — le serveur a pu accepter. `503` porte parfois un `Retry-After` de même
sémantique et serait l'ajout le plus naturel plus tard, mais c'est une panne, pas une
régulation, et l'UX doit dire autre chose.

## 5. Doublons : déjà couverts

Le cas « requête émise, connexion coupée, réponse inconnue » ne peut pas créer de
doublon : le modèle `Document` porte une contrainte d'unicité en base,
`uniq_rag_document_space_sha256_not_failed`, doublée d'une déduplication applicative
par sha256 dans le service d'ingestion. Une clé d'idempotence serait donc redondante
aujourd'hui.

Ce qui n'était **pas** couvert : l'**enfilement**. Un ré-upload du même contenu, alors que
le document existant attendait encore son traitement (0 chunk), était ré-enfilé ; en
staging le 21/09/2026, 515 uploads acceptés pour 502 documents ont laissé 8 documents en
file 2 ou 3 fois, traités autant de fois. Depuis, l'upload dédupliqué d'un document
**PENDING ou INGESTING** n'est plus enfilé : réponse `200` avec
`detail: "ingestion_already_in_progress"` (voir
[ingestion-extraction §8](../03-backend/ingestion-extraction.md)).

Ce qui n'était pas couvert non plus : un contenu dont l'occurrence existante est **`FAILED`**.
La dédup l'ignorait et créait une nouvelle ligne à chaque ré-import ; depuis
`fix(ingest): reuse failed documents for duplicate content`, cette ligne est **réutilisée**
(même `document_id`, `detail: "failed_document_retried"`) et un doublon qui ne peut plus
redevenir actif est affiché comme **contenu identique déjà présent** (`duplicate_content`),
distinct d'un échec d'analyse.

## 5 bis. Suivi de l'ingestion après l'envoi

Une fois les fichiers envoyés, la page suit l'ingestion par relecture de la liste. La
cadence est **adaptative** (`_lib/documentsPolling.ts`, fonction pure `nextPollDelay`) :

| Situation | Comportement |
|---|---|
| aucun document en file ni en analyse | aucun suivi |
| onglet caché (`visibilityState !== "visible"`) | aucun suivi ; **un** rafraîchissement au retour |
| les compteurs viennent de changer | tick suivant dans 5 s |
| rien ne bouge | l'intervalle double jusqu'à 30 s |
| HTTP 429 | prochain tick à l'échéance `Retry-After`, jamais avant |

Les stats de l'espace ne sont relues que si les compteurs changent. Budget au pire :
24 req/min par onglet **visible** (0 pour un onglet caché), contre 48 req/min par onglet,
caché compris, avec l'ancien suivi fixe à 2,5 s — trois onglets suffisaient alors à
dépasser le `UserRateThrottle` de 120 req/min par utilisateur et à afficher « API 429 ».
Le suivi d'un job de réindexation est lui aussi suspendu quand l'onglet est caché.

## 6. Limites connues

- **Plusieurs onglets** ne se coordonnent pas. Le suivi d'ingestion d'un onglet caché
  est suspendu, ce qui borne le coût ; un import simultané dans plusieurs onglets visibles
  peut encore provoquer des 429 — traités correctement. Le défaut devient une lenteur, plus une avalanche. Une
  coordination (`BroadcastChannel`, élection de leader) coûterait plus qu'elle ne
  rapporte.
- **Un rafraîchissement du navigateur** perd la file. Aucune reprise durable.
- **Le serveur reste l'arbitre** : la cadence réduit la probabilité d'un 429, elle ne
  la supprime pas. C'est pourquoi la gestion du 429 reste obligatoire, et non un
  filet de secours.
- Les en-têtes `RateLimit-Limit / Remaining / Reset` permettraient de *dériver* la
  cadence au lieu de la choisir. DRF n'émet aujourd'hui que `Retry-After`.

---

## Références d'implémentation

- `frontend/src/app/documents/_lib/uploadScheduler.ts` (ordonnanceur)
- `frontend/src/lib/apiError.ts` (`parseRetryAfterMs`, `ApiError.retryAfterMs`)
- `frontend/src/lib/api.ts` (lecture de l'en-tête sur les deux transports)
- `frontend/src/app/documents/page.tsx` (verdict typé, états, compteurs, annulation)
- `tests/frontend/lib/uploadScheduler.test.ts` (29 tests, dont un lot de 1 000)
- `tests/frontend/components/DocumentsUploadBackpressure.test.tsx` (5 tests)
- `frontend/src/app/documents/_lib/documentsPolling.ts` (cadence du suivi d'ingestion)
- `tests/frontend/components/DocumentsPollingBudget.test.tsx` (budget réseau sur horloge simulée)
- `backend/api/v1/documents/views_write.py` (`_duplicate_of_active_ingestion`)
- `tests/tests_root/http_api/test_upload_active_ingestion_guard.py`
