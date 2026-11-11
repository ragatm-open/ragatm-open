"""Export d'incident — manuel, approuvé, audité, borné.

CE QUI N'EXISTE PAS ICI, ET N'EXISTERA PAS
------------------------------------------
Il n'y a **aucun** chemin par lequel un clic utilisateur déclenche un export.
Aucune tâche périodique n'exporte. Aucune API n'exporte sans approbation. Le
seul point d'entrée est une action explicite, exécutée par une personne
habilitée, sur un incident déjà qualifié — et elle laisse une trace d'audit.

C'est une contrainte de conception, pas un réglage : le drapeau
``FEEDBACK_INCIDENT_EXPORT_ENABLED`` ouvre l'action manuelle, il ne crée pas de
déclenchement automatique.

TROIS NIVEAUX, TROIS DÉCISIONS DIFFÉRENTES
------------------------------------------
1. **métadonnées** — identifiants, scores, rangs, configuration. Aucun texte.
   C'est suffisant pour diagnostiquer la grande majorité des cas de retrieval.
2. **fragments** — les extraits réellement injectés. Contenu métier : motif
   obligatoire, chiffrement obligatoire.
3. **fichier source** — le document d'origine. Réservé au superuser, pour les
   seuls défauts d'extraction ou de chunking qu'aucun extrait ne reproduit.

On ne monte d'un niveau que si le niveau inférieur ne permet pas de reproduire.
"""
