"""Bornes du CONTRAT D'API — source unique, volontairement non configurables.

Une borne de sécurité qui se desserre par variable d'environnement n'est pas une
borne : c'est la doctrine déjà écrite pour `MAX_REQUEST_ID_CHARS`
(`services/query/settings.py`), et elle s'applique ici pour la même raison.

Ces valeurs vivent dans la couche API parce que ce sont des règles de CONTRAT —
ce que le serveur accepte de recevoir — et non des règles de retrieval.
"""
from __future__ import annotations

#: Longueur maximale d'une question utilisateur, en CARACTÈRES Unicode.
#:
#: Pourquoi une borne existe
#: -------------------------
#: `question` était déclarée `CharField(allow_blank=True)`, sans `max_length`. Les
#: seules limites étaient très en amont — `DATA_UPLOAD_MAX_MEMORY_SIZE` (25 Mo) et
#: `client_max_body_size 50m` côté nginx. Or le classifieur d'intention
#: (`detect_comparative_intent`) s'exécute sur CHAQUE requête, **avant tout
#: retrieval**, et son coût est QUADRATIQUE sur certaines entrées adversariales —
#: mesuré le 04/09/2026 sur une répétition de guillemets droits :
#:
#:     500 car. → 3,6 ms      4 000 car. →   242 ms
#:   1 000 car. → 14,8 ms     8 000 car. →   970 ms
#:   2 000 car. → 60,0 ms    16 000 car. → 3 910 ms
#:
#: Sans borne, une requête authentifiée de 100 Ko occupait un worker web plusieurs
#: minutes. Ce plafond ne corrige pas le défaut de complexité — c'est un lot
#: distinct — il l'empêche d'être atteignable.
#:
#: Pourquoi 4 000
#: --------------
#: Mesuré sur les usages RÉELS, pas choisi par intuition :
#:
#:   production (606 questions) : médiane 60, p99 116, **max 164**
#:   staging   (2 676)          : médiane 75, p99 239, **max 324**
#:   jeux de qualification      : **max 413**
#:
#: 4 000 vaut donc ~10× la plus longue entrée légitime jamais observée — de quoi
#: coller un paragraphe entier — tout en gardant le pire cas adversarial à ~240 ms,
#: soit l'ordre de grandeur d'un retrieval normal plutôt qu'une occupation
#: prolongée. 8 000 laisserait passer près d'une seconde de CPU par requête ;
#: 2 000 aurait été défendable (60 ms) mais ne laisse que 4,8× de marge à l'usage.
#:
#: Le dépassement est REFUSÉ (400), jamais tronqué en silence : une question
#: coupée produirait une réponse à une autre question que celle posée.
MAX_QUESTION_CHARS = 4_000
