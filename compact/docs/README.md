# RAGatm Open Compact — documentation d'installation

Installation autonome de RAGatm Open avec AI Gateway Open et de vrais modèles locaux, sans
AI Gateway Pool et sans aucun fournisseur d'IA externe. Point d'entrée produit : le dépôt
`ragatm-open` (ce répertoire `compact/` est son répertoire de déploiement générique ; il référence un clone
compatible d'[ai-gateway-open](https://github.com/ragatm-open/ai-gateway-open) par `AI_GATEWAY_OPEN_DIR` — aucun quatrième dépôt n'est créé).

| Document | Contenu |
|---|---|
| [installation.md](installation.md) | prérequis, préflight, secrets, construction des images, parcours réellement testé |
| [approvisionnement-modeles.md](approvisionnement-modeles.md) | téléchargement explicite des modèles (phase distincte de l'exécution nominale), digests, profils 8 Go / 12 Go |
| [exploitation.md](exploitation.md) | démarrage, arrêt, premier compte, diagnostic non inférentiel, erreurs explicites (503/502), diagnostic de retrieval, journaux |
| [sauvegarde-restauration.md](sauvegarde-restauration.md) | procédure de sauvegarde et de restauration dans une instance isolée, limites liées aux jobs |
| [configuration.md](configuration.md) | variables de `.env`, `config/backend.env`, `config/gateway.env`, réseau, budgets mémoire |
| [limites-et-capacites.md](limites-et-capacites.md) | limites matérielles connues, résultats de qualification, capacités désactivées |

Statut : candidat de publication en cours de validation. Les licences `LICENSE`/`NOTICE` restent à
poser par le titulaire des droits.
