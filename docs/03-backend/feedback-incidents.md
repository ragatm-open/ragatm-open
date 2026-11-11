# Feedback utilisateur et incidents diagnostiques

**Statut : implémenté** — lot 1 *activé par défaut*, lots 2 à 4 *activables par flag*
(tous à `0` à la livraison).

Ce document décrit comment un pouce vers le bas devient un incident **diagnosticable**
puis **rejouable** en développement, et surtout ce que le dispositif refuse de faire.

Valeurs de configuration : [../10-reference/configuration.md](../10-reference/configuration.md).
Rétention et ordonnancement : la documentation d'exploitation (hors périmètre de cette édition).
Gouvernance d'audit : la documentation d'exploitation (hors périmètre de cette édition).

---

## 1. Le problème résolu

Avant ce lot, un pouce vers le bas écrivait **trois colonnes** dans `RagQueryLog`
(`feedback_score`, `feedback_reason`, `feedback_comment`). Deux conséquences :

1. **Le signal ne survivait pas.** `RagQueryLog.rag_space` est en `CASCADE` :
   supprimer un espace détruisait *en même temps* la requête et le signalement
   disant qu'elle s'était mal passée. Or c'est souvent après la suppression de
   l'espace qu'on cherche à comprendre.
2. **Le signal n'expliquait rien.** Seules les 5 à 8 sources **retenues** sont
   journalisées, avec un extrait tronqué à 200 caractères. Impossible de répondre
   à la seule question qui compte : *le bon chunk était-il candidat, et pourquoi
   a-t-il été écarté ?*

Cas de référence : « what is Nordexia Conseil? » → « I don't know », alors que le
terme est indexé. Trois causes possibles, jusqu'ici indiscernables :

| Cause | Correction attendue |
|---|---|
| Le chunk n'était pas candidat | seuils, `RAG_VECTOR_TOP_K`, configuration FTS |
| Il était candidat mais écarté | diversification, pénalité structurelle, budget de prompt |
| Il était injecté, le modèle a refusé | prompt, politique d'abstention |

---

## 2. Architecture

```
réponse générée
   └─> capture MINIMALE des candidats dans un tampon Redis (TTL 48 h, aucun texte)

clic 👎
   └─> AnswerFeedback  (durable, survit à tout)          ← SYNCHRONE, < 50 ms
        └─> tâche Celery
             └─> FeedbackDiagnosticSnapshot (sensible, expire à 30 j)

qualification administrateur
   └─> autorisation d'export (niveau 1, 2 ou 3)          ← MANUELLE, auditée
        └─> paquet chiffré et signé
             └─> import en dev  ->  replay  ->  fixture de non-régression
```

### Le point dur, et pourquoi l'architecture est celle-là

Les candidats du retrieval **n'existent que pendant l'exécution de `query_rag`**. Ils
vivent dans une variable locale et disparaissent au `return`. Une tâche Celery
déclenchée au clic — trente secondes ou trois jours plus tard — ne peut donc rien
récupérer : *il n'y a plus rien à récupérer*.

Il n'existe que deux façons honnêtes d'obtenir la trace :

| Voie | Fidélité | Coût | Étiquette |
|---|---|---|---|
| **Tampon** — écrit pendant l'exécution | exacte | ~4,6 kB par réponse (mesuré, 20 candidats), expire seul | `capture_mode=buffer` |
| **Replay** — retrieval rejoué après coup | approximative | 1 retrieval, aucun LLM | `capture_mode=replay` |
| Ni l'un ni l'autre | ce que le journal contient encore | nul | `capture_mode=partial` |

> **Un replay n'est pas une capture.** L'index, la configuration et les modèles ont pu
> changer entre-temps. Le champ `capture_mode` existe précisément pour empêcher de
> conclure sur un état qui n'est pas celui de l'incident.

---

## 3. Modèle de données

### `AnswerFeedback` — le signal, durable

Aucune clé étrangère `CASCADE`. Les liens vivants (`query_log`, `created_by`) sont en
`SET_NULL` : ils *deviennent* nuls, ils n'emportent rien. Tout ce qui rend l'incident
interprétable est **dénormalisé à la création** dans des champs `*_hist` immuables.

| Groupe | Champs |
|---|---|
| Liens vivants (`SET_NULL`) | `query_log`, `created_by` |
| Liens logiques (UUID, sans FK) | `message_id`, `thread_id` |
| Identifiants historiques | `query_log_id_hist`, `rag_space_id_hist`, `workspace_id_hist`, `user_pseudonym`, `log_created_at` |
| Empreintes | `question_hash`, `answer_hash`, `prompt_hash`, `sources_fingerprint`, `dedup_fingerprint` |
| Contexte technique | `policy_hist`, `llm_provider_hist`, `llm_model_hist`, `app_git_sha` |
| Signal | `rating`, `kind`, `comment`, `expected_document`, `expected_term` |
| Gouvernance | `status`, `priority_score` |

**Contenu** : ni chunk, ni extrait, ni prompt. La question et la réponse n'y figurent
que sous forme de hash. Le seul texte libre est le commentaire — borné à 2 000
caractères, et **exclu des exports par défaut**.

**Immutabilité partielle** : les champs historiques sont figés après création
(`ValueError` à la réécriture). Le statut, la priorité et les champs corrigeables par
l'utilisateur restent modifiables — un feedback a un cycle de vie, contrairement à un
événement d'audit.

### `FeedbackDiagnosticSnapshot` — le matériel, expirable

Table **séparée**, pour quatre raisons dont aucune n'est esthétique :

1. **Rétention** — le signal se garde des années, ce contenu des semaines ;
2. **Public** — un admin de workspace voit qu'un incident existe, pas les extraits ;
3. **Cycle de vie** — purger le matériel ne doit pas effacer le signalement ;
4. **Schéma** — ce JSON suit le pipeline, le signal reste stable.

`CASCADE` depuis `AnswerFeedback` uniquement (un snapshot sans feedback n'a pas de
sens) ; append-only, même patron que `ActionAuditEvent`. `expires_at` est le seul champ
mutable, et sa prolongation est auditée.

---

## 4. Le tampon de capture

Clé logique : `ragatm:feedback-trace:<log_id>`. Le stockage passe par le cache Django
(`CACHES["default"]`, Redis partagé en staging/prod), donc la clé réelle porte en plus
le préfixe de version du cache Django (`:1:`).

**Ce qui y est écrit** — et rien d'autre :

```json
{
  "chunk_id": "…", "document_id": "…", "leg": "hybrid",
  "rank_fts": 3, "rank_vector": 7, "rank_fused": 4,
  "score": 0.71, "fusion_score": 0.032, "rerank_score": 0.82,
  "selected": false, "excluded_by": "diversity_max_per_group"
}
```

**Interdits, et vérifiés par un test dédié** : texte de chunk, nom de fichier, chemin,
question, réponse, prompt, commentaire, secret. C'est ce qui rend acceptable une
collecte systématique — le tampon est écrit pour 100 % des réponses.

**Fail-open absolu** : Redis indisponible, sérialisation impossible, quota dépassé → le
chat continue. Aucune exception ne remonte au pipeline.

### Qualifié contre un Redis réel

LocMemCache ne peut prouver ni la clé, ni le TTL, ni le partage entre processus. Une
commande dédiée les vérifie contre le Redis **réel** d'un environnement — à exécuter en
staging **avant** d'activer le tampon :

```bash
python manage.py qualify_feedback_buffer --ttl-check-seconds 3
```

Elle n'écrit qu'une clé de qualification, la supprime en sortie, et refuse de qualifier
si le cache n'est pas partagé (LocMem ⇒ le worker Celery ne verrait jamais la trace du
backend). Exécution de référence : **29/29 contrôles** sur Redis réel.

| Constat | Valeur mesurée |
|---|---|
| Clé réelle | `:1:ragatm:feedback-trace:<log_id>` (préfixe de version du cache Django) |
| TTL posé | 172 800 s, conforme au réglage |
| Taille | **4 645 octets** pour 20 candidats, soit **~232 o/candidat** |
| Lecture inter-processus | trace relue à l'identique par un processus distinct (backend ↔ worker) |
| Expiration | clé absente après échéance |
| Promotion | clé supprimée après passage en snapshot |
| Redis injoignable | écriture `False`, lecture `None`, aucune exception |

> **Sérialisation** : `django-redis` stocke la valeur **picklée**. Un `redis-cli GET`
> renvoie donc des octets binaires, pas du JSON lisible — le contenu JSON est à
> l'intérieur du pickle. À savoir avant d'inspecter le tampon en exploitation.

### Raisons d'exclusion

Vocabulaire **stable** (contrat de lecture des incidents déjà capturés) :

| Valeur | Signification | Émise aujourd'hui |
|---|---|---|
| `selected` | retenu dans le contexte final | ✅ |
| `prompt_budget` | retenu par le ranking, évincé par le budget de caractères | ✅ |
| `rerank_cut` | présent avant reranking, absent après | ✅ |
| `diversity_max_per_group` | écarté par le plafond de diversité | ✅ |
| `not_selected_top_k` | au-delà du `top_k` | ✅ |
| `unknown` | étape d'exclusion **non connue** | ✅ |
| `below_threshold` | sous le seuil de score | ❌ *(voir limites)* |
| `structural_penalty`, `duplicate`, `filtered_by_scope`, `unsupported_source` | réservés | ❌ |

> **Aucune raison n'est inventée.** Quand l'étape n'est pas connue avec certitude, la
> valeur est `unknown`. Une raison plausible mais fausse enverrait la correction dans la
> mauvaise direction — c'est pire que pas de raison du tout.

---

## 5. API

### `POST /api/v1/chat/feedback/`

```json
{
  "log_id": "uuid",
  "score": -1,
  "kind": "info_in_docs",
  "comment": "la réponse est dans le document résumé juste avant",
  "expected_document": "note-interne-2023.pdf",
  "expected_term": "Nordexia Conseil"
}
```

| Catégorie | Valeur diagnostique |
|---|---|
| `info_in_docs` | **maximale** — affirme une vérité vérifiable, ouvre les deux champs ci-dessus |
| `wrong_sources` | forte — pointe le retrieval |
| `wrong_answer` | moyenne — pointe la génération |
| `wrong_language` | faible en volume, mais **déterministe** donc auto-testable |
| `other` | nécessaire |

**Compatibilité ascendante** : l'ancien champ `reason` reste accepté et traduit
(`missing_sources`/`irrelevant` → `wrong_sources`, `incorrect`/`incomplete` →
`wrong_answer`). Les doublons historiques `feedback_*` sont ignorés silencieusement. Une
catégorie inconnue retombe sur `other` plutôt que de perdre le signalement.

**Doublons — choix retenu : `409` + `PATCH`.** Un second `POST` ne réécrit plus le
premier en silence ; il répond `409 feedback_exists` avec l'identifiant existant. La
correction passe par `PATCH /api/v1/chat/feedback/<id>/`, qui modifie catégorie,
commentaire et champs attendus, mais **jamais la note** : changer d'avis crée un cas, pas
une rature.

**Débit** : `DRF_THROTTLE_FEEDBACK_CREATE=10/min`, **par utilisateur**. Jamais de limite
globale — elle punirait tout le monde pour l'excès d'un seul.

**Latence** : le chemin synchrone ne fait que des lectures indexées et une écriture.
Aucun appel LLM, aucune recherche vectorielle, aucun balayage, aucun sous-processus. La
tâche Celery est enfilée par `transaction.on_commit`, jamais avant.

---

## 6. Priorisation et déduplication

| Signal | Points |
|---|---|
| `info_in_docs` | +3 |
| `wrong_sources` | +2 |
| ≥ 2 utilisateurs distincts sur la même empreinte | +2 |
| politique observée `no_answer` | +2 |
| terme déclaré **réellement trouvé** en base (asynchrone) | +1 |
| terme présent dans le contexte conversationnel capturé | +1 |
| aucun signal objectif | −1 |

Deux règles ont été **écartées** volontairement : « beaucoup de sources consultées »
(corrèle mal — une question large en consulte beaucoup sans être un incident) et
« commentaire long = signalement de qualité » (pénalise les utilisateurs peu bavards,
pas les mauvais signalements). Le malus porte sur l'absence de **tout signal objectif**,
qui est mesurable.

Le bonus « terme trouvé » est calculé **hors du chemin du clic** : c'est ce qui
transforme « l'information est dans mes documents » d'une déclaration en un fait.

**Empreinte de déduplication** :
`sha256(question normalisée | rag_space_id_hist | llm_model_hist | sources_fingerprint)`.
Elle regroupe « même question, même espace, même modèle, même retrieval ». La question
seule regrouperait à tort des cas devenus différents (index réindexé, modèle changé) :
la correction attendue ne serait alors pas la même. Chaque feedback reste visible
individuellement — le regroupement n'est jamais une fusion destructive.

---

## 7. Export d'incident

> **Il n'existe AUCUN chemin par lequel un clic utilisateur déclenche un export.**
> Aucune tâche périodique n'exporte. Le drapeau `FEEDBACK_INCIDENT_EXPORT_ENABLED` ouvre
> une action **manuelle**, il ne crée pas de déclenchement automatique. Un test
> structurel échoue si un module autre que le service d'export appelle la génération.

### Niveaux

| Niveau | Contenu | Motif | Rôle | Chiffrement |
|---|---|---|---|---|
| 1 — métadonnées | ids, rangs, scores, seuils, configuration. **Aucun texte** | non | admin plateforme | non requis |
| 2 — fragments | + extraits injectés, noms de fichiers **pseudonymisés** | **obligatoire** | admin plateforme | **obligatoire** |
| 3 — fichier source | + document d'origine | **obligatoire** | **superuser** | **obligatoire** |

Le niveau 1 suffit à répondre à la question du diagnostic. On ne monte d'un niveau que
si le niveau inférieur ne permet pas de reproduire — le niveau 3 vise les défauts
d'**extraction** ou de **chunking**, que par définition aucun extrait déjà découpé ne
reproduit.

### Ordre des opérations — non négociable

```
autorisation -> rédaction -> détection de secrets -> intégrité -> chiffrement
```

Chiffrer avant de rédiger produirait un paquet sûr en transit et sale à l'arrivée.
Signer avant de chiffrer garantit que la signature porte sur le contenu, pas sur son
enveloppe.

### Rédaction et secrets — deux mécanismes distincts

- **Rédaction** : retire e-mails, chemins, identifiants de compte, et le **commentaire
  libre** (exclu par défaut, quel que soit le niveau). Noms de documents pseudonymisés
  en alias stables `doc-<8 hex>.<ext>`.
- **Détection de secrets** : cherche ce qui ne devrait jamais s'y trouver (clé d'API,
  jeton, clé privée). Une détection **bloque** l'export ; elle ne le nettoie pas —
  nettoyer donnerait l'illusion qu'il n'y en avait pas.

`redaction_status` : `pending` | `passed` | `blocked` | `manually_approved`. La dernière
valeur n'est posée que par une décision humaine, tracée.

### Chiffrement

Fernet (AES-128-CBC + HMAC-SHA256, `cryptography`). **Aucune cryptographie maison.**
La clé vit dans la configuration, jamais dans le paquet, jamais dans les logs.
**Sans clé, un export contenant du contenu de production est REFUSÉ** — pas produit en
clair : le repli silencieux est exactement la façon dont un contenu sensible finit par
sortir.

### Format du paquet

```
incident/
├── manifest.json              provenance, intégrité, autorisation
├── request.json               question, langue, contexte conversationnel
├── effective_config.json      seuils, budgets, flags, modèles
├── retrieval_candidates.json  candidats, rangs, scores, raisons d'exclusion
├── selected_chunks.json       sources retenues (contrat v3)
├── generation.json            provider, modèle, politique, [prompt]
├── source_fragments/          niveau 3 uniquement
└── expected_behavior.md       ce qui aurait dû se produire
```

`content_sha256` suit une **procédure canonique** documentée et reproductible :
`manifest.json` écarté, chemins triés par octets, absorption de
`chemin + \x00 + sha256(contenu) + \n`. Le tri rend l'empreinte indépendante de l'ordre
d'écriture ; le sha256 par fichier la rend calculable en flux sur de gros fichiers.
La signature est un HMAC-SHA256 de ce condensat.

### Rôles

| Rôle | Voir le signal | Voir la trace | Voir les extraits | Autoriser | Télécharger |
|---|---|---|---|---|---|
| Utilisateur | son propre feedback | non | non | non | non |
| Admin workspace | son périmètre | non | **non** | non | non |
| Admin plateforme | oui | oui | oui | niveaux 1–2 | non |
| Superuser | oui | oui | oui | **niveau 3** | **oui** |
| Développeur | non | via paquet importé | via paquet | non | paquet approuvé |

> **Réserve** : dans l'instance actuelle, « admin plateforme » **est** le superuser
> (`is_ragatm_owner` ⇒ `is_superuser`). La séparation est implémentée et testée, mais
> elle ne devient effective qu'avec un rôle plateforme distinct.

---

## 8. Replay en développement

| Mode | Données requises | Reproduit | Ne reproduit pas | Déterministe |
|---|---|---|---|---|
| `prompt-only` | prompt complet — **non disponible aujourd'hui, voir ci-dessous** | hallucination, abstention, langue, format | tout ce qui vient du retrieval | non |
| `retrieval` | mini-RagSpace + chunks | **rang, score, sélection du bon chunk** | la formulation finale | **oui** |
| `full-pipeline` | ci-dessus + LLM | le défaut de bout en bout | la reproductibilité stricte | non |

`retrieval` et `full-pipeline` exigent un RagSpace : `search_chunks_hybrid` et
`search_chunks_vector` interrogent PostgreSQL (FTS + pgvector), et aucune abstraction ne
permet de les alimenter depuis un fichier. `prompt-only` fonctionnerait **hors base**.

> ### ⚠️ `prompt-only` n'est PAS disponible depuis une capture réelle
>
> Le prompt final n'est présent dans **aucun** snapshot issu du fonctionnement normal.
> Il est construit dans `query_rag`, renvoyé à l'appelant, et **personne ne le
> persiste** : le tampon exclut délibérément tout texte, et la tâche de promotion
> appelle `build_generation_metadata(log, prompt=None)`.
>
> Conséquences exactes :
>
> - `generation_metadata.prompt_capture` vaut **toujours `hash_only`** ;
> - `RAG_FEEDBACK_DIAGNOSTIC_CAPTURE_PROMPT=1` **ne capture pas le prompt** — il
>   n'ouvre en pratique que la capture du texte du contexte conversationnel ;
> - `replay_modes_supported` n'annonce donc **jamais** `prompt-only`, ce qui est le
>   comportement voulu : un mode annoncé mais inexécutable ferait perdre du temps ;
> - la commande `replay_rag_incident --mode prompt-only` **refuse** explicitement,
>   plutôt que de reconstruire un faux prompt.
>
> Le mode reste implémenté et testé sur un prompt fourni. Le rendre atteignable
> depuis une capture réelle suppose un **stockage sensible distinct**, décidé dans
> un lot de conception séparé. **Stocker le prompt dans Redis pour combler l'écart
> est exclu** : cela romprait l'invariant « aucun texte dans le tampon », qui est ce
> qui rend acceptable une collecte écrite pour 100 % des réponses.

### Contrôles à l'import

`import_rag_incident` vérifie, **avant toute écriture** : version de format, empreinte de
chaque fichier, empreinte globale, signature, expiration, environnement d'origine, modèle
d'embedding.

Le contrôle décisif n'est pas cryptographique — c'est le **modèle d'embedding**. Rejouer
un incident produit sur `text-embedding-3-large` avec des embeddings `ollama` donne des
scores sans rapport : le replay conclurait, et il conclurait faux. L'import **refuse**.

Le modèle local est celui du `--rag-space` de replay (c'est le RagSpace qui porte
l'embedding effectif, pas un réglage global). Sans espace désigné, la comparaison est
déclarée **impossible** et signalée : refuser sur une comparaison qu'on ne sait pas faire
produirait des faux positifs, et un contrôle qui se déclenche à tort finit désactivé.

`--delete-plaintext-after-import` supprime l'archive source : une copie de contenu de
production sur un poste de développement doit être éphémère.

### Promotion en non-régression

`promote_rag_incident_to_fixture` produit une ligne **JSONL du format déjà consommé par
`rag_benchmark`** (`expected_documents`, `expected_chunk_ids`, `should_answer`…).
Réutiliser le harnais existant plutôt qu'en créer un second signifie qu'un incident promu
entre immédiatement dans la mesure de qualité en place, avec ses seuils et son
`--fail-under`.

`expected_answer_contains` n'est **pas** généré par défaut : il proviendrait du
signalement, donc potentiellement d'un contenu de production, et une fixture vit
indéfiniment dans l'historique git. Il faut `--allow-content`, après vérification.

> **Un incident d'ABSTENTION n'est pas promouvable automatiquement.** Quand le journal
> ne porte aucune source retenue — la signature même d'un « I don't know » — il n'y a
> ni `expected_documents` ni `expected_chunk_ids`, donc rien de mesurable. La commande
> **refuse** avec la marche à suivre, plutôt que d'émettre une ligne JSONL que
> `rag_benchmark` rejetterait à la lecture. Le cas se complète à la main depuis
> `expected_behavior.md`, qui porte le document et le terme déclarés par l'utilisateur.

| Assertion | Déterministe | Où |
|---|---|---|
| le bon chunk est candidat | ✅ | `rag_benchmark` (recall@k) |
| le bon chunk est sélectionné | ✅ | `rag_benchmark` |
| pas d'abstention injustifiée | ✅ | `rag_benchmark` (no-answer accuracy) |
| langue résolue correcte | ✅ | test unitaire |
| formulation de la réponse | ❌ | `--with-answers`, hors CI bloquante |

---

### Désactiver le diagnostic le désactive VRAIMENT

Un snapshot n'existe que si **au moins une capacité de capture est explicitement
activée**. Sans cela, le clic produit un `AnswerFeedback` durable — et rien d'autre.

| `BUFFER` | `REPLAY` | Résultat |
|---|---|---|
| 0 | 0 | **feedback seul, aucun snapshot, aucune tâche** |
| 1 | 0 | snapshot `buffer` si la trace est là, sinon `partial` |
| 0 | 1 | snapshot `replay` ou `partial` |
| 1 | 1 | priorité au tampon, replay en repli |

`RAG_FEEDBACK_DIAGNOSTIC_CAPTURE_PROMPT` **n'entre pas** dans cette décision : il
gouverne le contenu sensible d'un snapshot, jamais son existence.

**Garde à deux niveaux**, et les deux sont nécessaires :

1. le **producteur** n'enfile pas la tâche — économie, pas garantie ;
2. le **worker** revérifie — c'est lui qui tient l'invariant, car la garde du
   producteur ne couvre ni une tâche en retard, ni un appel direct, ni un
   producteur d'une version antérieure, ni une bascule de configuration entre
   l'enfilement et l'exécution. Il renvoie alors `diagnostics_disabled` et émet
   `feedback_snapshot_skipped reason=diagnostics_disabled`.

> **Pourquoi cette garde existe.** Sans elle, un clic produisait un snapshot
> `partial` même avec les deux drapeaux à `0` : une **seconde copie** de la
> question, de la réponse et des extraits, conservée 30 jours. Un opérateur ayant
> désactivé le diagnostic collectait plus qu'annoncé, et le rollback du lot 2
> était incomplet. Constaté en smoke test **production** du 2026-07-28.
>
> Contrepartie assumée : le snapshot `partial`, qui préserve la question et les
> sources après la purge du journal, n'est plus produit implicitement. Cette
> conservation doit résulter d'une capacité **explicitement activée**.

Désactiver n'efface **rien** : les snapshots antérieurs vivent jusqu'à leur purge,
et les incidents déjà capturés restent exportables.

### Idempotence de la capture

La tâche est appelable plusieurs fois sans dommage. Deux niveaux, et le second
compte autant que le premier :

1. **séquentiel** — si le feedback porte déjà un snapshot, la tâche sort en
   `already_captured` ;
2. **concurrent** — la garde ci-dessus lit une relation *mise en cache* sur
   l'instance : elle ne voit pas un snapshot créé entre le chargement et
   l'écriture. La contrainte d'unicité en base est le vrai garde-fou, et la
   tâche la **reconnaît** : `IntegrityError` devient `already_captured`.

Sans ce second niveau, un doublon partait dans `autoretry_for=(Exception,)`,
échouait trois fois avec backoff, et finissait en tâche en erreur — alors que le
travail était fait et la base cohérente. Constaté en qualification staging du
2026-07-28 avec un worker réel.

## 9. Rétention

| Objet | Horizon | Purge |
|---|---|---|
| `AnswerFeedback` | **durable** | aucune purge automatique |
| `FeedbackDiagnosticSnapshot` | **30 j**, prolongeable à 90 j | tâche Celery `purge-expired-feedback-snapshots` |
| Tampon Redis | 48 h | expiration native, aucune purge |
| Paquet d'export | 30 j (`expires_at` du manifeste) | refus à l'import au-delà |

Purger le snapshot **ne purge jamais** le signal : c'est l'invariant central du lot, et
il est testé. Après purge, l'incident reste visible, priorisable et regroupable ; seul le
matériel sensible a disparu.

La purge des snapshots est **séparée** de `apps.audit.retention` à dessein : cette
dernière calcule un seuil global (`created_at` − N jours), alors qu'un snapshot porte sa
propre échéance (`expires_at`) — c'est ce qui rend possible la prolongation, tracée, d'un
incident précis sans toucher à la règle globale.

---

## 10. Observabilité

Événements structurés émis (métadonnées seules) : `feedback_created`,
`feedback_snapshot_created`, `feedback_snapshot_partial`, `feedback_buffer_miss`,
`feedback_export_approved`, `feedback_exported`, `feedback_replay_completed`,
`feedback_status_changed`, `feedback_snapshot_skipped` (avec `reason=diagnostics_disabled`).

Champs autorisés : `feedback_id`, `capture_mode`, `status`, `duration_ms`,
`candidate_count`, `snapshot_size`, `truncated`, `model`, `provider`, `error_code`.
**Interdits** : question, réponse, prompt, extrait, commentaire, chemin, secret.

Toutes les opérations de gouvernance (qualification, autorisation, génération,
téléchargement, prolongation de rétention, clôture) émettent un `ActionAuditEvent`
append-only via `platform_audit`, avec repli direct hors contexte HTTP — sans quoi une
opération lancée en commande de gestion serait silencieusement non auditée.

---

## 11. Déploiement et rollback

| Phase | Contenu | Vérification |
|---|---|---|
| 1 | Lot 1 seul, aucun tampon | création et **survie** des feedbacks |
| 2 | Lot 2, flag OFF puis ON sur un espace de test | Redis, snapshots, purge |
| 3 | Lot 3, exports **niveau 1 en staging uniquement** | audit, rédaction |
| 4 | Lot 4 en dev, incidents **synthétiques** d'abord | les trois modes de replay |

### Deux réglages à poser AVANT la phase 1 en environnement déployé

Deux system checks (**avertissements**, jamais bloquants) les signalent au démarrage en
`staging`/`prod` :

| Check | Réglage | Pourquoi ça ne se rattrape pas après coup |
|---|---|---|
| `feedback.W001` | `APP_GIT_SHA` | Le repli `git rev-parse` échoue dans une image sans `.git` (release `git archive`, build Docker). Les incidents porteraient une révision vide, et « corrigé depuis quelle version ? » resterait sans réponse. Les composes déployés transmettent désormais `APP_GIT_SHA: ${APP_GIT_SHA:-}`. |
| `feedback.W002` | `FEEDBACK_PSEUDONYM_SALT` | Le repli sur `SECRET_KEY` couple deux usages : une rotation de la clé Django changerait tous les pseudonymes et casserait le regroupement multi-signalants des incidents antérieurs — sans erreur, sans trace. |

**Rollback — par les drapeaux, jamais par `migrate zero` :**

| Lot | Rollback |
|---|---|
| 1 | `FEEDBACK_DUAL_WRITE_RAG_QUERY_LOG=1` reste ; la table demeure, `RagQueryLog.feedback_*` continue d'être écrit |
| 2 | `RAG_FEEDBACK_DIAGNOSTIC_BUFFER_ENABLED=0` **et** `REPLAY_ENABLED=0` ; Redis expire seul ; **plus aucun snapshot n'est créé** ; les snapshots antérieurs vivent jusqu'à leur purge |
| 3 | `FEEDBACK_INCIDENT_EXPORT_ENABLED=0` ; feedbacks intacts |
| 4 | `FEEDBACK_INCIDENT_IMPORT_ENABLED=0` ; aucun impact production |

Les deux migrations sont **additives** (`CreateModel` uniquement). Aucune altération de
`RagQueryLog`, aucun backfill.

---

## 12. Limites connues

- **Le tampon capture le pool POST-FUSION.** Les candidats écartés en amont par les
  seuils, dans le `CandidateRetriever`, ne sont pas observables : `below_threshold`
  n'est donc jamais émis.

  **Formulation imposée dans tout rapport d'incident** :

  | Observation | Conclusion autorisée | Conclusion INTERDITE |
  |---|---|---|
  | chunk présent dans la trace | son rang, son score et l'étape qui l'a écarté sont connus | — |
  | chunk absent de la trace | « **absent du pool post-fusion** » — écarté avant ou pendant le rappel initial | « jamais candidat » |

  On ne peut pas encore distinguer : absence FTS, absence vectorielle, seuil interne du
  `CandidateRetriever`, filtre de périmètre, ou défaut d'indexation. La trace transporte
  elle-même cette frontière (`observation_boundary: "post_fusion"` et
  `absent_candidate_means`), et la commande de replay l'imprime — pour que la règle de
  lecture voyage avec la donnée plutôt que de vivre dans cette page seule.
- **La langue résolue n'est pas persistée** par le pipeline. Le snapshot expose la langue
  *détectée*, recalculée, avec `source: "recomputed"` et `resolved_available: false`.
- **Le prompt complet n'est jamais capturé aujourd'hui**, quel que soit
  `RAG_FEEDBACK_DIAGNOSTIC_CAPTURE_PROMPT` : il n'est persisté nulle part au moment où
  la tâche de promotion s'exécute. `prompt_capture` vaut toujours `hash_only` et
  `prompt-only` n'est jamais annoncé ni exécutable depuis une capture réelle (§8).
  Écart CONNU, non bloquant pour staging ni production — où ce drapeau doit rester
  à `0` de toute façon. Correction = lot de conception dédié, jamais un stockage du
  prompt dans Redis.
- **Admin plateforme = superuser** dans l'instance actuelle (cf. §7).
- **Les niveaux d'export 2 et 3 ne sont pas exposés dans l'admin Django** : ils exigent
  une justification écrite qu'une action de masse ne peut pas recueillir. Ils passent par
  `manage.py export_rag_incident --justification …`.
- **Aucun mini-RagSpace n'est créé automatiquement** à l'import : le replay `retrieval`
  exige un `--rag-space` déjà peuplé.

---

## Références d'implémentation

- [`backend/apps/feedback/models.py`](../../backend/apps/feedback/models.py) — `AnswerFeedback`, `FeedbackDiagnosticSnapshot`
- [`backend/apps/feedback/services/create_feedback.py`](../../backend/apps/feedback/services/create_feedback.py), [`identity.py`](../../backend/apps/feedback/services/identity.py), [`priority.py`](../../backend/apps/feedback/services/priority.py), [`transitions.py`](../../backend/apps/feedback/services/transitions.py), [`export_service.py`](../../backend/apps/feedback/services/export_service.py)
- [`backend/apps/feedback/diagnostics/`](../../backend/apps/feedback/diagnostics/) — `capture.py`, `buffer.py`, `snapshot.py`, `conversation.py`, `reasons.py`, `hook.py`
- [`backend/apps/feedback/export/`](../../backend/apps/feedback/export/) — `package.py`, `levels.py`, `redaction.py`, `crypto.py`
- [`backend/apps/feedback/tasks.py`](../../backend/apps/feedback/tasks.py), [`retention.py`](../../backend/apps/feedback/retention.py), [`admin.py`](../../backend/apps/feedback/admin.py), [`permissions.py`](../../backend/apps/feedback/permissions.py)
- [`backend/apps/rag_core/services/query/orchestrator.py`](../../backend/apps/rag_core/services/query/orchestrator.py) — `_capture_feedback_trace`, paramètre `trace_sink`
- [`backend/api/v1/chat/views.py`](../../backend/api/v1/chat/views.py) — `ChatFeedbackView`, `ChatFeedbackDetailView`
- [`frontend/src/app/chat/_components/MessageActions.tsx`](../../frontend/src/app/chat/_components/MessageActions.tsx)
- Commandes : [`export_rag_incident`](../../backend/apps/feedback/management/commands/export_rag_incident.py), [`import_rag_incident`](../../backend/apps/feedback/management/commands/import_rag_incident.py), [`replay_rag_incident`](../../backend/apps/feedback/management/commands/replay_rag_incident.py), [`promote_rag_incident_to_fixture`](../../backend/apps/feedback/management/commands/promote_rag_incident_to_fixture.py)
- Tests : `tests/tests_root/feedback/` (API, survie, transitions, tampon, snapshot, intégration, export, replay, invariants de sécurité), `tests/frontend/components/MessageActions.test.tsx`
