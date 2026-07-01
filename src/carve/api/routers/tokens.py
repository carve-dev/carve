"""``/api/v1/tokens`` — API bearer-token mint + revoke (``carve auth rotate``).

``POST`` returns the plaintext token **once**; ``DELETE`` revokes immediately.
``GET`` lists token *metadata* (never the hash or plaintext).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict

from carve.api.dependencies import get_state_store
from carve.api.errors import ResourceNotFound

if TYPE_CHECKING:
    from carve.core.state.store import StateStore

router = APIRouter(prefix="/tokens", tags=["tokens"])


class MintTokenBody(BaseModel):
    scopes: list[str] | None = None


class MintedToken(BaseModel):
    """A freshly minted token — ``token`` is shown exactly once."""

    id: str
    token: str
    scopes: list[str]


class TokenOut(BaseModel):
    """Token metadata (no secret material)."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    scopes: list[str]
    is_default: bool
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


@router.get("", response_model=list[TokenOut])
def list_tokens(
    state_store: StateStore = Depends(get_state_store),
) -> list[TokenOut]:
    """List active tokens (metadata only)."""
    return [TokenOut.model_validate(t) for t in state_store.tokens.list_all()]


@router.post("", response_model=MintedToken, status_code=201)
def mint_token(
    body: MintTokenBody | None = None,
    state_store: StateStore = Depends(get_state_store),
) -> MintedToken:
    """Mint a new token; the plaintext is returned once — save it."""
    scopes = body.scopes if body and body.scopes else ["*"]
    token_id, plaintext = state_store.tokens.create(scopes=scopes)
    return MintedToken(id=token_id, token=plaintext, scopes=scopes)


@router.delete("/{token_id}", status_code=204)
def revoke_token(
    token_id: str,
    state_store: StateStore = Depends(get_state_store),
) -> Response:
    """Revoke a token (immediate effect)."""
    if not state_store.tokens.revoke(token_id):
        raise ResourceNotFound(f"Token {token_id!r} not found.")
    return Response(status_code=204)


__all__ = ["router"]
