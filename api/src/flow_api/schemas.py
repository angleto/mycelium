"""Schemi I/O dell'API (pydantic v2). Niente logica di dominio."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class SignupIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=200)
    org_name: str = Field(min_length=1, max_length=200)


class SignupOut(BaseModel):
    user_id: uuid.UUID
    org_id: uuid.UUID
    token: str


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class TokenOut(BaseModel):
    token: str


class OrgOut(BaseModel):
    id: uuid.UUID
    name: str
    version: int


class OrgPatchIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    expected_version: int = Field(ge=1)


class OrgVersionOut(BaseModel):
    id: uuid.UUID
    version: int
