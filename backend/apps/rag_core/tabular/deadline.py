"""Échéance ABSOLUE unique, partagée par toutes les phases d'une requête.

Le défaut que ce module supprime : chaque phase créait son propre budget complet à
partir de `TABULAR_MAX_QUERY_SECONDS`. Une requête filtrée passait par `execute` puis
`count_selected`, chacun ouvrant un budget entier — mesuré **2,0× le budget annoncé**,
et cela sans compter le catalogue, la lecture et le typage, entièrement hors budget.

Un plafond qu'on peut multiplier par le nombre de phases n'est pas un plafond.

Le contrat tient en une phrase : **une requête, une échéance**. Chaque phase consomme
le temps restant ; aucune n'en recrée.

Quelques choix explicites :

- `time.monotonic()`, jamais l'horloge murale : un ajustement NTP ne doit pas allonger
  ni raccourcir un budget en cours ;
- **aucun thread, aucun minuteur** dans cet objet — il est purement passif. Le contrôle
  est coopératif : les phases appellent `checkpoint()` là où l'arrêt est propre. C'est
  le seul mécanisme fiable pour une boucle Python, et cela évite un thread par requête ;
- horloge **injectable**, pour que les tests n'aient pas à dormir ;
- aucun état partagé entre requêtes : l'objet est créé par requête et transmis
  explicitement, jamais lu depuis une variable de module.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from apps.rag_core.tabular import errors as E

#: Budget plancher. En deçà, aucune requête ne peut aboutir et le refus serait plus
#: coûteux à produire que la requête elle-même.
MIN_BUDGET_SECONDS = 0.05


@dataclass
class Deadline:
    """Échéance absolue d'une requête, avec trace du temps par phase."""

    budget_seconds: float
    #: Horloge injectable — les tests fournissent la leur plutôt que de dormir.
    clock: Callable[[], float] = time.monotonic
    started_at: float = field(default=0.0)
    #: Temps cumulé par phase. Diagnostic uniquement : aucun contenu utilisateur.
    spent: dict[str, float] = field(default_factory=dict)
    _last_mark: float = field(default=0.0)

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = self.clock()
        self._last_mark = self.started_at

    # -- construction ---------------------------------------------------- #
    @classmethod
    def from_timeout(cls, seconds: float, *, clock: Callable[[], float] = time.monotonic
                     ) -> "Deadline":
        return cls(budget_seconds=max(MIN_BUDGET_SECONDS, float(seconds)), clock=clock)

    @classmethod
    def from_limits(cls, limits, *, clock: Callable[[], float] = time.monotonic
                    ) -> "Deadline":
        return cls.from_timeout(getattr(limits, "max_query_seconds", 10.0), clock=clock)

    # -- lecture ---------------------------------------------------------- #
    @property
    def expires_at(self) -> float:
        return self.started_at + self.budget_seconds

    def elapsed_seconds(self) -> float:
        return self.clock() - self.started_at

    def remaining_seconds(self) -> float:
        """Temps restant, **jamais négatif** — un budget négatif n'a pas de sens et
        serait interprété comme « infini » par la plupart des API qui le reçoivent."""
        return max(0.0, self.expires_at - self.clock())

    def expired(self) -> bool:
        return self.clock() >= self.expires_at

    # -- contrôle ---------------------------------------------------------- #
    def raise_if_expired(self, *, phase: str = "") -> None:
        """Lève `query_timeout` si l'échéance est franchie.

        La phase est **technique** (`canonical_csv_write`, `duckdb_import`…) : elle
        aide au diagnostic sans révéler ni document, ni question, ni contenu.
        """
        if self.expired():
            raise E.TabularLimitExceeded(
                E.ERR_QUERY_TIMEOUT,
                f"budget de {self.budget_seconds:.3g} s épuisé",
                phase=phase or "unknown")

    #: Alias : `checkpoint` se lit mieux au fil d'une boucle.
    checkpoint = raise_if_expired

    def mark(self, phase: str) -> None:
        """Impute à `phase` le temps écoulé depuis le dernier repère."""
        maintenant = self.clock()
        self.spent[phase] = self.spent.get(phase, 0.0) + (maintenant - self._last_mark)
        self._last_mark = maintenant

    # -- diagnostic --------------------------------------------------------- #
    def timings_ms(self) -> dict[str, float]:
        return {k: round(v * 1000, 2) for k, v in self.spent.items()}

    def summary(self) -> dict:
        """Résumé journalisable : aucune donnée utilisateur."""
        return {
            "budget_s": round(self.budget_seconds, 3),
            "elapsed_s": round(self.elapsed_seconds(), 3),
            "remaining_s": round(self.remaining_seconds(), 3),
            "phases_ms": self.timings_ms(),
        }


def ensure(deadline: Deadline | None, limits) -> Deadline:
    """Renvoie l'échéance fournie, ou en crée une depuis les limites.

    Le repli existe pour les appels directs (tests, banc de mesure) et ne doit PAS
    servir de porte d'entrée normale : sur le chemin de production, l'échéance est
    créée une seule fois par le service et transmise explicitement. Un repli utilisé
    au milieu du pipeline recréerait exactement le budget que ce module supprime.
    """
    return deadline if deadline is not None else Deadline.from_limits(limits)
