from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status


@dataclass(frozen=True, slots=True)
class AuthContext:
    principal_id: uuid.UUID
    display_name: str
    session_id: uuid.UUID
    session_key: str
    roles: tuple[str, ...]
    capabilities: tuple[str, ...]
    elevation_expires_at: datetime | None = None

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities


def get_optional_auth(request: Request) -> AuthContext | None:
    return getattr(request.state, "auth", None)


def require_auth(request: Request) -> AuthContext:
    context = get_optional_auth(request)
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "AUTHENTICATION_REQUIRED", "message": "Authentication required"},
        )
    return context


CurrentAuth = Annotated[AuthContext, Depends(require_auth)]


def require_capability(capability: str):
    def dependency(context: CurrentAuth) -> AuthContext:
        if not context.has_capability(capability):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "CAPABILITY_REQUIRED", "message": f"Missing {capability}"},
            )
        return context

    return dependency
