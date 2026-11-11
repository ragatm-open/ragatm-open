# config/settings

## Emplacement
- Chemin (relatif racine repo) : `backend/config/settings`
- Chemin (absolu) : `<racine du dépôt>`


## Rôle
Settings Django par environnement.

## Contenu attendu
- dev.py : dev local
- prod.py : prod
- base.py : commun

## Contraintes
- Séparer strictement dev/prod
- Logs configurables, pas de données sensibles dans les logs

