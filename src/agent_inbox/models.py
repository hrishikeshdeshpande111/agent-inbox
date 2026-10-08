"""Pydantic request/response models for the v1 API."""

from typing import Any, Optional

from pydantic import BaseModel, Field


class CreateInboxRequest(BaseModel):
    label: Optional[str] = Field(default=None, max_length=120)


class CreateInboxResponse(BaseModel):
    id: str
    url: str
    read_secret: str
    write_secret: str
    created_at: str


class DeliverResponse(BaseModel):
    message_id: str
    received_at: str
    # True/False when the sender supplied an HMAC signature, None otherwise.
    signature_valid: Optional[bool] = None


class Message(BaseModel):
    id: str
    received_at: str
    content_type: Optional[str] = None
    # Raw payload as delivered (text). JSON payloads are stored verbatim.
    body: str
    # Filtered request headers (authorization material redacted).
    headers: dict[str, str] = {}
    signature_valid: Optional[bool] = None


class ListMessagesResponse(BaseModel):
    messages: list[Message]
    # Pass as ?before_id= to paginate (keyset, newest-first).
    next_before_id: Optional[str] = None


class InboxInfo(BaseModel):
    id: str
    label: Optional[str] = None
    url: str
    created_at: str
    last_activity_at: Optional[str] = None
    message_count: int


class RotateSecretsResponse(BaseModel):
    id: str
    read_secret: str
    write_secret: str


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str
    retention_days: int


class ErrorResponse(BaseModel):
    detail: str
