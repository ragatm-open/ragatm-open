"""Sous-ensemble de ``scipy.ndimage`` utilisé par ``curve_digitization``.

Le prototype figé du lot 13B appelle ``ndimage.label(mask, structure=np.ones((3, 3)))`` puis
``ndimage.find_objects(labels)``. scipy n'est pas installé dans les images RagATM : plutôt
qu'une dépendance de ~40 Mo pour deux fonctions, ce module en reproduit la sémantique
EXACTE sur ce seul cas (masque booléen 2-D, connexité 8) :

* ``label`` : composantes connexes 8-voisines, numérotées 1..n dans l'ordre de leur
  premier pixel en balayage ligne par ligne (même numérotation que scipy) ;
* ``find_objects`` : pour chaque étiquette, ``(slice(lignes), slice(colonnes))`` de sa
  boîte englobante.

Toute autre structure est refusée (``ValueError``) : le module ne prétend pas couvrir
scipy. L'équivalence est vérifiée par les tests (cas construits, et scipy lui-même
lorsqu'il est installé) et par le rejeu bit à bit des sorties du lot 13B.
"""
from __future__ import annotations

from typing import List, Tuple

import numpy as np


def _runs(row: np.ndarray) -> List[Tuple[int, int]]:
    """Segments ``[début, fin]`` (inclus) de pixels vrais d'une ligne."""
    padded = np.concatenate(([False], row, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(edges[i]), int(edges[i + 1]) - 1) for i in range(0, len(edges), 2)]


def label(mask, structure=None):
    """Étiquetage 8-connexe d'un masque 2-D. Rend ``(labels int32, nombre)``."""
    mask = np.asarray(mask, dtype=bool)
    if mask.ndim != 2:
        raise ValueError("curve_ndimage.label : masque 2-D seulement")
    if structure is not None and not np.array_equal(np.asarray(structure), np.ones((3, 3))):
        raise ValueError("curve_ndimage.label : seule la connexité 8 (np.ones((3, 3))) est prise en charge")
    parent: List[int] = []

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    runs = []  # (ligne, début, fin, identifiant de segment)
    previous: List[Tuple[int, int, int]] = []
    for y in range(mask.shape[0]):
        current = []
        for start, end in _runs(mask[y]):
            rid = len(parent)
            parent.append(rid)
            for p_start, p_end, p_id in previous:
                if p_start <= end + 1 and p_end >= start - 1:
                    a, b = find(rid), find(p_id)
                    if a != b:
                        parent[max(a, b)] = min(a, b)
            current.append((start, end, rid))
            runs.append((y, start, end, rid))
        previous = current
    labels = np.zeros(mask.shape, dtype=np.int32)
    numbering: dict = {}
    for y, start, end, rid in runs:  # ordre de balayage : premier pixel de chaque composante
        root = find(rid)
        if root not in numbering:
            numbering[root] = len(numbering) + 1
        labels[y, start:end + 1] = numbering[root]
    return labels, len(numbering)


def find_objects(labels) -> list:
    """Boîtes englobantes ``(slice(lignes), slice(colonnes))`` des étiquettes 1..max."""
    labels = np.asarray(labels)
    n = int(labels.max()) if labels.size else 0
    out: list = [None] * n
    ys, xs = np.nonzero(labels)
    if not len(ys):
        return out
    vals = labels[ys, xs] - 1
    big = np.iinfo(np.int64).max
    y0, x0 = np.full(n, big, dtype=np.int64), np.full(n, big, dtype=np.int64)
    y1, x1 = np.full(n, -1, dtype=np.int64), np.full(n, -1, dtype=np.int64)
    np.minimum.at(y0, vals, ys)
    np.minimum.at(x0, vals, xs)
    np.maximum.at(y1, vals, ys)
    np.maximum.at(x1, vals, xs)
    for k in range(n):
        if y1[k] >= 0:
            out[k] = (slice(int(y0[k]), int(y1[k]) + 1), slice(int(x0[k]), int(x1[k]) + 1))
    return out
