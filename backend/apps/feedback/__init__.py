"""Application ``feedback`` — signal utilisateur durable et diagnostic d'incident.

Trois responsabilités, volontairement séparées (cf. docs/03-backend/feedback-incidents.md) :

1. **Signal durable** (:class:`apps.feedback.models.AnswerFeedback`) — peu sensible,
   conservé longtemps, il SURVIT à la suppression de l'espace, du workspace, du
   compte et même du ``RagQueryLog`` d'origine.
2. **Snapshot diagnostique** (:class:`~apps.feedback.models.FeedbackDiagnosticSnapshot`)
   — sensible, expirable, purgé indépendamment du signal.
3. **Export d'incident** — jamais automatique, toujours approuvé et audité.
"""
