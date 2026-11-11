# backend/apps/rag_core/services/ingest/extraction/table_chunking.py
"""Fragmentation bornée d'un tableau Markdown, par lignes logiques.

Un tableau structuré devenait **un chunk atomique jamais refragmenté**, borné par
une simple troncature. Deux conséquences, mesurées sur staging en R3 :

* la troncature perdait la fin du tableau en silence — 22 824 caractères ramenés
  à 12 000, soit 47 % du contenu, sans que rien ne le signale ;
* la borne valait 12 000 alors que le modèle d'embeddings gouverné de l'espace en
  acceptait 6 000. Le chunk partait quand même, la passerelle le refusait, et
  c'est le **document entier** qui basculait en `FAILED` — 68 chunks valides
  perdus pour un seul tableau.

Ce module remplace la troncature par un découpage qui suit la structure. Trois
règles, dans cet ordre :

1. un tableau qui tient dans le budget sort **inchangé**, en un seul fragment ;
2. sinon on empile des **lignes entières**, en répétant l'en-tête, parce qu'un
   fragment de valeurs sans ses colonnes ne veut plus rien dire ;
3. on ne coupe à l'intérieur d'une ligne que lorsqu'aucune frontière de ligne
   n'existe — c'est-à-dire quand une ligne seule dépasse déjà le budget.

Aucun fragment ne sort au-dessus du budget, quel que soit le chemin emprunté : la
garantie est vérifiée avant de rendre la main, pas seulement espérée de la
construction.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

#: Ligne de séparation Markdown (`|---|---|`). Elle appartient à l'en-tête : la
#: détacher produirait des fragments que ni un lecteur ni un parseur ne relisent.
_SEPARATEUR = re.compile(r"^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$")


@dataclass
class Fragmentation:
    """Résultat du découpage. `tronque` n'est jamais silencieux : il remonte."""

    fragments: List[str] = field(default_factory=list)
    entete_lignes: int = 0
    lignes_donnees: int = 0
    lignes_emises: int = 0
    tronque: bool = False
    raison: Optional[str] = None

    @property
    def complet(self) -> bool:
        return not self.tronque and self.lignes_emises == self.lignes_donnees


def _separer_entete(lignes: Sequence[str]) -> int:
    """Nombre de lignes d'en-tête en tête de tableau (0, 1 ou 2)."""
    if not lignes:
        return 0
    if len(lignes) >= 2 and _SEPARATEUR.match(lignes[1]) and not _SEPARATEUR.match(lignes[0]):
        return 2
    return 1 if not _SEPARATEUR.match(lignes[0]) else 0


def _couper_ligne(ligne: str, budget: int) -> List[str]:
    """Coupe UNE ligne trop longue, aux frontières de cellules quand il y en a.

    Dernier recours du §13 : une ligne seule plus grande que le budget. On coupe
    sur les barres verticales, qui sont les seules frontières porteuses de sens
    qui restent, et sur les caractères uniquement si une cellule les dépasse à
    elle seule.
    """
    if len(ligne) <= budget:
        return [ligne]
    morceaux, courant = [], ""
    for cellule in ligne.split("|"):
        piece = ("|" if courant else "") + cellule
        if courant and len(courant) + len(piece) > budget:
            morceaux.append(courant)
            courant = cellule
        else:
            courant += piece
        while len(courant) > budget:  # une seule cellule dépasse : coupe dure
            morceaux.append(courant[:budget])
            courant = courant[budget:]
    if courant:
        morceaux.append(courant)
    return morceaux or [ligne[:budget]]


def fragmenter_tableau_markdown(markdown: str, *, budget: int,
                                max_fragments: int) -> Fragmentation:
    """Découpe `markdown` en fragments d'au plus `budget` caractères.

    `budget` vient du budget de découpage de l'espace, qui applique déjà le
    plafond du modèle gouverné — cette fonction ne recalcule aucune règle de
    gouvernance, elle la reçoit.
    """
    res = Fragmentation()
    texte = (markdown or "").strip()
    if not texte:
        return res

    budget = max(1, int(budget))
    max_fragments = max(1, int(max_fragments))

    # Cas courant, et le seul qui compte pour la non-régression : un tableau qui
    # tient sort tel quel, au caractère près.
    if len(texte) <= budget:
        res.fragments = [texte]
        lignes = texte.splitlines()
        res.entete_lignes = _separer_entete(lignes)
        res.lignes_donnees = res.lignes_emises = max(0, len(lignes) - res.entete_lignes)
        return res

    lignes = texte.splitlines()
    n_ent = _separer_entete(lignes)
    entete = lignes[:n_ent]
    donnees = lignes[n_ent:]
    res.entete_lignes = n_ent
    res.lignes_donnees = len(donnees)

    prefixe = "\n".join(entete)
    # §12 : un en-tête qui ne laisse pas la place à une seule ligne de données ne
    # peut pas être répété. On l'abandonne comme préfixe plutôt que d'émettre un
    # fragment hors budget — le tableau reste lisible, l'en-tête est conservé en
    # tête du premier fragment par le flux normal des lignes.
    repeter_entete = bool(prefixe) and len(prefixe) + 1 < budget
    if prefixe and not repeter_entete:
        donnees = entete + donnees
        prefixe = ""
        res.lignes_donnees = len(donnees)
        res.entete_lignes = 0

    fragments: List[str] = []
    courant: List[str] = []
    taille = 0

    def _fermer() -> None:
        nonlocal courant, taille
        if not courant:
            return
        corps = "\n".join(courant)
        fragments.append(f"{prefixe}\n{corps}" if prefixe else corps)
        courant, taille = [], 0

    base = len(prefixe) + 1 if prefixe else 0
    for ligne in donnees:
        # §13 : la ligne seule ne rentre pas, même dans un fragment vide.
        if base + len(ligne) > budget:
            _fermer()
            for morceau in _couper_ligne(ligne, budget - base if base < budget else budget):
                fragments.append(f"{prefixe}\n{morceau}" if prefixe and base + len(morceau) <= budget
                                 else morceau)
            res.lignes_emises += 1
            continue
        ajout = len(ligne) + (1 if courant else 0)
        if courant and base + taille + ajout > budget:
            _fermer()
            ajout = len(ligne)
        courant.append(ligne)
        taille += ajout
        res.lignes_emises += 1
    _fermer()

    if len(fragments) > max_fragments:
        # Borne d'explosion. On le DIT, au lieu de laisser croire à un tableau complet.
        gardes = fragments[:max_fragments]
        res.tronque = True
        res.raison = f"table_fragments_capped:{len(fragments)}>{max_fragments}"
        fragments = gardes
        res.lignes_emises = -1  # non décompté : la couverture n'est plus exacte

    # Garantie finale, vérifiée et non supposée : AUCUN fragment ne part au-dessus
    # du budget, quel que soit le chemin suivi plus haut.
    surs: List[str] = []
    for f in fragments:
        f = f.strip("\n")
        if len(f) <= budget:
            surs.append(f)
            continue
        surs.extend(m for m in _couper_ligne(f, budget) if m.strip())
        res.raison = res.raison or "hard_split_applied"

    res.fragments = _nettoyer(surs)
    return res


def _porte_du_contenu(fragment: str) -> bool:
    """Reste-t-il autre chose que la charpente du tableau ?

    Découper un tableau finit par isoler des morceaux qui ne contiennent que des
    barres, des tirets et des espaces — typiquement une ligne de séparation restée
    seule. Ce n'est pas une donnée : indexé, cela ne fait qu'ajouter un voisin
    inutile au retrieval.
    """
    return bool(fragment.strip(" \t\n|-:"))


def _nettoyer(fragments: Sequence[str]) -> List[str]:
    """Écarte la charpente vide et les doublons EXACTS, en conservant l'ordre.

    Le doublon n'est pas théorique : sur un bilan réel, MinerU a émis deux fois la
    même ligne d'en-tête fusionnée (4 919 et 4 927 caractères). Découpées, leurs
    premières tranches étaient identiques au caractère. Deux chunks identiques
    portent le même vecteur et la même information — le second n'ajoute rien et
    occupe une place au classement.

    La répétition de l'en-tête EN TÊTE d'un fragment n'est pas concernée : ces
    fragments-là diffèrent par leur corps, donc ne sont pas des doublons exacts.
    """
    vus = set()
    out: List[str] = []
    for f in fragments:
        f = f.strip()
        if not f or not _porte_du_contenu(f) or f in vus:
            continue
        vus.add(f)
        out.append(f)
    return out
