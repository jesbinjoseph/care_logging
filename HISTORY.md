# History

## 0.1.0 (2026-08-05)

* Initial release: opt-in stdout/stderr split for Care console logging.
* Preserves Care `disable_existing_loggers` and existing non-console handlers/filters.
* Namespaced plug filter/handler keys (`care_logging_*`) to avoid collisions.
* Keeps `time_logging_middleware` ERROR+ records on stderr when that logger does not propagate.
* Celery worker/beat support via `after_setup_logger` / `after_setup_task_logger`.
* Idempotent apply based on live logger state (Celery hijack can re-apply safely).
* Fail-safe apply with best-effort restore of the original Care `LOGGING` config.
