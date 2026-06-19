"""The secret-path deny-list — shared by ``read_file``/``glob``/``grep``.

A read-only explorer must not be able to leak a credential file into an
answer, so secret-bearing paths are denied on *every* read tool in
*every* mode (including ``read_only``). The patterns cover the dlt/dbt
credential surface and the common dotenv/PEM shapes:

* ``.env`` and ``.env.*`` (dotenv files anywhere)
* ``**/secrets.toml`` (dlt's secrets file, project or home)
* ``*.pem`` (private keys)
* ``~/.dlt/secrets.toml`` (dlt's home-dir secrets)

Matching is by the path's *name* and its position, on the resolved path,
so a symlink or ``..`` that lands on ``.env`` is still caught. This is a
deny check layered *on top of* the existing project-root containment —
it does not replace it.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

# Filename glob patterns that deny regardless of directory. Patterns are
# lowercase; the match casefolds the candidate name first (see below), so
# `.ENV` / `.Env` / `SECRETS.TOML` are all caught.
_DENIED_NAME_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "secrets.toml",
    "*.pem",
)


def _normalize_name(name: str) -> str:
    """Casefold and strip trailing dots/spaces from a filename.

    Both quirks are real bypass vectors: case-insensitive filesystems
    (macOS default, Windows) open ``.ENV`` as ``.env``, and an FS that
    normalizes trailing dots/spaces (Windows) opens ``.env `` / ``.env.``
    as ``.env``. We normalize the *compared* name the same way before
    matching so the deny-list sees the file the OS would actually open.
    """
    return name.rstrip(". ").casefold()


def is_secret_path(path: Path | str) -> bool:
    """Return True iff ``path`` matches a secret-bearing pattern.

    Matches the path's basename, **casefolded and trailing-trimmed**
    (``_normalize_name``), against the (lowercase) deny globs — so case
    and trailing-dot/space variants of a secret name are all denied.
    Caller is responsible for resolving the path first when a symlink/
    ``..`` escape matters (the read tools do).
    """
    name = _normalize_name(Path(path).name)
    return any(fnmatch.fnmatch(name, pat) for pat in _DENIED_NAME_GLOBS)


def secret_path_reason(path: Path | str) -> str:
    """A short, actionable denial message for a secret path."""
    return (
        f"Reading {Path(path).name!r} is not allowed: it may contain "
        "credentials (dotenv / secrets.toml / private key). Secret files "
        "are denied to every tool in every mode."
    )


__all__ = ["is_secret_path", "secret_path_reason"]
