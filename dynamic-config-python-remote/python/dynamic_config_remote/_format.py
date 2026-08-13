"""Which document format a store is reading, decided where the mistake is.

Five of the seven stores hold a whole configuration document under a key, and
all five take the format from the key's extension — ``myapp/db.json`` is JSON
— or from an explicit argument. The engine deduces it the same way, so this
module changes nothing about *what* is read; what it changes is **when** a key
that names no format is reported.

Without it, `Consul("http://consul:8500", "myapp/db")` builds happily and the
first refresh fails with a message naming `with_format`, which is a Rust
builder method a Python caller cannot call. With it, the constructor raises a
`ValueError` naming the ``format`` argument, at the line the mistake is on.
"""

from __future__ import annotations

from typing import Optional

#: The key extensions the engine reads a format from.
EXTENSIONS = (".json", ".toml", ".yaml", ".yml")

#: What ``format`` may be spelled as. ``yml`` is accepted because a key may
#: end in it, and refusing the spelling that keys use would be a distinction
#: nobody could act on.
NAMES = ("json", "toml", "yaml", "yml")

#: The default deadline for one fetch, in seconds. Ten, matching every Rust
#: store crate — which is the point of it being one number rather than each
#: store's own opinion.
DEFAULT_TIMEOUT = 10.0


def checked(key: str, format: Optional[str]) -> Optional[str]:  # noqa: A002
    """The format for ``key``, or `None` to let the engine read the extension.

    Raises `ValueError` if ``format`` is not a format this engine reads, or if
    it is absent and the key's name does not supply one.
    """
    if format is not None:
        if format.lower() not in NAMES:
            raise ValueError(
                f'{format!r} is not a document format — expected "json", '
                f'"toml" or "yaml"'
            )

        return format

    if not key.lower().endswith(EXTENSIONS):
        raise ValueError(
            f'the key {key!r} names no format — pass format="json", "toml" or "yaml"'
        )

    return None
