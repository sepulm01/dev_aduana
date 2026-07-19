from config.settings import *  # noqa: F401,F403

# django.contrib.postgres depende de backends específicos de Postgres
# (p.ej. ArrayField/lookups) que no aplican en sqlite; lo removemos solo
# para el entorno de tests.
INSTALLED_APPS = [app for app in INSTALLED_APPS if app != "django.contrib.postgres"]  # noqa: F405

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    }
}

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True

# Crear tablas directamente desde los modelos, evitando migraciones
# específicas de Postgres.
MIGRATION_MODULES = {
    "aduana": None,
    "devices": None,
    "live": None,
    "operadores": None,
    "monitoring": None,
}
