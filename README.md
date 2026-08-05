# care_logging

Care backend plug that splits console logging by severity:

- **DEBUG / INFO / WARNING** → `stdout`
- **ERROR+** → `stderr`

## How it works

Care loads this package as a Django app via `plug_config.py` or `ADDITIONAL_PLUGS`. On startup, `AppConfig.ready()` reconfigures process logging from Care's existing `LOGGING` settings:

1. `console` → `stdout`, filtered to below ERROR
2. `console_error` → `stderr` for ERROR+
3. Root logger uses both handlers
4. ERROR-only named loggers are pointed at `console_error` with `propagate: False` (no duplicate stderr lines)

If the plug is not installed, Care's default logging is unchanged. Configuration failures are logged and never crash Care startup.

## Enable

### `ADDITIONAL_PLUGS`

```json
[
  {
    "name": "care_logging",
    "version": "@main",
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
    version="@main",
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
make test-unit   # local unit tests
make test-care   # fetch latest ohcnetwork/care@develop LOGGING and verify the plug
make test        # both
```

`make test-care` is the practical success check against Care: it loads Care's real
`LOGGING` dicts from `base.py` / `deployment.py` / `test.py`, applies the plug, and
asserts INFO→stdout / ERROR→stderr. A full Care install is unnecessary for this
plug and mostly tests Care's own dependencies rather than logging behavior.
