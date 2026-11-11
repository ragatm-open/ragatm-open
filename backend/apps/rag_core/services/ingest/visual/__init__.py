"""Socle partagé de l'analyse visuelle via l'AI Gateway.

* ``contracts``     — ``VisualAsset``, requête V1, identifiants, ``VisualArtifact``,
                      remappage de bbox, utilisabilité, dédoublonnage exact ;
* ``table_adapter`` — ``TablePayload`` Gateway -> rail tabulaire générique EXISTANT ;
* ``normalizers``   — sérialisation déterministe photo / diagramme / graphique /
                      dessin technique / régions mixtes.

Rien ici n'est branché à l'indexation : ni découverte de visuels dans une source,
ni unités indexées, ni règlement d'état de document.
"""
