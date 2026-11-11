from django.apps import AppConfig

class WorkspacesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.workspaces"

    def ready(self) -> None:
        # Enregistre les system checks des flags owner_scope / ACL (workspaces.E001).
        from . import checks  # noqa: F401
