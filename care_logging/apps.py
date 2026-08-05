from django.apps import AppConfig
from django.utils.translation import gettext_lazy as _

PLUGIN_NAME = "care_logging"


class CareLoggingConfig(AppConfig):
    name = PLUGIN_NAME
    verbose_name = _("Care Logging")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        # Import inside ready() so installing the package does not configure
        # logging until Django has loaded settings.
        from care_logging.logging_config import apply_split_console_logging

        apply_split_console_logging()
