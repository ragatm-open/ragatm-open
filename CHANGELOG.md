# Changelog — RAGatm Open

Toutes les évolutions notables de ce dépôt sont consignées ici (format inspiré de Keep a Changelog ; versions SemVer).

## [Unreleased]

### Ajouté
- Première publication candidate de l'édition Open : code source, licence Apache-2.0, notices de tiers,
  politique de sécurité, guide de contribution, workflows CI, profils de tests `fast` / `standard` / `full`.

### Modifié
- Suites de tests rationalisées sans perte de couverture : 2 doublons stricts retirés (propriété conservée dans
  `tests/tests_root/http_api/test_access_control.py`), fixtures `superuser` factorisées, 6 groupes de tests
  paramétrés (mêmes cas, moins de définitions), profils FAST / STANDARD / FULL explicites (`make test-fast`,
  `make test-standard`, `make test-full`, option pytest `--no-db`).

### Sécurité
- Dépendances épinglées par empreintes, images de base référencées par digest, exécution des conteneurs sous un
  utilisateur non privilégié.
