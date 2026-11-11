from django.apps import AppConfig


class CorpusConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.corpus"
    label = "corpus"
    verbose_name = "Corpus documentaires métier"
