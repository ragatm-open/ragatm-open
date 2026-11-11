"""Témoin d'observation qui FRANCHIT une frontière de processus.

Depuis EXTRACTION-TIMEOUT-HARDENING-03, `extract_document` exécute l'extraction dans
un processus enfant réellement terminable. Un espion qui incrémente une liste ou un
compteur en mémoire est donc muté **dans l'enfant** : le parent ne voit plus rien, et
l'assertion qui s'appuie dessus ne peut plus échouer pour la bonne raison — elle
échoue toujours. Un test qui ne peut plus réussir ne prouve rien, exactement comme un
test qui ne peut plus échouer.

Ce témoin s'appuie sur un fichier : le seul état que parent et enfant partagent
réellement après un `fork`. Les écritures sont en mode « ajout », donc sûres même si
plusieurs processus notent en même temps.
"""
from __future__ import annotations

import os
import tempfile
from typing import List


class TemoinPartage:
    """Liste d'observations partagée entre le processus de test et ses enfants."""

    def __init__(self, prefixe: str = "temoin-"):
        fd, self._chemin = tempfile.mkstemp(prefix=prefixe, suffix=".txt")
        os.close(fd)

    def note(self, valeur: object = "1") -> None:
        """Enregistre une observation. Appelable depuis n'importe quel processus."""
        with open(self._chemin, "a", encoding="utf-8") as fh:
            fh.write(f"{valeur}\n")
            fh.flush()

    @property
    def valeurs(self) -> List[str]:
        try:
            with open(self._chemin, encoding="utf-8") as fh:
                return [l for l in fh.read().splitlines() if l != ""]
        except OSError:
            return []

    def __len__(self) -> int:
        return len(self.valeurs)

    def __bool__(self) -> bool:
        return bool(self.valeurs)

    def __iter__(self):
        return iter(self.valeurs)

    def __eq__(self, autre) -> bool:
        if isinstance(autre, TemoinPartage):
            return self.valeurs == autre.valeurs
        return self.valeurs == list(autre)

    def __repr__(self) -> str:
        return f"TemoinPartage({self.valeurs!r})"
