from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import tomllib
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Protocol

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("assistant-events")

IDENTITY_PATH = Path(os.getenv("ASSISTANT_IDENTITY_PATH", "/config/identity.toml"))


def load_identity() -> tuple[str, str]:
    fallback_name = os.getenv("ASSISTANT_NAME", "Assistant").strip() or "Assistant"
    fallback_id = os.getenv("ASSISTANT_INTERNAL_ID", "main").strip() or "main"
    try:
        data = tomllib.loads(IDENTITY_PATH.read_text(encoding="utf-8"))
        assistant = data.get("assistant", {})
        display_name = str(assistant.get("display_name", fallback_name)).strip() or fallback_name
        internal_id = str(assistant.get("internal_id", fallback_id)).strip() or fallback_id
        return display_name, internal_id
    except FileNotFoundError:
        logger.warning("Assistant identity file not found at %s; using environment fallbacks", IDENTITY_PATH)
    except Exception:
        logger.exception("Could not read assistant identity file %s; using environment fallbacks", IDENTITY_PATH)
    return fallback_name, fallback_id


def private_chat_fallback(raw_allowed_users: str) -> str:
    ids: list[str] = []
    for item in raw_allowed_users.replace(" ", ",").split(","):
        item = item.strip()
        if item and item.lstrip("-").isdigit() and item not in ids:
            ids.append(item)
    return ids[0] if len(ids) == 1 else ""


APP_NAME, ASSISTANT_INTERNAL_ID = load_identity()
API_KEY = os.getenv("EVENTS_API_KEY", "").strip()
DATA_DIR = Path(os.getenv("EVENTS_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "events.db"
MAX_EVENT_MESSAGE_CHARS = max(200, int(os.getenv("EVENTS_MAX_MESSAGE_CHARS", "3500")))
REMINDER_POLL_SECONDS = max(0.5, float(os.getenv("EVENTS_REMINDER_POLL_SECONDS", "1")))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
if not TELEGRAM_CHAT_ID:
    TELEGRAM_CHAT_ID = private_chat_fallback(os.getenv("TELEGRAM_ALLOWED_USER_IDS", ""))
TELEGRAM_API_BASE = os.getenv("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/")
TELEGRAM_TIMEOUT_SECONDS = float(os.getenv("TELEGRAM_TIMEOUT_SECONDS", "20"))


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


SEVERITY_RANK = {Severity.INFO.value: 0, Severity.WARNING.value: 1, Severity.CRITICAL.value: 2}


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
    notification_key: str | None = Field(default=None, max_length=240)
    # Retained only so older producers do not break. v0.1.3 no longer uses a
    # time cooldown for duplicate suppression.
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

    @field_validator("target", "agent_id", "correlation_id", "fingerprint", "notification_key")
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
        normalized: list[str] = []
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
    reminder_scheduler: str


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


class ReminderIn(BaseModel):
    title: str = Field(default="Reminder", min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=8000)
    delay_seconds: int | None = Field(default=None, ge=1, le=31536000)
    due_at: datetime | None = None
    condition_fingerprint: str | None = Field(default=None, max_length=240)
    only_if_active: bool = False
    actor_id: str = Field(default="user", min_length=1, max_length=120)
    agent_id: str | None = Field(default=None, max_length=120)
    channels: list[str] | None = None

    @field_validator("title", "message", "actor_id")
    @classmethod
    def reminder_required(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("condition_fingerprint", "agent_id")
    @classmethod
    def reminder_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @field_validator("channels")
    @classmethod
    def reminder_channels(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        result: list[str] = []
        for item in value:
            item = str(item).strip().lower()
            if item and item not in result:
                result.append(item)
        return result

    @model_validator(mode="after")
    def reminder_schedule_valid(self) -> "ReminderIn":
        if (self.delay_seconds is None) == (self.due_at is None):
            raise ValueError("provide exactly one of delay_seconds or due_at")
        if self.only_if_active and not self.condition_fingerprint:
            raise ValueError("only_if_active requires condition_fingerprint")
        return self


class ReminderResult(BaseModel):
    reminder_id: str
    status: str
    due_at: str
    condition_fingerprint: str | None
    only_if_active: bool


class ReminderRow(BaseModel):
    id: str
    created_at: str
    due_at: str
    title: str
    message: str
    condition_fingerprint: str | None
    only_if_active: bool
    actor_id: str
    agent_id: str | None
    status: str
    status_reason: str


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
_reminder_task: asyncio.Task | None = None
_reminder_stop: asyncio.Event | None = None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_dt(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def iso_now() -> str:
    return iso_dt(utc_now())


def epoch_seconds() -> int:
    return int(utc_now().timestamp())


def make_fingerprint(event: EventIn) -> str:
    if event.fingerprint:
        return event.fingerprint
    identity = "|".join(
        [event.source.casefold(), event.event_type.casefold(), (event.target or "").casefold()]
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"{event.source}:{event.event_type}:{digest}"


def make_notification_key(event: EventIn) -> str:
    if event.notification_key:
        return event.notification_key
    # Intentionally excludes message text/metrics. Producers can update duration,
    # counters, etc. without re-alerting. If a meaningful sub-state changes, the
    # producer should supply a different notification_key or fingerprint.
    return f"{event.state.value}:{event.severity.value}"


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


def ensure_column(connection: sqlite3.Connection, table: str, name: str, declaration: str) -> None:
    cols = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in cols:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


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

            CREATE TABLE IF NOT EXISTS reminders (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                created_epoch INTEGER NOT NULL,
                due_at TEXT NOT NULL,
                due_epoch INTEGER NOT NULL,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                condition_fingerprint TEXT,
                only_if_active INTEGER NOT NULL,
                actor_id TEXT NOT NULL,
                agent_id TEXT,
                channels_json TEXT NOT NULL,
                status TEXT NOT NULL,
                status_reason TEXT NOT NULL,
                fired_event_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_reminders_due
                ON reminders(status, due_epoch);
            """
        )
        ensure_column(connection, "notification_state", "last_severity", "TEXT")
        ensure_column(connection, "notification_state", "last_notification_key", "TEXT")
        ensure_column(connection, "notification_state", "last_notified_state", "TEXT")
        ensure_column(connection, "notification_state", "last_notified_severity", "TEXT")
        ensure_column(connection, "notification_state", "last_notified_key", "TEXT")


def prior_state(fingerprint: str) -> sqlite3.Row | None:
    with db() as connection:
        return connection.execute(
            "SELECT * FROM notification_state WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()


def decide_notification(event: EventIn, state_row: sqlite3.Row | None) -> NotificationDecision:
    if not event.notify:
        return NotificationDecision(False, "event requested storage without notification")
    if event.channels == []:
        return NotificationDecision(False, "event requested no notification channels")

    key = make_notification_key(event)

    if event.state == EventState.RECOVERED:
        if not event.notify_on_recovery:
            return NotificationDecision(False, "recovery notification disabled for this event")
        if state_row is None or state_row["last_state"] != EventState.ACTIVE.value:
            return NotificationDecision(False, "recovery has no previously active condition")
        if state_row["last_notified_state"] == EventState.RECOVERED.value and state_row["last_notified_key"] == key:
            return NotificationDecision(False, "recovery already notified")
        return NotificationDecision(True, "active condition recovered")

    if state_row is None or not state_row["last_notified_state"]:
        return NotificationDecision(True, "first notification for condition")

    if event.state == EventState.ACTIVE and state_row["last_state"] != EventState.ACTIVE.value:
        return NotificationDecision(True, "condition became active")

    previous_severity = state_row["last_notified_severity"] or Severity.INFO.value
    if SEVERITY_RANK[event.severity.value] > SEVERITY_RANK.get(previous_severity, 0):
        return NotificationDecision(True, "condition severity increased")

    if state_row["last_notified_key"] != key:
        return NotificationDecision(True, "condition meaningfully changed")

    if state_row["last_notified_state"] != event.state.value:
        return NotificationDecision(True, "condition state changed")

    return NotificationDecision(False, "unchanged condition already notified")


def insert_event(event_id: str, event: EventIn, fingerprint: str, notification_status: str, reason: str) -> None:
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
                event_id, now, now_epoch, event.source, event.event_type, event.severity.value,
                event.state.value, event.target, event.actor_id, event.agent_id,
                event.correlation_id, fingerprint, event.title, event.message,
                json.dumps(event.metadata, separators=(",", ":"), ensure_ascii=False),
                int(event.notify), notification_status, reason,
            ),
        )


def update_event_notification(event_id: str, notification_status: str, reason: str) -> None:
    with db() as connection:
        connection.execute(
            "UPDATE events SET notification_status = ?, notification_reason = ? WHERE id = ?",
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
    key = make_notification_key(event)
    with db() as connection:
        existing = connection.execute(
            "SELECT * FROM notification_state WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        notified_epoch = existing["last_notified_epoch"] if existing else None
        notified_state = existing["last_notified_state"] if existing else None
        notified_severity = existing["last_notified_severity"] if existing else None
        notified_key = existing["last_notified_key"] if existing else None
        if notified:
            notified_epoch = now_epoch
            notified_state = event.state.value
            notified_severity = event.severity.value
            notified_key = key

        connection.execute(
            """
            INSERT INTO notification_state (
                fingerprint, last_state, last_event_id, last_seen_epoch,
                last_notified_epoch, last_notification_status, last_severity,
                last_notification_key, last_notified_state, last_notified_severity,
                last_notified_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                last_state = excluded.last_state,
                last_event_id = excluded.last_event_id,
                last_seen_epoch = excluded.last_seen_epoch,
                last_notified_epoch = excluded.last_notified_epoch,
                last_notification_status = excluded.last_notification_status,
                last_severity = excluded.last_severity,
                last_notification_key = excluded.last_notification_key,
                last_notified_state = excluded.last_notified_state,
                last_notified_severity = excluded.last_notified_severity,
                last_notified_key = excluded.last_notified_key
            """,
            (
                fingerprint, event.state.value, event_id, now_epoch, notified_epoch,
                notification_status, event.severity.value, key, notified_state,
                notified_severity, notified_key,
            ),
        )


async def deliver(event: EventIn, event_id: str, fingerprint: str) -> tuple[list[str], dict[str, str]]:
    requested_channels = event.channels if event.channels is not None else sorted(SINKS)
    delivered: list[str] = []
    failed: dict[str, str] = {}
    if not requested_channels:
        failed["none"] = "no notification sinks are enabled"
        return delivered, failed
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
    return delivered, failed


async def process_event(event: EventIn) -> EventResult:
    event_id = str(uuid.uuid4())
    fingerprint = make_fingerprint(event)
    state_row = prior_state(fingerprint)
    decision = decide_notification(event, state_row)

    initial_status = "pending" if decision.should_notify else "suppressed"
    insert_event(event_id, event, fingerprint, initial_status, decision.reason)

    if not decision.should_notify:
        update_notification_state(fingerprint, event, event_id, "suppressed", notified=False)
        return EventResult(
            event_id=event_id, fingerprint=fingerprint, stored=True,
            notification_status="suppressed", reason=decision.reason,
        )

    delivered, failed = await deliver(event, event_id, fingerprint)
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
    update_notification_state(fingerprint, event, event_id, final_status, notified=bool(delivered))
    return EventResult(
        event_id=event_id, fingerprint=fingerprint, stored=True,
        notification_status=final_status, reason=reason,
        delivered_channels=delivered, failed_channels=failed,
    )


def reminder_due(reminder: ReminderIn) -> datetime:
    if reminder.delay_seconds is not None:
        return utc_now() + timedelta(seconds=reminder.delay_seconds)
    assert reminder.due_at is not None
    if reminder.due_at.tzinfo is None:
        return reminder.due_at.replace(tzinfo=timezone.utc)
    return reminder.due_at.astimezone(timezone.utc)


def insert_reminder(reminder: ReminderIn) -> ReminderResult:
    reminder_id = str(uuid.uuid4())
    due = reminder_due(reminder)
    if due <= utc_now():
        raise HTTPException(status_code=422, detail="reminder due_at must be in the future")
    with db() as connection:
        connection.execute(
            """
            INSERT INTO reminders (
                id, created_at, created_epoch, due_at, due_epoch, title, message,
                condition_fingerprint, only_if_active, actor_id, agent_id,
                channels_json, status, status_reason, fired_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'scheduled', 'waiting', NULL)
            """,
            (
                reminder_id, iso_now(), epoch_seconds(), iso_dt(due), int(due.timestamp()),
                reminder.title, reminder.message, reminder.condition_fingerprint,
                int(reminder.only_if_active), reminder.actor_id, reminder.agent_id,
                json.dumps(reminder.channels),
            ),
        )
    return ReminderResult(
        reminder_id=reminder_id,
        status="scheduled",
        due_at=iso_dt(due),
        condition_fingerprint=reminder.condition_fingerprint,
        only_if_active=reminder.only_if_active,
    )


def condition_is_active(fingerprint: str) -> bool:
    row = prior_state(fingerprint)
    return bool(row and row["last_state"] == EventState.ACTIVE.value)


def claim_due_reminders() -> list[sqlite3.Row]:
    now = epoch_seconds()
    claimed: list[sqlite3.Row] = []
    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM reminders WHERE status = 'scheduled' AND due_epoch <= ? ORDER BY due_epoch, rowid LIMIT 50",
            (now,),
        ).fetchall()
        for row in rows:
            changed = connection.execute(
                "UPDATE reminders SET status = 'processing', status_reason = 'due' WHERE id = ? AND status = 'scheduled'",
                (row["id"],),
            ).rowcount
            if changed:
                claimed.append(row)
    return claimed


def finish_reminder(reminder_id: str, status_value: str, reason: str, event_id: str | None = None) -> None:
    with db() as connection:
        connection.execute(
            "UPDATE reminders SET status = ?, status_reason = ?, fired_event_id = ? WHERE id = ?",
            (status_value, reason, event_id, reminder_id),
        )


async def process_due_reminder(row: sqlite3.Row) -> None:
    fingerprint = row["condition_fingerprint"]
    if row["only_if_active"] and (not fingerprint or not condition_is_active(fingerprint)):
        finish_reminder(row["id"], "skipped", "condition is no longer active")
        logger.info("Conditional reminder %s skipped because condition is not active", row["id"])
        return

    channels = json.loads(row["channels_json"]) if row["channels_json"] else None
    event = EventIn(
        source="reminder",
        event_type="scheduled_reminder",
        severity=Severity.INFO,
        state=EventState.INFO,
        title=row["title"],
        message=row["message"],
        target=fingerprint or "user",
        actor_id=row["actor_id"],
        agent_id=row["agent_id"],
        fingerprint=f"reminder:{row['id']}",
        notification_key="fire",
        channels=channels,
    )
    result = await process_event(event)
    if result.delivered_channels:
        finish_reminder(row["id"], "delivered", result.reason, result.event_id)
    else:
        finish_reminder(row["id"], "failed", result.reason, result.event_id)


async def reminder_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            for row in claim_due_reminders():
                await process_due_reminder(row)
        except Exception:
            logger.exception("Reminder scheduler iteration failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=REMINDER_POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def require_api_key(authorization: str | None = Header(default=None)) -> None:
    if not API_KEY:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="EVENTS_API_KEY is not configured")
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    supplied = authorization[len(prefix):].strip()
    if not hmac.compare_digest(supplied, API_KEY):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid bearer token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _telegram_sink, _reminder_task, _reminder_stop
    init_db()

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        _telegram_sink = TelegramSink(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
        SINKS[_telegram_sink.name] = _telegram_sink
        logger.info("Telegram notification sink enabled for chat_id=%s", TELEGRAM_CHAT_ID)
    else:
        logger.warning("Telegram sink disabled: bot token/chat ID unavailable")

    if not API_KEY:
        logger.error("EVENTS_API_KEY is empty; authenticated endpoints will refuse requests")

    _reminder_stop = asyncio.Event()
    _reminder_task = asyncio.create_task(reminder_loop(_reminder_stop), name="assistant-reminder-scheduler")
    logger.info("Assistant events starting; assistant=%r db=%s sinks=%s", APP_NAME, DB_PATH, sorted(SINKS))

    try:
        yield
    finally:
        if _reminder_stop is not None:
            _reminder_stop.set()
        if _reminder_task is not None:
            await _reminder_task
            _reminder_task = None
        _reminder_stop = None
        if _telegram_sink is not None:
            await _telegram_sink.close()
            _telegram_sink = None
        SINKS.clear()


app = FastAPI(title="Assistant Events", version="0.1.3", lifespan=lifespan)


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
        schema_version=2,
        enabled_channels=sorted(SINKS),
        database=database_status,
        reminder_scheduler="running" if _reminder_task is not None and not _reminder_task.done() else "stopped",
    )


@app.post("/events", response_model=EventResult, dependencies=[Depends(require_api_key)])
async def create_event(event: EventIn) -> EventResult:
    return await process_event(event)


@app.get("/events", response_model=list[EventRow], dependencies=[Depends(require_api_key)])
async def list_events(limit: int = Query(default=50, ge=1, le=500)) -> list[EventRow]:
    with db() as connection:
        rows = connection.execute(
            """
            SELECT id, received_at, source, event_type, severity, state, target,
                   actor_id, agent_id, fingerprint, title, message,
                   notification_status, notification_reason
            FROM events ORDER BY received_epoch DESC, rowid DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [EventRow(**dict(row)) for row in rows]


@app.post("/reminders", response_model=ReminderResult, dependencies=[Depends(require_api_key)])
async def create_reminder(reminder: ReminderIn) -> ReminderResult:
    return insert_reminder(reminder)


@app.get("/reminders", response_model=list[ReminderRow], dependencies=[Depends(require_api_key)])
async def list_reminders(limit: int = Query(default=50, ge=1, le=500)) -> list[ReminderRow]:
    with db() as connection:
        rows = connection.execute(
            """
            SELECT id, created_at, due_at, title, message, condition_fingerprint,
                   only_if_active, actor_id, agent_id, status, status_reason
            FROM reminders ORDER BY created_epoch DESC, rowid DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [ReminderRow(**{**dict(row), "only_if_active": bool(row["only_if_active"])}) for row in rows]


@app.post("/reminders/{reminder_id}/cancel", dependencies=[Depends(require_api_key)])
async def cancel_reminder(reminder_id: str) -> dict[str, str]:
    with db() as connection:
        changed = connection.execute(
            "UPDATE reminders SET status = 'cancelled', status_reason = 'cancelled by request' WHERE id = ? AND status = 'scheduled'",
            (reminder_id,),
        ).rowcount
    if not changed:
        raise HTTPException(status_code=409, detail="reminder is not scheduled or does not exist")
    return {"reminder_id": reminder_id, "status": "cancelled"}
