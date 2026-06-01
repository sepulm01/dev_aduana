from django.apps import AppConfig


class OperadoresConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "operadores"

    def ready(self):
        import operadores.signals  # noqa: F401
