from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Protocol

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("assistant-events")

APP_NAME = os.getenv("ASSISTANT_NAME", "Jarvis").strip() or "Assistant"
API_KEY = os.getenv("EVENTS_API_KEY", "").strip()
DATA_DIR = Path(os.getenv("EVENTS_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "events.db"
DEFAULT_COOLDOWN_SECONDS = max(0, int(os.getenv("EVENTS_DEFAULT_COOLDOWN_SECONDS", "600")))
MAX_EVENT_MESSAGE_CHARS = max(200, int(os.getenv("EVENTS_MAX_MESSAGE_CHARS", "3500")))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
TELEGRAM_TIMEOUT_SECONDS = float(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "20"))


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class EventState(str, Enum):
    INFO = "info"
    ACTIVE = "active"
    RECOVERED = "recovered"


class EventIn(BaseModel):
    source: str = Field(min_length=1, max_length=120)
    event_type: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=8000)
    severity: Severity = Severity.INFO
    state: EventState = EventState.INFO
    target: str | None = Field(default=None, max_length=200)
    actor_id: str = Field(default="system", min_length=1, max_length=120)
    agent_id: str | None = Field(default=None, max_length=120)
    correlation_id: str | None = Field(default=None, max_length=160)
    fingerprint: str | None = Field(default=None, max_length=240)
    cooldown_seconds: int | None = Field(default=None, ge=0, le=604800)
    notify: bool = True
    notify_on_recovery: bool = True
    channels: list[str] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source", "event_type", "title", "message", "actor_id")
    @classmethod
    def strip_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("target", "agent_id", "correlation_id", "fingerprint")
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("channels")
    @classmethod
    def normalize_channels(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = []
        for item in value:
            item = str(item).strip().lower()
            if item and item not in normalized:
                normalized.append(item)
        return normalized


class EventResult(BaseModel):
    event_id: str
    fingerprint: str
    stored: bool
    notification_status: str
    reason: str
    delivered_channels: list[str] = Field(default_factory=list)
    failed_channels: dict[str, str] = Field(default_factory=dict)


class HealthResult(BaseModel):
    ok: bool
    service: str
    assistant_name: str
    schema_version: int
    enabled_channels: list[str]
    database: str


class EventRow(BaseModel):
    id: str
    received_at: str
    source: str
    event_type: str
    severity: str
    state: str
    target: str | None
    actor_id: str
    agent_id: str | None
    fingerprint: str
    title: str
    message: str
    notification_status: str
    notification_reason: str


@dataclass(frozen=True)
class NotificationDecision:
    should_notify: bool
    reason: str


class NotificationSink(Protocol):
    name: str

    async def send(self, event: EventIn, event_id: str, fingerprint: str) -> None:
        ...


class TelegramSink:
    name = "telegram"

    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(TELEGRAM_TIMEOUT_SECONDS))

    async def close(self) -> None:
        await self._client.aclose()

    async def send(self, event: EventIn, event_id: str, fingerprint: str) -> None:
        emoji = {
            Severity.INFO: "ℹ️",
            Severity.WARNING: "⚠️",
            Severity.CRITICAL: "🚨",
        }[event.severity]
        state_label = {
            EventState.INFO: "INFO",
            EventState.ACTIVE: "ACTIVE",
            EventState.RECOVERED: "RECOVERED",
        }[event.state]

        header = f"{emoji} {APP_NAME} · {event.severity.value.upper()} · {state_label}"
        body = event.message.strip()
        if len(body) > MAX_EVENT_MESSAGE_CHARS:
            body = body[: MAX_EVENT_MESSAGE_CHARS - 3].rstrip() + "..."

        lines = [header, "", event.title.strip(), body, "", f"Source: {event.source}"]
        if event.target:
            lines[-1] += f" · Target: {event.target}"
        if event.agent_id:
            lines[-1] += f" · Agent: {event.agent_id}"
        text = "\n".join(lines)

        response = await self._client.post(
            f"{TELEGRAM_API_BASE}/bot{self._token}/sendMessage",
            json={
                "chat_id": self._chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram rejected notification: {payload}")


SINKS: dict[str, NotificationSink] = {}
_telegram_sink: TelegramSink | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def epoch_seconds() -> int:
    return int(utc_now().timestamp())


def make_fingerprint(event: EventIn) -> str:
    if event.fingerprint:
        return event.fingerprint
    identity = "|".join(
        [
            event.source.casefold(),
            event.event_type.casefold(),
            (event.target or "").casefold(),
        ]
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"{event.source}:{event.event_type}:{digest}"


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                received_at TEXT NOT NULL,
                received_epoch INTEGER NOT NULL,
                source TEXT NOT NULL,
                event_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                state TEXT NOT NULL,
                target TEXT,
                actor_id TEXT NOT NULL,
                agent_id TEXT,
                correlation_id TEXT,
                fingerprint TEXT NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                notify INTEGER NOT NULL,
                notification_status TEXT NOT NULL,
                notification_reason TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_events_received_epoch
                ON events(received_epoch DESC);
            CREATE INDEX IF NOT EXISTS idx_events_fingerprint
                ON events(fingerprint, received_epoch DESC);

            CREATE TABLE IF NOT EXISTS notification_state (
                fingerprint TEXT PRIMARY KEY,
                last_state TEXT NOT NULL,
                last_event_id TEXT NOT NULL,
                last_seen_epoch INTEGER NOT NULL,
                last_notified_epoch INTEGER,
                last_notification_status TEXT NOT NULL
            );
            """
        )


def prior_state(fingerprint: str) -> sqlite3.Row | None:
    with db() as connection:
        return connection.execute(
            "SELECT * FROM notification_state WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()


def decide_notification(event: EventIn, state_row: sqlite3.Row | None) -> NotificationDecision:
    if not event.notify:
        return NotificationDecision(False, "event requested storage without notification")

    if event.channels == []:
        return NotificationDecision(False, "event requested no notification channels")

    if event.state == EventState.RECOVERED:
        if not event.notify_on_recovery:
            return NotificationDecision(False, "recovery notification disabled for this event")
        if state_row is None or state_row["last_state"] != EventState.ACTIVE.value:
            return NotificationDecision(False, "recovery has no previously active state")
        return NotificationDecision(True, "active condition recovered")

    cooldown = event.cooldown_seconds
    if cooldown is None:
        cooldown = DEFAULT_COOLDOWN_SECONDS

    if state_row is None:
        return NotificationDecision(True, "first event for fingerprint")

    last_state = state_row["last_state"]
    last_notified_epoch = state_row["last_notified_epoch"]

    if event.state == EventState.ACTIVE and last_state != EventState.ACTIVE.value:
        return NotificationDecision(True, "condition became active")

    if last_notified_epoch is None:
        return NotificationDecision(True, "no successful prior notification")

    age = epoch_seconds() - int(last_notified_epoch)
    if age >= cooldown:
        return NotificationDecision(True, f"cooldown elapsed ({age}s >= {cooldown}s)")

    return NotificationDecision(False, f"suppressed by cooldown ({age}s < {cooldown}s)")


def insert_event(
    event_id: str,
    event: EventIn,
    fingerprint: str,
    notification_status: str,
    reason: str,
) -> None:
    now = iso_now()
    now_epoch = epoch_seconds()
    with db() as connection:
        connection.execute(
            """
            INSERT INTO events (
                id, received_at, received_epoch, source, event_type, severity, state,
                target, actor_id, agent_id, correlation_id, fingerprint, title, message,
                metadata_json, notify, notification_status, notification_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                now,
                now_epoch,
                event.source,
                event.event_type,
                event.severity.value,
                event.state.value,
                event.target,
                event.actor_id,
                event.agent_id,
                event.correlation_id,
                fingerprint,
                event.title,
                event.message,
                json.dumps(event.metadata, separators=(",", ":"), ensure_ascii=False),
                int(event.notify),
                notification_status,
                reason,
            ),
        )


def update_event_notification(event_id: str, notification_status: str, reason: str) -> None:
    with db() as connection:
        connection.execute(
            """
            UPDATE events
            SET notification_status = ?, notification_reason = ?
            WHERE id = ?
            """,
            (notification_status, reason, event_id),
        )


def update_notification_state(
    fingerprint: str,
    event: EventIn,
    event_id: str,
    notification_status: str,
    notified: bool,
) -> None:
    now_epoch = epoch_seconds()
    with db() as connection:
        existing = connection.execute(
            "SELECT last_notified_epoch FROM notification_state WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        last_notified = existing["last_notified_epoch"] if existing else None
        if notified:
            last_notified = now_epoch

        connection.execute(
            """
            INSERT INTO notification_state (
                fingerprint, last_state, last_event_id, last_seen_epoch,
                last_notified_epoch, last_notification_status
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                last_state = excluded.last_state,
                last_event_id = excluded.last_event_id,
                last_seen_epoch = excluded.last_seen_epoch,
                last_notified_epoch = excluded.last_notified_epoch,
                last_notification_status = excluded.last_notification_status
            """,
            (
                fingerprint,
                event.state.value,
                event_id,
                now_epoch,
                last_notified,
                notification_status,
            ),
        )


async def require_api_key(authorization: str | None = Header(default=None)) -> None:
    if not API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="EVENTS_API_KEY is not configured",
        )
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    supplied = authorization[len(prefix):].strip()
    if not hmac.compare_digest(supplied, API_KEY):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid bearer token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _telegram_sink
    init_db()

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        _telegram_sink = TelegramSink(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        SINKS[_telegram_sink.name] = _telegram_sink
        logger.info("Telegram notification sink enabled for chat_id=%s", TELEGRAM_CHAT_ID)
    else:
        logger.warning("Telegram sink disabled: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing")

    if not API_KEY:
        logger.error("EVENTS_API_KEY is empty; authenticated endpoints will refuse requests")

    logger.info(
        "Assistant events starting; assistant=%r db=%s cooldown=%ss sinks=%s",
        APP_NAME,
        DB_PATH,
        DEFAULT_COOLDOWN_SECONDS,
        sorted(SINKS),
    )

    try:
        yield
    finally:
        if _telegram_sink is not None:
            await _telegram_sink.close()
            _telegram_sink = None
        SINKS.clear()


app = FastAPI(title="Assistant Events", version="0.1.0", lifespan=lifespan)


@app.get("/health", response_model=HealthResult)
async def health() -> HealthResult:
    database_status = "healthy"
    try:
        with db() as connection:
            connection.execute("SELECT 1").fetchone()
    except Exception:
        database_status = "error"

    return HealthResult(
        ok=database_status == "healthy",
        service="assistant-events",
        assistant_name=APP_NAME,
        schema_version=1,
        enabled_channels=sorted(SINKS),
        database=database_status,
    )


@app.post("/events", response_model=EventResult, dependencies=[Depends(require_api_key)])
async def create_event(event: EventIn) -> EventResult:
    event_id = str(uuid.uuid4())
    fingerprint = make_fingerprint(event)
    state_row = prior_state(fingerprint)
    decision = decide_notification(event, state_row)

    # Store first. A sink outage must never erase the fact that the event occurred.
    initial_status = "pending" if decision.should_notify else "suppressed"
    insert_event(event_id, event, fingerprint, initial_status, decision.reason)

    if not decision.should_notify:
        update_notification_state(
            fingerprint,
            event,
            event_id,
            notification_status="suppressed",
            notified=False,
        )
        logger.info(
            "Event suppressed id=%s fingerprint=%s reason=%s",
            event_id,
            fingerprint,
            decision.reason,
        )
        return EventResult(
            event_id=event_id,
            fingerprint=fingerprint,
            stored=True,
            notification_status="suppressed",
            reason=decision.reason,
        )

    requested_channels = event.channels if event.channels is not None else sorted(SINKS)
    delivered: list[str] = []
    failed: dict[str, str] = {}

    if not requested_channels:
        failed["none"] = "no notification sinks are enabled"
    else:
        for channel in requested_channels:
            sink = SINKS.get(channel)
            if sink is None:
                failed[channel] = "notification sink is not enabled"
                continue
            try:
                await sink.send(event, event_id, fingerprint)
                delivered.append(channel)
            except Exception as exc:
                logger.exception("Notification sink %s failed for event %s", channel, event_id)
                failed[channel] = str(exc)[:500]

    if delivered and not failed:
        final_status = "delivered"
        reason = decision.reason
    elif delivered:
        final_status = "partial"
        reason = f"{decision.reason}; some channels failed"
    else:
        final_status = "failed"
        reason = f"{decision.reason}; no channel delivered"

    update_event_notification(event_id, final_status, reason)
    update_notification_state(
        fingerprint,
        event,
        event_id,
        notification_status=final_status,
        notified=bool(delivered),
    )

    logger.info(
        "Event processed id=%s fingerprint=%s status=%s delivered=%s failed=%s",
        event_id,
        fingerprint,
        final_status,
        delivered,
        sorted(failed),
    )

    return EventResult(
        event_id=event_id,
        fingerprint=fingerprint,
        stored=True,
        notification_status=final_status,
        reason=reason,
        delivered_channels=delivered,
        failed_channels=failed,
    )


@app.get("/events", response_model=list[EventRow], dependencies=[Depends(require_api_key)])
async def list_events(limit: int = Query(default=50, ge=1, le=500)) -> list[EventRow]:
    with db() as connection:
        rows = connection.execute(
            """
            SELECT id, received_at, source, event_type, severity, state, target,
                   actor_id, agent_id, fingerprint, title, message,
                   notification_status, notification_reason
            FROM events
            ORDER BY received_epoch DESC, rowid DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [EventRow(**dict(row)) for row in rows]
