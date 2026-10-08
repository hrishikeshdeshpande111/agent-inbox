"""Agent Inbox API server.

Every endpoint returns JSON errors shaped as {"detail": "..."}.
Authentication is via per-inbox secrets (never cookies, never sessions):

  write path:  X-Write-Secret header  OR  ?write_secret= query param
               (query param exists so dumb webhook sources that can't set
               headers — Stripe, GitHub, HTML forms — can still deliver)
  read path:   X-Read-Secret header   OR  ?read_secret= query param

Secrets are shown once at creation/rotation and stored only as hashes.
"""

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from . import __version__, cleanup, models
from .config import settings
from .db import db
from .rate_limit import RateLimiter
from .security import hash_secret, new_secret, secrets_match, verify_hmac_sha256

log = logging.getLogger("agent-inbox")

# Header names whose values are never persisted (case-insensitive match).
_SENSITIVE_HEADER_HINTS = ("secret", "auth", "token", "api-key", "apikey", "cookie", "signature")


def _filter_headers(raw: dict) -> dict[str, str]:
    kept: dict[str, str] = {}
    for k, v in raw.items():
        lk = k.lower()
        if any(hint in lk for hint in _SENSITIVE_HEADER_HINTS):
            kept[k] = "[redacted]"
        elif lk in ("content-length", "host"):
            continue
        else:
            kept[k] = v[:500]
        if len(kept) >= 40:
            break
    return kept


def _client_ip(request: Request) -> str:
    # Trust X-Forwarded-For only for the leftmost (client-originated) entry.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    await db.init()
    log.info("agent-inbox v%s starting (db=%s)", __version__, settings.db_path)
    task = asyncio.create_task(cleanup.loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await db.close()


app = FastAPI(
    title="Agent Inbox",
    description="A mailbox for your AI agent. Give it a URL; anything that can POST a webhook can reach it.",
    version=__version__,
    lifespan=lifespan,
)

_ip_limiter = RateLimiter(settings.rate_limit_per_min)
_inbox_limiter = RateLimiter(settings.rate_limit_per_min)


@app.middleware("http")
async def request_middleware(request: Request, call_next):
    request_id = uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error request_id=%s %s %s", request_id, request.method, request.url.path)
        return JSONResponse({"detail": "internal server error"}, status_code=500)
    elapsed_ms = (time.monotonic() - start) * 1000
    response.headers["X-Request-ID"] = request_id
    log.info(
        "%s %s -> %d (%.1fms) request_id=%s",
        request.method, request.url.path, response.status_code, elapsed_ms, request_id,
    )
    return response


def _enforce_rate_limit(request: Request, inbox_id: str | None = None) -> None:
    ip = _client_ip(request)
    for key in (f"ip:{ip}",) + ((f"inbox:{inbox_id}:{ip}",) if inbox_id else ()):
        limiter = _ip_limiter if key.startswith("ip:") else _inbox_limiter
        if not limiter.allowed(key):
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded",
                headers={"Retry-After": str(limiter.retry_after(key))},
            )


def _write_secret(request: Request) -> str | None:
    return request.headers.get("x-write-secret") or request.query_params.get("write_secret")


def _read_secret(request: Request) -> str | None:
    return request.headers.get("x-read-secret") or request.query_params.get("read_secret")


async def _require_inbox(inbox_id: str) -> dict:
    inbox = await db.get_inbox(inbox_id)
    if not inbox:
        raise HTTPException(status_code=404, detail="inbox not found")
    return inbox


def _inbox_url(inbox_id: str) -> str:
    return f"{settings.base_url}/v1/inboxes/{inbox_id}"


# --------------------------------------------------------------------------
# Public endpoints
# --------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def landing_page():
    return FileResponse("static/index.html")


@app.get("/health", response_model=models.HealthResponse)
async def health():
    return models.HealthResponse(version=__version__, retention_days=settings.retention_days)


@app.post("/v1/inboxes", response_model=models.CreateInboxResponse, status_code=201)
async def create_inbox(body: models.CreateInboxRequest, request: Request):
    _enforce_rate_limit(request)
    read_secret, write_secret = new_secret(), new_secret()
    row = await db.create_inbox(hash_secret(read_secret), hash_secret(write_secret), body.label)
    return models.CreateInboxResponse(
        id=row["id"],
        url=_inbox_url(row["id"]),
        read_secret=read_secret,
        write_secret=write_secret,
        created_at=row["created_at"],
    )


@app.get("/v1/inboxes/{inbox_id}", response_model=models.InboxInfo)
async def inbox_info(inbox_id: str, request: Request):
    _enforce_rate_limit(request, inbox_id)
    inbox = await _require_inbox(inbox_id)
    if not secrets_match(_read_secret(request), inbox["read_secret_hash"]):
        raise HTTPException(status_code=401, detail="invalid read secret")
    stats = await db.inbox_stats(inbox_id)
    return models.InboxInfo(
        id=inbox["id"],
        label=inbox["label"],
        url=_inbox_url(inbox_id),
        created_at=inbox["created_at"],
        last_activity_at=inbox["last_activity_at"],
        message_count=stats["message_count"],
    )


@app.post("/v1/inboxes/{inbox_id}", response_model=models.DeliverResponse, status_code=202)
async def deliver(
    inbox_id: str,
    request: Request,
    x_signature_256: str | None = Header(default=None),
):
    """Deliver a message. Body may be any content type (JSON recommended)."""
    _enforce_rate_limit(request, inbox_id)
    inbox = await _require_inbox(inbox_id)
    provided = _write_secret(request)
    if not secrets_match(provided or "", inbox["write_secret_hash"]):
        raise HTTPException(status_code=401, detail="invalid write secret")

    raw = await request.body()
    if len(raw) > settings.max_body_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"payload too large (max {settings.max_body_bytes} bytes)",
        )

    signature_valid: bool | None = None
    if x_signature_256:
        signature_valid = verify_hmac_sha256(raw, x_signature_256, provided or "")
        if not signature_valid:
            raise HTTPException(status_code=401, detail="invalid webhook signature")

    try:
        body_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="body must be UTF-8 text or JSON")

    row = await db.insert_message(
        inbox_id=inbox_id,
        body=body_text,
        content_type=request.headers.get("content-type"),
        headers=_filter_headers(dict(request.headers)),
        signature_valid=signature_valid,
    )
    await db.touch_inbox(inbox_id)
    return models.DeliverResponse(
        message_id=row["id"], received_at=row["received_at"], signature_valid=signature_valid
    )


@app.get("/v1/inboxes/{inbox_id}/messages", response_model=models.ListMessagesResponse)
async def list_messages(
    inbox_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    before_id: str | None = Query(default=None),
):
    _enforce_rate_limit(request, inbox_id)
    inbox = await _require_inbox(inbox_id)
    if not secrets_match(_read_secret(request), inbox["read_secret_hash"]):
        raise HTTPException(status_code=401, detail="invalid read secret")
    messages, next_before_id = await db.list_messages(inbox_id, limit, before_id)
    return models.ListMessagesResponse(
        messages=[models.Message(**m) for m in messages],
        next_before_id=next_before_id,
    )


@app.delete("/v1/inboxes/{inbox_id}/messages/{message_id}")
async def ack_message(inbox_id: str, message_id: str, request: Request):
    """Acknowledge (delete) a single message after processing it."""
    _enforce_rate_limit(request, inbox_id)
    inbox = await _require_inbox(inbox_id)
    if not secrets_match(_read_secret(request), inbox["read_secret_hash"]):
        raise HTTPException(status_code=401, detail="invalid read secret")
    if not await db.delete_message(inbox_id, message_id):
        raise HTTPException(status_code=404, detail="message not found")
    return {"ok": True}


@app.post("/v1/inboxes/{inbox_id}/rotate", response_model=models.RotateSecretsResponse)
async def rotate_secrets(inbox_id: str, request: Request):
    """Rotate both secrets. Old secrets stop working immediately."""
    _enforce_rate_limit(request, inbox_id)
    inbox = await _require_inbox(inbox_id)
    if not secrets_match(_read_secret(request), inbox["read_secret_hash"]):
        raise HTTPException(status_code=401, detail="invalid read secret")
    read_secret, write_secret = new_secret(), new_secret()
    await db.rotate_secrets(inbox_id, hash_secret(read_secret), hash_secret(write_secret))
    return models.RotateSecretsResponse(id=inbox_id, read_secret=read_secret, write_secret=write_secret)


@app.delete("/v1/inboxes/{inbox_id}")
async def delete_inbox(inbox_id: str, request: Request):
    _enforce_rate_limit(request, inbox_id)
    inbox = await _require_inbox(inbox_id)
    if not secrets_match(_read_secret(request), inbox["read_secret_hash"]):
        raise HTTPException(status_code=401, detail="invalid read secret")
    await db.delete_inbox(inbox_id)
    return {"ok": True}
