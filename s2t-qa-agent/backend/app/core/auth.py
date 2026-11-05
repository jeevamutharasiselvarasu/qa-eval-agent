"""Minimal token-based RBAC with project boundaries.

QA_AUTH_TOKENS="tokenA:alice:approver:retail|finance,tokenB:bob:analyst:retail"
In QA_ENV=dev with no tokens configured, requests without a token act as a dev admin.
This is a deliberately small layer; enterprise SSO (OIDC) is a documented gap.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status

from ..config import get_settings

ROLE_RANK = {"viewer": 0, "analyst": 1, "approver": 2, "admin": 3}


@dataclass
class Principal:
    user: str
    role: str
    projects: set[str]

    def can_access(self, project: str) -> bool:
        return self.role == "admin" or "*" in self.projects or project in self.projects

    def has(self, role: str) -> bool:
        return ROLE_RANK.get(self.role, -1) >= ROLE_RANK[role]


def _parse_tokens(raw: str) -> dict[str, Principal]:
    out: dict[str, Principal] = {}
    for item in raw.split(","):
        parts = item.strip().split(":")
        if len(parts) != 4:
            continue
        token, user, role, projects = parts
        if role not in ROLE_RANK:
            continue
        out[token] = Principal(user, role, set(p for p in projects.split("|") if p))
    return out


def get_principal(authorization: str | None = Header(default=None)) -> Principal:
    s = get_settings()
    tokens = _parse_tokens(s.auth_tokens)
    if not tokens:
        if s.env == "dev" or s.env == "test":
            return Principal("dev.user", "admin", {"*"})
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Authentication not configured")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    p = tokens.get(authorization.split(" ", 1)[1].strip())
    if not p:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    return p


def require(role: str):
    def dep(p: Principal = Depends(get_principal)) -> Principal:
        if not p.has(role):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"Requires role '{role}'")
        return p

    return dep
