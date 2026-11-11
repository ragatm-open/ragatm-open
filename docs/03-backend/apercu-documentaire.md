# Aperçu documentaire — visualiser la source d'une citation DOCX

Objectif : décrire l'endpoint qui rend une **fenêtre de blocs** autour du passage
cité par une source de chat, sans jamais envoyer le document entier ni prétendre
reproduire la mise en page Word.
Public : développeurs backend. Source de vérité = le code.

> **Statut** : *implémenté*, **activable par flag** — `DOCUMENT_DOCX_PREVIEW_ENABLED`.
> Fermé, un DOCX retombe sur l'état explicite (nom, format, téléchargement).

Côté interface : [../04-frontend/visualiseur-documents.md](../04-frontend/visualiseur-documents.md).
Aperçu tabulaire (XLSX) : [apercu-tabulaire.md](apercu-tabulaire.md).
Ingestion : [ingestion-extraction.md](ingestion-extraction.md).

---

## 1. Le problème, et pourquoi il n'est pas celui du XLSX

Un classeur porte sa localisation **en base** : `Chunk.meta` contient feuille,
plage, lignes et colonnes. Un DOCX ne porte **rien** de tel.

`DocxExtractor` produit bien des blocs typés (titre, paragraphe, liste, tableau),
mais `indexing.py` ne consulte jamais `result.blocks` : le texte est aplati par
`join_blocks_text` puis découpé **au caractère** par le chunker générique. Ce qui
survit en base se réduit à deux entiers — `char_start` et `char_end`.

La question était donc : peut-on retrouver la structure sans la persister ?

**Oui, et c'est prouvé par test.** Trois propriétés le permettent :

1. l'extraction est **déterministe** — mêmes octets, mêmes blocs, même texte ;
2. `join_blocks_text` est une fonction **pure et simple** :
   `"\n\n".join(blocs non vides strippés)` ;
3. les offsets persistés désignent des positions dans ce texte.

On réextrait donc à la demande, on recalcule les bornes de chaque bloc, et on
projette les offsets dessus. **Aucune migration, aucune réindexation, et tous les
DOCX déjà ingérés deviennent visualisables immédiatement.**

```
GET /api/v1/documents/{document_id}/document-preview/
    ?chunk_id=…&context_before=10&context_after=20
```

---

## 2. Séparation des autorités

| Question | Autorité |
|---|---|
| Quel document ? | `document_id` |
| Quelle version binaire ? | `Document.sha256` |
| Quel passage ? | `chunk_id` |
| Quelles bornes ? | `Chunk.meta.char_start` / `char_end` |
| Quel texte afficher ? | le **même extracteur** que l'ingestion |
| Quelle structure ? | blocs recalculés par ce même extracteur |

**Le snippet n'est jamais analysé.** Il n'entre dans aucune décision — ce qui
importe d'autant plus ici qu'un document contient couramment le même texte à
plusieurs endroits : une recherche textuelle choisirait la mauvaise occurrence,
et rien ne le signalerait.

---

## 3. Fidélité — deux invariants, vérifiés par test

**Invariant 1 — les offsets reconstruisent le chunk :**

```
result.text[char_start:char_end].strip() == chunk.text
```

Vérifié caractère par caractère sur cinq configurations de chunking
(120/30, 250/50, 2500/250, 80/0, 60/20), sur un corpus couvrant accents, Unicode
et CJK, espaces insécables, tabulations, paragraphes vides, listes, tableaux,
cellules vides et **texte répété à deux positions**.

**Invariant 2 — le surlignage reconstruit le chunk :**

> La concaténation des fragments surlignés, séparés par `\n\n`, est exactement
> `chunk.text`.

C'est l'invariant qui porte sur le **rendu** : ce que l'utilisateur voit marqué
est exactement la source citée, ni plus, ni moins.

### Le surlignage est au caractère

Les chunks sont découpés **sans égard pour les frontières de blocs**. Le premier
et le dernier bloc d'une citation ne sont donc couverts que **partiellement** :

```
highlight_start = max(char_start, block_start) - block_start
highlight_end   = min(char_end,   block_end)   - block_start
```

Marquer ces blocs en entier étendrait la citation à du texte qu'elle ne porte
pas. La précision est ici **meilleure** que celle du tabulaire, qui s'arrête à la
cellule.

---

## 4. Garde d'intégrité — la condition de sûreté

Toute la stratégie repose sur la reproductibilité de l'extraction. Si le texte
réextrait diffère de celui qui a servi à l'indexation, les offsets désignent
autre chose — et l'aperçu surlignerait le mauvais passage **sans que rien
n'échoue**. Trois gardes, complémentaires :

| Garde | Portée | Rôle |
|---|---|---|
| `extractor_version` (`docx-v1`) | documents ingérés depuis ce lot | précise ; refuse toute version différente |
| Compteurs `chars` / `blocks` | **tous**, y compris historiques | déjà persistés par `finalize()` ; détectent un texte de longueur ou de structure différente |
| Reconstruction == texte extrait | toujours | `index_blocks` rejoue la jointure et **compare** ; une évolution de la règle lève au lieu de décaler |

Aucune ne suffit seule : la version est absente de l'existant, les compteurs sont
moins précis qu'une version, et la reconstruction ne voit pas un changement de
contenu à longueur égale. Ensemble, elles couvrent l'existant **sans migration**
et les nouveaux documents avec précision.

Un document sans métadonnées d'extraction rend `preview_unavailable` : sans
repère, rien ne garantit la position, et refuser vaut mieux qu'approximer.

---

## 5. Blocs — ce qui est rendu, et ce qui est déclaré

Quatre types seulement, parce que l'extracteur n'en produit pas d'autres.
Déclarer `page_break`, `image` ou `section` serait annoncer ce qui n'existe pas.

| Type | Rendu | Fidélité déclarée |
|---|---|---|
| `heading` | titre, niveau borné 1–6 à l'affichage | heuristique sur le préfixe du `styleId` (voir §6) |
| `paragraph` | texte, `pre-wrap` | — |
| `list` | **texte à plat** | `list_fidelity: "flattened"` |
| `table` | **`<pre>` monospacé** | `render_mode: "preformatted"` + `table_simplified` |

### Pourquoi les tableaux ne sont pas des `<table>`

L'extraction linéarise en `cellule | cellule` et **ignore les fusions**
(`gridSpan`, `vMerge`) : les lignes n'ont donc pas toutes le même nombre de
champs. Pire, une cellule vide **en tête de ligne** perd sa position, parce que
`_table_text` applique `.strip()` à la ligne jointe — `" | vide"` devient
`"| vide"`. Un test verrouille ce comportement.

Reconstruire une grille à partir de ce texte placerait des valeurs dans les
mauvaises colonnes, **en silence**. Le préformaté montre exactement ce qui a été
indexé, et le dit.

### Pourquoi les listes sont à plat

Le pipeline agrège tous les items consécutifs en un seul bloc, avec une puce
uniforme `- `. Ni la numérotation, ni le niveau (`ilvl`), ni la frontière entre
deux listes distinctes (`numId`) ne survivent — `numbering.xml` n'est jamais lu.

---

## 6. Titres et navigation

Le sommaire est construit à partir des seuls blocs que l'extracteur a classés en
titre. **Aucune heuristique n'est ajoutée dans ce lot.**

La détection existante est fragile, et il faut le savoir : elle teste le préfixe
du `styleId` (`heading` / `titre` / `title`) et lit son premier chiffre. Elle
manque donc les titres portés par `outlineLvl`, ceux des documents non anglophones
(`Überschrift1`, `Título1`), et accepte à tort un style maison nommé
`TitreSociete`.

L'améliorer **changerait le texte extrait**, donc les offsets de tous les chunks
existants. C'est le lot 2, pas celui-ci. Quand aucun titre n'est détecté,
`headings_not_detected` est émis et l'interface le dit — plutôt que de présenter
comme fiable une navigation qui ne l'est pas.

**Aucune navigation par page.** Les sauts de page explicites eux-mêmes sont
perdus : `_para_text` mappe **tout** `w:br` sur `\n` sans lire son `w:type`. Un
saut de page est donc indiscernable d'un saut de ligne. Et la pagination calculée
par Word n'est pas une donnée du fichier — elle dépend des polices, des marges et
du pilote d'impression.

---

## 7. Ce qui n'est pas affiché — et pourquoi

En-têtes, pieds de page, notes de bas de page, notes de fin, commentaires et
images ne sont **pas indexés** : ils vivent dans des parties du conteneur DOCX que
l'extracteur ne lit jamais (seul `word/document.xml` est ouvert).

Les afficher exposerait un contenu que le RAG a écarté — la même doctrine que les
feuilles `veryHidden` du tabulaire. Ce n'est donc pas une limitation à regretter,
c'est une **cohérence à préserver** : l'aperçu montre le contenu *indexé*.

Deux pertes silencieuses supplémentaires, mesurées : le contenu d'un `w:sdt` au
niveau du corps et celui d'un **tableau imbriqué** ne sont pas extraits, donc pas
affichés. Elles relèvent du lot 2.

Aucun champ Word n'est recalculé : `instrText` n'est pas lu, seule la **valeur en
cache** est reprise. Le texte des révisions supprimées (`w:delText`) n'est pas
capté — l'aperçu montre le **texte final accepté**.

---

## 8. Bornes et mesures

| Borne | Défaut | Maximum |
|---|---|---|
| `DOCX_PREVIEW_CONTEXT_BEFORE` / `_MAX` | 10 | 50 |
| `DOCX_PREVIEW_CONTEXT_AFTER` / `_MAX` | 20 | 100 |
| `DOCX_PREVIEW_MAX_BLOCKS` | 300 | — |
| `DOCX_PREVIEW_MAX_BLOCK_CHARS` (affichage) | 2 000 | — |
| `DOCX_PREVIEW_MAX_TOTAL_CHARS` (**cardinale**) | 400 000 | — |
| `DOCX_PREVIEW_MAX_HEADINGS` | 500 | — |
| `DOCX_PREVIEW_MAX_DOCUMENT_BLOCKS` | 50 000 | — |

Banc : `python manage.py bench_document_preview --skip-checks`.

| Cas | Blocs | Caractères | p50 | p95 | Pic RSS | Charge utile |
|---|---|---|---|---|---|---|
| ~10 pages | 106 | 10 958 | 5 ms | 6 ms | 0,2 Mo | 7,0 Ko |
| ~100 pages | 1 036 | 108 801 | 37 ms | 37 ms | 1,7 Mo | 7,1 Ko |
| ~500 pages | 5 120 | 545 815 | 176 ms | 186 ms | 7,6 Mo | 7,1 Ko |
| **~1000 pages** | 10 240 | 1 092 785 | **362 ms** | 368 ms | 15,2 Mo | 7,1 Ko |
| 400 titres | 2 399 | 220 255 | 73 ms | 74 ms | 3,2 Mo | 6,6 Ko |
| 200 tableaux | 1 209 | 153 205 | 295 ms | 296 ms | 13,7 Mo | 7,1 Ko |

Décomposition à ~1000 pages : extraction 294 ms · bornes de blocs 66 ms ·
sommaire 0,9 ms · projection 0,4 ms.

Deux enseignements :

1. **Le coût suit la taille du document, pas la position du passage cité.** Il n'y
   a pas d'équivalent du streaming linéaire d'openpyxl : une citation en fin de
   document ne coûte pas plus qu'une citation au début.
2. **La contrainte dominante est la charge utile.** À 1000 pages, la fenêtre
   bornée pèse **7,1 Ko** contre **1 047 Ko** pour le texte entier — un facteur
   147. C'est ce que la fenêtre évite d'envoyer.

Mémoire stable : 30 ouvertures successives donnent une dérive de **−0,00 Mo**.

---

## 9. Codes de refus

**Aucune erreur n'est convertie en résultat vide**, et un document vide n'est pas
une erreur : il rend `200` avec `blocks: []` et `total_blocks: 0`.

| `reason_code` | HTTP | Sens |
|---|---|---|
| `not_found` | 404 | inexistant **ou** hors périmètre (indiscernables) |
| `no_file_available` | 404 | fichier absent du stockage |
| `chunk_document_mismatch` | 404 | chunk d'un autre document / RagSpace |
| `block_not_found` | 404 | bloc hors document |
| `chunk_stale` | 409 | chunk supprimé — document réindexé |
| `document_version_mismatch` | 409 | extraction incompatible, ou `sha256` inattendu |
| `unsupported_format` | 422 | pas un `.docx` |
| `document_unreadable` | 422 | archive ou XML invalide — **jamais un 500** |
| `preview_unavailable` | 422 | aucune métadonnée d'extraction exploitable |
| `invalid_chunk_offsets` | 422 | offsets absents ou incohérents |
| `document_too_large` | 413 | au-delà des bornes |
| `invalid_block_range` | 400 | fenêtre invalide |
| `context_limit_exceeded` | 400 | contexte excessif |
| `preview_timeout` | 504 | budget dépassé |

`chunk_stale` est le plus important : retomber sur le début du document
afficherait une source fausse avec l'apparence d'une source vraie.

---

## 10. Sécurité

- **Permission d'abord.** `load_accessible_document` — la même porte que le
  téléchargement — avant lecture de fichier, de cache, de chunk. Aucune règle
  d'autorisation n'est réinventée, aucune permission propre au DOCX.
- **Le chunk est résolu AVANT le cache.** Sans cela, un aperçu survivrait à la
  réindexation qui a supprimé son chunk, pendant tout le TTL. Verrouillé par test.
- **IDOR** : appartenance au document **et** au RagSpace vérifiées.
- **Lecture ZIP bornée** (`read_entry_strict`) : `guard_zip_archive` ne peut
  vérifier que les tailles **déclarées** dans le répertoire central, et `file_size`
  est écrit par le fichier lui-même. Une archive qui sous-déclare une entrée
  passait la garde puis se décompressait sans limite. La lecture est désormais
  bornée **pendant** la décompression.
- **Profondeur XML bornée** (`reject_deeply_nested_xml`, 200 niveaux) : quelques
  kilo-octets suffisent à épuiser la pile d'expat, et aucune garde de **taille** ne
  le voit. `XMLPullParser` plutôt qu'un scan d'octets — un `>` non échappé est
  légal dans une valeur d'attribut et un scan naïf compterait faux.
- **Relations externes** : `.rels` n'est jamais lu, donc rien n'est chargeable.
- **Macros / OLE** : `.docm` refusé par contenu ; rien n'est jamais exécuté.
- **Contenu de bloc** : transporté comme texte, échappé par le rendu React.
- **Journalisation** : identifiants, bornes, durée, nombre de blocs. **Jamais** de
  texte de document — vérifié par un test à canari.

---

## 11. Hors périmètre et suites

**Lot 2 — extraction** (change `res.text`, donc **bump de version + réindexation
contrôlée**) : `outlineLvl` et `styles.xml`, `numbering.xml` (`numId`/`ilvl`),
`w:br@type="page"`, descente dans les `w:sdt`, tableaux imbriqués, `gridSpan` /
`vMerge`, warnings d'extraction, rail tabulaire atomique.

**Lot 3 — tableaux structurés et images** : cellules structurées et fusions avant
tout `<table>` ; relations, position, type MIME, taille bornée, texte alternatif
et endpoint média protégé avant toute image.

---

## Références d'implémentation

- `backend/apps/rag_core/services/preview/document_service.py` (orchestration)
- `backend/apps/rag_core/services/preview/document_blocks.py` (bornes, projection)
- `backend/apps/rag_core/services/preview/{config,errors}.py`
- `backend/api/v1/documents/views_document_preview.py`, `backend/api/v1/urls.py`
- `backend/apps/rag_core/services/ingest/extraction/docx_extractor.py`
  (`EXTRACTOR_VERSION`, lecture bornée, garde de profondeur)
- `backend/apps/rag_core/services/ingest/extraction/{zip_security,xml_security}.py`
- `backend/apps/rag_core/management/commands/bench_document_preview.py`
- Tests : `tests/tests_root/unit/preview/test_document_preview_fidelity.py` (oracle),
  `tests/tests_root/http_api/test_documents_document_preview.py`,
  `tests/tests_root/integration/test_docx_source_to_preview.py`
