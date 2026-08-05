# care_logging

Care backend plug that splits console logging by severity:

- **DEBUG / INFO / WARNING** → `stdout`
- **ERROR / CRITICAL** → `stderr`

Without this plug installed, Care logging is unchanged. Install it only when you
want the stream split.

## Behavior

### Django / Gunicorn

Care loads this package as a Django app via `plug_config.py` or `ADDITIONAL_PLUGS`.
On startup, `AppConfig.ready()` transforms Care's existing `LOGGING` settings and
applies them with `dictConfig`:

1. `console` → `stdout`, filtered to below ERROR (`care_logging_below_error`)
2. `care_logging_stderr` → `stderr` for ERROR+
3. Root logger uses both handlers
4. ERROR-level named loggers that use `console` are pointed at `care_logging_stderr`;
   `propagate` is set to `False` only when needed to avoid duplicate stderr lines
5. `time_logging_middleware` keeps INFO on stdout and still emits ERROR+ on stderr
   exactly once (even though that logger uses `propagate=False`)

Existing non-console handlers, formatters, filters, logger levels, and Care's
`disable_existing_loggers` value are preserved. Plug filter/handler names are
namespaced so they do not overwrite Care keys such as `below_error` or
`console_error`.

### Celery worker and beat

Celery 5.x defaults to `worker_hijack_root_logger=True`, which clears Django's
root handlers after `AppConfig.ready()` and installs its own stderr handler.
This plug connects to Celery's `after_setup_logger` and `after_setup_task_logger`
signals so the stdout/stderr split is re-applied after that hijack.

- CLI log levels (`-l INFO`, etc.) are preserved
- Task loggers keep `propagate=False` and get their own split handlers (no duplicates)
- File logging (`--logfile`) is left alone
- If Celery is not installed, the Django path still works; Celery hooks soft-import

### Fail-safe apply

- If copying or transforming `settings.LOGGING` fails, the plug aborts and leaves
  the current logging configuration untouched
- If applying the transformed config fails, the plug makes a **best-effort** restore
  of the original Care `LOGGING` dict. Logs state clearly whether restore succeeded;
  it does not claim the old configuration was preserved unless restore worked
- Apply failures are logged and never crash Django or Celery startup

## Enable

Prefer a **tagged release** in production rather than `@main`.

### `ADDITIONAL_PLUGS`

```json
[
  {
    "name": "care_logging",
    "version": "@v0.1.0",
    "package_name": "git+https://github.com/jesbinjoseph/care_logging.git",
    "configs": {}
  }
]
```

### `plug_config.py`

```python
from plugs.manager import PlugManager
from plugs.plug import Plug

care_logging = Plug(
    name="care_logging",
    package_name="git+https://github.com/jesbinjoseph/care_logging.git",
    version="@v0.1.0",
    configs={},
)

plugs = [care_logging]
manager = PlugManager(plugs)
```

## Local development

```bash
pip install -e ".[test]"
make check
```

Register a local `Plug` the same way as other Care plugs (editable install path).

## Tests

```bash
make test-unit   # local unit tests (includes exact Care LOGGING fixtures)
make test-care   # fetch latest ohcnetwork/care@develop LOGGING and verify the plug
make test        # both
```
