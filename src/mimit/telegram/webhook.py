"""Authenticated, bounded Telegram update ingress. No outbound calls occur here."""

import hmac
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mimit.clock import Clock
from mimit.config import Settings
from mimit.telegram.service import process_message

MAX_BODY_BYTES = 64 * 1024
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
MAX_DATABASE_ID = 2**63 - 1


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("invalid JSON constant")


def _integer(value: object, *, minimum: int | None) -> int:
    if type(value) is not int:
        raise HTTPException(status_code=422, detail="Invalid Telegram identifier")
    if (
        not -MAX_DATABASE_ID <= value <= MAX_DATABASE_ID
        or (minimum is None and value == 0)
        or (minimum is not None and value < minimum)
    ):
        raise HTTPException(status_code=422, detail="Invalid Telegram identifier")
    return value


async def _payload(request: Request) -> dict[str, Any]:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body too large")
        body.extend(chunk)
    try:
        parsed = json.loads(
            body,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise HTTPException(status_code=400, detail="Invalid JSON body") from None
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="Telegram update must be an object")
    return parsed


def create_webhook_router(
    settings: Settings, clock: Clock, sessions: async_sessionmaker[AsyncSession]
) -> APIRouter:
    settings.require_telegram_configuration()
    assert settings.telegram_webhook_secret is not None
    expected = settings.telegram_webhook_secret.get_secret_value().encode("ascii")
    router = APIRouter()

    @router.post("/telegram/webhook")
    async def telegram_webhook(request: Request) -> dict[str, bool]:
        headers = request.headers.getlist(SECRET_HEADER)
        if len(headers) != 1 or not hmac.compare_digest(headers[0].encode("utf-8"), expected):
            raise HTTPException(status_code=403, detail="Forbidden")
        payload = await _payload(request)
        update_id = _integer(payload.get("update_id"), minimum=0)
        if "message" not in payload:
            return {"ok": True}
        message = payload["message"]
        if not isinstance(message, dict):
            raise HTTPException(status_code=422, detail="Invalid Telegram message")
        sender = message.get("from")
        chat = message.get("chat")
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            raise HTTPException(status_code=422, detail="Invalid Telegram message identity")
        user_id = _integer(sender.get("id"), minimum=1)
        chat_id = _integer(chat.get("id"), minimum=None)
        if chat.get("type") != "private" or not settings.allows_telegram_identity(
            user_id=user_id, chat_id=chat_id
        ):
            raise HTTPException(status_code=403, detail="Forbidden")
        if "text" not in message:
            return {"ok": True}
        text = message["text"]
        if not isinstance(text, str) or not text or len(text) > 4096:
            raise HTTPException(status_code=422, detail="Invalid Telegram message text")
        try:
            text.encode("utf-8")
        except UnicodeEncodeError:
            raise HTTPException(status_code=422, detail="Invalid Telegram message text") from None
        if "\x00" in text:
            raise HTTPException(status_code=422, detail="Invalid Telegram message text")
        try:
            await process_message(
                sessions,
                update_id=update_id,
                user_id=user_id,
                chat_id=chat_id,
                text=text,
                clock=clock,
            )
        except Exception:
            # SQLAlchemy exception strings/tracebacks can contain text and credentials.
            logging.getLogger(__name__).error("telegram_processing_failed")
            raise HTTPException(
                status_code=503, detail="Processing temporarily unavailable"
            ) from None
        return {"ok": True}

    return router
