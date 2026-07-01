"""The tokens repository — API bearer-token issue + verification.

Mirrors :class:`~carve.core.state.telemetry.TelemetryRepo`: constructed from the
shared ``sessionmaker``, short sync transactions, detached returns.

**Only the argon2 hash is stored.** A minted token's plaintext is returned once
and never persisted. argon2id is *salted*, so ``token_hash`` is not an
equality-lookup key: :meth:`Tokens.find_by_token` verifies a presented plaintext
against the small set of active rows (OSS runs one token). ``Identity`` and the
hashing/generation helpers live here — the state layer owns token *material* —
and :mod:`carve.api.auth` re-exports them so ``api`` depends on ``state`` and
never the reverse.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import argon2
import sqlalchemy as sa

from carve.core.state.models import Token

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker


def _utcnow() -> datetime:
    return datetime.now(UTC)


# OWASP argon2id minimums (memory 19 MiB, 2 iterations, 1 lane): strong, but
# light enough that per-request verification in the auth middleware stays cheap.
_HASHER = argon2.PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)

#: Plaintext token prefix — recognizable, greppable, and namespaced.
_TOKEN_PREFIX = "carve_pat_"
#: Separates the non-secret ``<lookup>`` from the ``<secret>``. Not in the
#: base64url alphabet (``token_urlsafe``), so it never appears inside either part.
_TOKEN_SEP = "."


@dataclass(frozen=True)
class Identity:
    """The authenticated principal a validated bearer token resolves to.

    OSS single-user mode always resolves ``user_id == tenant_id == 1`` with
    ``scopes == ["*"]``; hosted carries tenant/RBAC claims.
    """

    user_id: int
    tenant_id: int
    token_id: str
    scopes: list[str]


def generate_token() -> tuple[str, str]:
    """Return ``(lookup_id, plaintext)`` for a fresh bearer token.

    Plaintext is ``carve_pat_<lookup>.<secret>``: ``<lookup>`` is a non-secret,
    indexed handle (so authentication is one indexed row-fetch + one argon2
    verify), ``<secret>`` is the 256-bit high-entropy credential. The whole
    string is opaque to clients.
    """
    lookup = secrets.token_urlsafe(8)
    secret = secrets.token_urlsafe(32)
    return lookup, f"{_TOKEN_PREFIX}{lookup}{_TOKEN_SEP}{secret}"


def _parse_lookup(plaintext: str) -> str | None:
    """Extract the ``<lookup>`` handle from a plaintext token, or ``None`` if malformed."""
    if not plaintext.startswith(_TOKEN_PREFIX):
        return None
    rest = plaintext[len(_TOKEN_PREFIX) :]
    lookup, sep, secret = rest.partition(_TOKEN_SEP)
    if not sep or not lookup or not secret:
        return None
    return lookup


def hash_token(plaintext: str) -> str:
    """argon2id-hash a plaintext token for storage."""
    return _HASHER.hash(plaintext)


def verify_token(token_hash: str, plaintext: str) -> bool:
    """Return ``True`` iff ``plaintext`` matches the stored argon2 ``token_hash``."""
    try:
        return _HASHER.verify(token_hash, plaintext)
    except argon2.exceptions.VerifyMismatchError:
        return False
    except argon2.exceptions.Argon2Error:
        # A malformed/foreign hash never authenticates.
        return False


class Tokens:
    """Typed access to the ``tokens`` table.

    Construct once per process from the same ``sessionmaker`` as
    :class:`~carve.core.state.repository.Repository`.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create(
        self,
        *,
        scopes: list[str] | None = None,
        tenant_id: int = 1,
        user_id: int = 1,
        is_default: bool = False,
    ) -> tuple[str, str]:
        """Mint a token; return ``(token_id, plaintext)``.

        Only the argon2 hash is stored. The plaintext is the caller's *only*
        chance to capture the secret.
        """
        lookup, plaintext = generate_token()
        token_id = "tok_" + uuid.uuid4().hex
        token = Token(
            id=token_id,
            token_hash=hash_token(plaintext),
            lookup_id=lookup,
            scopes=list(scopes) if scopes is not None else ["*"],
            tenant_id=tenant_id,
            user_id=user_id,
            is_default=is_default,
            created_at=_utcnow(),
        )
        with self._session_factory() as session:
            session.add(token)
            session.commit()
        return token_id, plaintext

    def find_by_token(self, plaintext: str) -> Identity | None:
        """Resolve a presented plaintext to an :class:`Identity`, or ``None``.

        Narrows to the one candidate via the indexed, non-secret ``lookup_id``
        parsed from the plaintext, then runs a **single** argon2 verify — O(1)
        per request regardless of how many tokens exist. A malformed token, an
        unknown/revoked ``lookup_id``, or a verify mismatch all reject.
        """
        lookup = _parse_lookup(plaintext)
        if lookup is None:
            return None
        stmt = sa.select(Token).where(
            Token.lookup_id == lookup,
            Token.revoked_at.is_(None),
        )
        with self._session_factory() as session:
            token = session.scalars(stmt).one_or_none()
        if token is None or not verify_token(token.token_hash, plaintext):
            return None
        return Identity(
            user_id=token.user_id,
            tenant_id=token.tenant_id,
            token_id=token.id,
            scopes=list(token.scopes),
        )

    def touch_last_used(self, token_id: str) -> None:
        """Stamp ``last_used_at = now()`` (best-effort; unknown id is a no-op)."""
        with self._session_factory() as session:
            token = session.get(Token, token_id)
            if token is None:
                return
            token.last_used_at = _utcnow()
            session.commit()

    def revoke(self, token_id: str) -> bool:
        """Revoke a token (immediate effect). Returns ``False`` if unknown."""
        with self._session_factory() as session:
            token = session.get(Token, token_id)
            if token is None:
                return False
            if token.revoked_at is None:
                token.revoked_at = _utcnow()
                session.commit()
            return True

    def list_all(self, *, include_revoked: bool = False) -> list[Token]:
        """List tokens (newest first). Excludes revoked unless asked."""
        stmt = sa.select(Token).order_by(Token.created_at.desc())
        if not include_revoked:
            stmt = stmt.where(Token.revoked_at.is_(None))
        with self._session_factory() as session:
            return list(session.scalars(stmt).all())

    def has_active_default(self) -> bool:
        """Whether a non-revoked default token exists (bootstrap idempotency)."""
        stmt = sa.select(sa.func.count()).select_from(Token).where(
            Token.is_default.is_(True),
            Token.revoked_at.is_(None),
        )
        with self._session_factory() as session:
            return bool(session.execute(stmt).scalar_one())


__all__ = [
    "Identity",
    "Tokens",
    "generate_token",
    "hash_token",
    "verify_token",
]
