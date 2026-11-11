from django.apps import AppConfig


class FeedbackConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.feedback"
    label = "feedback"
    verbose_name = "Feedback utilisateur et incidents diagnostiques"

    def ready(self):
        from django.core.checks import register

        from apps.feedback.checks import check_feedback_provenance

        register(check_feedback_provenance)
