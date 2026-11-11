# audit

## Emplacement
- Chemin (relatif racine repo) : `backend/apps/audit`
- Chemin (absolu) : `<racine du dépôt>`


## Rôle
Traçabilité des actions : qui a fait quoi, quand, et sur quel espace/document/chat.

## Contenu attendu
- événements audit (create/read/update/delete)
- logs structurés (minimiser PII)
- export/rotation (via scripts prod)

## Contraintes RAG-ATM
- Logs conservés 1 mois (selon tes specs)
- Privacy d’abord : éviter de logger du contenu sensible

