"""Tiny .env loader. No external deps, intentional shape.

Handles the small grammar `carve init` produces: `KEY=value`, double- and
single-quoted values, blank lines, `#` comments, and `\\`-escapes inside
double quotes. Multi-line values and `${VAR}` expansion are out of scope —
the config loader handles `${VAR}` interpolation at the TOML level instead.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_LINE_RE = re.compile(
    r"""
    ^\s*
    (?P<key>[A-Za-z_][A-Za-z0-9_]*)
    \s*=\s*
    (?P<value>
        "(?:\\.|[^"\\])*"      # double-quoted, with backslash-escapes
      | '(?:[^'])*'              # single-quoted, no escapes
      | [^\s#]*                   # bare value, stops at whitespace or comment
    )
    \s*(?:\#.*)?$
    """,
    re.VERBOSE,
)

# Small, explicit escape table. Anything not listed here is treated as a
# literal `\` followed by the next char — never raises, never mojibakes
# non-ASCII characters via `unicode_escape`'s Latin-1 round-trip.
_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "\\": "\\",
    '"': '"',
}


def load_dotenv(path: Path, *, override: bool = False) -> dict[str, str]:
    """Parse `path` as a .env file. Set any keys not already in os.environ.

    Returns the dict of keys actually set this call (for caller logging).
    Missing or unreadable file is not an error — returns ``{}``.

    Existing shell-set values win unless ``override=True``: ``.env`` is a
    default, not an authority. Malformed lines are silently skipped so a
    stray line in the middle of a user's file doesn't stop the rest from
    loading.
    """
    if not path.is_file():
        return {}

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Permission denied, non-UTF-8 bytes, etc. — silent fallback.
        return {}

    set_keys: dict[str, str] = {}
    for raw_line in text.splitlines():
        try:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            m = _LINE_RE.match(line)
            if not m:
                continue  # silently skip malformed lines
            key = m.group("key")
            value = _unquote(m.group("value"))
            if not override and key in os.environ:
                continue
            os.environ[key] = value
            set_keys[key] = value
        except Exception:
            # Defense in depth: any unexpected per-line error is swallowed
            # so one bad line can't break the rest of the file.
            continue
    return set_keys


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        body = value[1:-1]
        if value[0] == '"':
            return _decode_double_quoted(body)
        return body  # single-quoted: literal
    return value


def _decode_double_quoted(body: str) -> str:
    """Decode the small set of supported `\\`-escapes inside double quotes.

    Unknown escapes are passed through as a literal backslash plus the next
    character. A trailing lone backslash is also kept literal. Never raises.
    """
    out: list[str] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == "\\" and i + 1 < n:
            nxt = body[i + 1]
            out.append(_ESCAPES.get(nxt, "\\" + nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)
