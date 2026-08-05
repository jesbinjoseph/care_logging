# History

## 0.1.0 (2026-08-05)

* Initial release: opt-in stdout/stderr split for Care console logging.
* Idempotent, fail-safe apply from `AppConfig.ready()`.
* Avoids duplicate ERROR lines on named loggers via `propagate: False`.
