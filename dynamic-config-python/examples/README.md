# Examples

Every one of these is a script you can run. None needs a server, a
network or a setup step — they write their own configuration into a
temporary directory and clean up after themselves.

```sh
python examples/01_quick_start.py
```

| Example | Shows |
|---|---|
| [`01_quick_start`](01_quick_start.py) | A model, a file, `init()`, `current()` — and where each value came from |
| [`02_layering`](02_layering.py) | Files, the environment, `.env` and profiles, in precedence order, with `explain` proving it |
| [`03_watching`](03_watching.py) | A watcher, a reload hook, and a rejected edit changing nothing |
| [`04_asyncio_service`](04_asyncio_service.py) | `init_async`/`reload_async`, `async for config.changes()`, and reading once per request |
| [`05_decorator`](05_decorator.py) | `@dynamic_config` on a model class, and why it does not load at import time |
| [`06_multi_tenant`](06_multi_tenant.py) | One model, one configuration per tenant, each with its own snapshot |
| [`07_secrets_and_recovery`](07_secrets_and_recovery.py) | `SecretStr` as the only declaration, a redacted cache, and starting from it when the source breaks |
| [`08_diagnostics`](08_diagnostics.py) | `source_of`, `is_set`, `explain`, `check`, `snapshot`, `changed_paths` |
| [`09_testing_overrides`](09_testing_overrides.py) | Pinning configuration in a test without touching the filesystem: `with config.overrides(...)`, a nested block, and one that raises |
| [`10_fastapi_service`](10_fastapi_service.py) | FastAPI: configuration as a dependency in both `async def` and `def` endpoints, a watcher owned by the app's lifespan and started with `watch_async`, and a test override (`pip install fastapi httpx`) |
| [`11_flask_service`](11_flask_service.py) | Flask: read per request rather than copied into `app.config` (`pip install flask`) |
| [`12_django_settings`](12_django_settings.py) | Django: static settings for Django, a reloadable half for the values operators turn (`pip install django`) |
| [`13_asyncio_many_files`](13_asyncio_many_files.py) | Three configurations, three files, one loop: `asyncio.gather` to load, a follower each, and an executor of the service's own |
| [`14_async_decorator_services`](14_async_decorator_services.py) | The same, on the model classes: three decorated services, `Model.config.init_async()`, a watcher and a follower each |
| [`15_pydantic_settings`](15_pydantic_settings.py) | An existing `BaseSettings` class, its declaration translated into engine sources by `from_settings` (`pip install pydantic-settings`) |
| [`16_callbacks`](16_callbacks.py) | Every callback shape: `on_reload`, the decorator, `on_change` filters, a scoped guard, handing work to the thread that owns it, and an async follower |
| [`17_dataclasses`](17_dataclasses.py) | A plain `dataclasses.dataclass` as the schema, with no Pydantic installed — structural validation, secrets in `field(metadata=...)`, and the same diagnostics |
| [`18_python_remote_source`](18_python_remote_source.py) | A remote store written in Python: `RemoteSource`, an explicit `refresh_remote()`, the GIL measured across a 200 ms fetch, and a store that starts refusing its credential |
| [`19_document_shape`](19_document_shape.py) | A file with no section header (`whole_document()`), a key the model does not declare — ignored, forbidden, or refused by a dataclass — two files holding half a model each, and a field nothing supplies |
| [`20_schemaless`](20_schemaless.py) | `Values`: a configuration with no model class, read by dotted path — the same layers and diagnostics, `check()` saying it compared no field names, and `secrets=` buying the redacting cache |
| [`21_decorator_whole_document`](21_decorator_whole_document.py) | `@dynamic_config` argument by argument, and `whole_document=True` against a file with no header — including `key=""` for a configuration with nothing to call itself |
| [`22_msgspec`](22_msgspec.py) | A `msgspec.Struct` as the schema: secrets declared in `Meta(extra=...)`, unknown keys left to the struct, an empty `errors` on a refusal, and the value msgspec quotes taken back out (`pip install dynamic-config-py[msgspec]`) |

Examples 10 to 12 need their framework installed, 15 needs
pydantic-settings and 22 needs msgspec; 17 needs nothing at all, which is
its point — it is the base install. The rest need Pydantic (`pip install
dynamic-config-py[pydantic]`). All twenty-two run in CI, because an
example nobody runs is documentation that has already started rotting —
and the three framework ones are driven again by
`tests/test_integration.py`, which asserts what they answer rather than
only that they exit zero.
