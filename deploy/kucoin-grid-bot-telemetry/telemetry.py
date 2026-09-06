#!/usr/bin/env python3
"""Read-only HTTP telemetry sidecar for the KuCoin grid bot.

The service never talks to KuCoin and never mutates the bot's state/config.
It reads the bot's persisted state, derives anchor-valued accounting metrics,
and stores its own small history database for trend queries.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

DEFAULT_CONFIG = Path("/etc/kucoin-grid-bot/config.yaml")
DEFAULT_STATE = Path("/var/lib/kucoin-grid-bot/state.json")
DEFAULT_TOKEN = Path("/etc/kucoin-grid-bot/telemetry.token")
DEFAULT_HISTORY = Path("/var/lib/kucoin-grid-bot-telemetry/history.sqlite3")
DEFAULT_FRESH_SECONDS = 300
DEFAULT_SAMPLE_SECONDS = 60
DEFAULT_CHECKPOINT_SECONDS = 300
DEFAULT_RETENTION_DAYS = 180
BOT_SERVICE = "kucoin-grid-bot.service"

WINDOW_SECONDS = {
    "day": 24 * 60 * 60,
    "week": 7 * 24 * 60 * 60,
    "month": 30 * 24 * 60 * 60,
}


class TelemetryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Snapshot:
    ts: int
    state_mtime: float
    state_age_seconds: float
    fresh: bool
    symbol: str
    initialized: bool
    cycle: int
    anchor_price: Decimal
    managed_base: Decimal
    managed_quote: Decimal
    target_coin_value: Decimal
    portfolio_high: Decimal
    initial_capital: Decimal
    anchor_value: Decimal
    anchor_pnl: Decimal
    anchor_return_pct: Decimal
    peak_pnl: Decimal
    peak_return_pct: Decimal
    drawdown_from_peak: Decimal
    drawdown_pct: Decimal
    maker_fill_count: int
    taker_fill_count: int
    gross_fees_base: Decimal
    gross_fees_quote: Decimal
    kcs_fee_ledger_paid: Decimal
    kcs_fee_ledger_refunded: Decimal
    fee_ledger_base_adjustment: Decimal
    fee_ledger_quote_adjustment: Decimal
    reported_maker_fee_rate: Decimal
    reported_taker_fee_rate: Decimal


def _decimal(value: Any, field: str) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError) as exc:
        raise TelemetryError(f"Invalid decimal in state field {field!r}") from exc


def _config_scalar(config_path: Path, key: str) -> str | None:
    """Read one YAML-ish scalar without needing PyYAML or exposing secrets."""
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:\s*(.*?)\s*(?:#.*)?$")
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise TelemetryError(f"Could not read bot config: {exc}") from exc
    for line in lines:
        match = pattern.match(line)
        if not match:
            continue
        raw = match.group(1).strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
            raw = raw[1:-1]
        return raw
    return None


def _state_path(config_path: Path, fallback: Path) -> Path:
    configured = _config_scalar(config_path, "state_file")
    if not configured:
        return fallback
    path = Path(configured)
    if path.is_absolute():
        return path
    return config_path.parent / path


def _read_snapshot(
    config_path: Path,
    fallback_state_path: Path,
    fresh_seconds: int,
) -> Snapshot:
    state_path = _state_path(config_path, fallback_state_path)
    try:
        stat = state_path.stat()
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TelemetryError(f"Could not read bot state: {exc}") from exc
    if not isinstance(state, dict):
        raise TelemetryError("Bot state is not a JSON object")

    initial_raw = _config_scalar(config_path, "initial_capital")
    if initial_raw is None:
        raise TelemetryError("initial_capital is missing from bot config")
    initial_capital = _decimal(initial_raw, "initial_capital")
    if initial_capital <= 0:
        raise TelemetryError("initial_capital must be greater than zero")

    anchor_price = _decimal(state.get("anchor_price"), "anchor_price")
    managed_base = _decimal(state.get("managed_base"), "managed_base")
    managed_quote = _decimal(state.get("managed_quote"), "managed_quote")
    portfolio_high = _decimal(state.get("portfolio_high"), "portfolio_high")
    anchor_value = managed_quote + managed_base * anchor_price
    anchor_pnl = anchor_value - initial_capital
    anchor_return_pct = (anchor_pnl / initial_capital) * Decimal("100")
    peak_pnl = portfolio_high - initial_capital
    peak_return_pct = (peak_pnl / initial_capital) * Decimal("100")
    drawdown = anchor_value - portfolio_high
    drawdown_pct = (
        (drawdown / portfolio_high) * Decimal("100")
        if portfolio_high > 0
        else Decimal("0")
    )

    now = time.time()
    age = max(0.0, now - stat.st_mtime)
    return Snapshot(
        ts=int(now),
        state_mtime=stat.st_mtime,
        state_age_seconds=age,
        fresh=age <= fresh_seconds,
        symbol=str(state.get("symbol") or ""),
        initialized=bool(state.get("initialized")),
        cycle=int(state.get("cycle") or 0),
        anchor_price=anchor_price,
        managed_base=managed_base,
        managed_quote=managed_quote,
        target_coin_value=_decimal(state.get("target_coin_value"), "target_coin_value"),
        portfolio_high=portfolio_high,
        initial_capital=initial_capital,
        anchor_value=anchor_value,
        anchor_pnl=anchor_pnl,
        anchor_return_pct=anchor_return_pct,
        peak_pnl=peak_pnl,
        peak_return_pct=peak_return_pct,
        drawdown_from_peak=drawdown,
        drawdown_pct=drawdown_pct,
        maker_fill_count=int(state.get("maker_fill_count") or 0),
        taker_fill_count=int(state.get("taker_fill_count") or 0),
        gross_fees_base=_decimal(state.get("gross_fees_base"), "gross_fees_base"),
        gross_fees_quote=_decimal(state.get("gross_fees_quote"), "gross_fees_quote"),
        kcs_fee_ledger_paid=_decimal(state.get("kcs_fee_ledger_paid"), "kcs_fee_ledger_paid"),
        kcs_fee_ledger_refunded=_decimal(
            state.get("kcs_fee_ledger_refunded"), "kcs_fee_ledger_refunded"
        ),
        fee_ledger_base_adjustment=_decimal(
            state.get("fee_ledger_base_adjustment"), "fee_ledger_base_adjustment"
        ),
        fee_ledger_quote_adjustment=_decimal(
            state.get("fee_ledger_quote_adjustment"), "fee_ledger_quote_adjustment"
        ),
        reported_maker_fee_rate=_decimal(
            state.get("reported_maker_fee_rate"), "reported_maker_fee_rate"
        ),
        reported_taker_fee_rate=_decimal(
            state.get("reported_taker_fee_rate"), "reported_taker_fee_rate"
        ),
    )


def _d(value: Decimal, places: int = 12) -> str:
    text = format(value, f".{places}f")
    return text.rstrip("0").rstrip(".") or "0"


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def _bot_service_status() -> dict[str, Any]:
    """Read fixed systemd service state; never accepts model-controlled commands."""
    command = [
        "systemctl",
        "show",
        BOT_SERVICE,
        "--no-pager",
        "--property=ActiveState",
        "--property=SubState",
        "--property=MainPID",
        "--property=ActiveEnterTimestamp",
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "active": None, "error": str(exc)}
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "systemctl failed").strip()[:300]
        return {"available": False, "active": None, "error": message}

    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    active_state = values.get("ActiveState", "unknown")
    pid_raw = values.get("MainPID", "0")
    try:
        main_pid = int(pid_raw or 0)
    except ValueError:
        main_pid = 0
    return {
        "available": True,
        "active": active_state == "active",
        "active_state": active_state,
        "sub_state": values.get("SubState", "unknown"),
        "main_pid": main_pid,
        "active_since": values.get("ActiveEnterTimestamp") or None,
    }


def _status_payload(
    snapshot: Snapshot,
    service: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "symbol": snapshot.symbol,
        "initialized": snapshot.initialized,
        "cycle": snapshot.cycle,
        "anchor_price": _d(snapshot.anchor_price),
        "managed_base": _d(snapshot.managed_base),
        "managed_quote": _d(snapshot.managed_quote),
        "target_coin_value": _d(snapshot.target_coin_value),
        "portfolio_high": _d(snapshot.portfolio_high),
        "maker_fill_count": snapshot.maker_fill_count,
        "taker_fill_count": snapshot.taker_fill_count,
        "state_modified_at": _iso(snapshot.state_mtime),
        "state_age_seconds": round(snapshot.state_age_seconds, 1),
        "state_fresh": snapshot.fresh,
        "bot_service": service if service is not None else _bot_service_status(),
        "valuation_basis": "persisted_anchor_price",
        "live_market_price_used": False,
    }


def _performance_payload(snapshot: Snapshot) -> dict[str, Any]:
    net_kcs_fee = snapshot.kcs_fee_ledger_paid - snapshot.kcs_fee_ledger_refunded
    return {
        "symbol": snapshot.symbol,
        "valuation_basis": "persisted_anchor_price",
        "live_market_price_used": False,
        "initial_capital": _d(snapshot.initial_capital),
        "anchor_price": _d(snapshot.anchor_price),
        "managed_base": _d(snapshot.managed_base),
        "managed_quote": _d(snapshot.managed_quote),
        "anchor_portfolio_value": _d(snapshot.anchor_value),
        "anchor_pnl": _d(snapshot.anchor_pnl),
        "anchor_return_pct": _d(snapshot.anchor_return_pct, 6),
        "peak_portfolio_value": _d(snapshot.portfolio_high),
        "peak_pnl": _d(snapshot.peak_pnl),
        "peak_return_pct": _d(snapshot.peak_return_pct, 6),
        "drawdown_from_peak": _d(snapshot.drawdown_from_peak),
        "drawdown_pct": _d(snapshot.drawdown_pct, 6),
        "target_coin_value": _d(snapshot.target_coin_value),
        "cycle": snapshot.cycle,
        "maker_fill_count": snapshot.maker_fill_count,
        "taker_fill_count": snapshot.taker_fill_count,
        "fees": {
            "gross_base": _d(snapshot.gross_fees_base),
            "gross_quote": _d(snapshot.gross_fees_quote),
            "kcs_paid": _d(snapshot.kcs_fee_ledger_paid),
            "kcs_refunded": _d(snapshot.kcs_fee_ledger_refunded),
            "net_kcs_paid": _d(net_kcs_fee),
            "base_ledger_adjustment": _d(snapshot.fee_ledger_base_adjustment),
            "quote_ledger_adjustment": _d(snapshot.fee_ledger_quote_adjustment),
            "reported_maker_fee_rate": _d(snapshot.reported_maker_fee_rate),
            "reported_taker_fee_rate": _d(snapshot.reported_taker_fee_rate),
        },
        "state_modified_at": _iso(snapshot.state_mtime),
        "state_age_seconds": round(snapshot.state_age_seconds, 1),
        "state_fresh": snapshot.fresh,
    }


class HistoryStore:
    def __init__(self, path: Path, retention_days: int = DEFAULT_RETENTION_DAYS):
        self.path = path
        self.retention_days = max(1, retention_days)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=5)
        con.row_factory = sqlite3.Row
        return con

    def _init(self) -> None:
        with self._connect() as con:
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    ts INTEGER PRIMARY KEY,
                    cycle INTEGER NOT NULL,
                    anchor_price REAL NOT NULL,
                    managed_base REAL NOT NULL,
                    managed_quote REAL NOT NULL,
                    anchor_value REAL NOT NULL,
                    anchor_pnl REAL NOT NULL,
                    portfolio_high REAL NOT NULL,
                    maker_fills INTEGER NOT NULL,
                    taker_fills INTEGER NOT NULL
                )
                """
            )
            con.execute("CREATE INDEX IF NOT EXISTS idx_samples_cycle ON samples(cycle)")

    def maybe_record(self, snapshot: Snapshot, checkpoint_seconds: int) -> None:
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT ts, cycle FROM samples ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if row is not None:
                same_cycle = int(row["cycle"]) == snapshot.cycle
                recent = snapshot.ts - int(row["ts"]) < checkpoint_seconds
                if same_cycle and recent:
                    return
            ts = snapshot.ts
            while con.execute("SELECT 1 FROM samples WHERE ts = ?", (ts,)).fetchone():
                ts += 1
            con.execute(
                """
                INSERT INTO samples (
                    ts, cycle, anchor_price, managed_base, managed_quote,
                    anchor_value, anchor_pnl, portfolio_high, maker_fills, taker_fills
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    snapshot.cycle,
                    float(snapshot.anchor_price),
                    float(snapshot.managed_base),
                    float(snapshot.managed_quote),
                    float(snapshot.anchor_value),
                    float(snapshot.anchor_pnl),
                    float(snapshot.portfolio_high),
                    snapshot.maker_fill_count,
                    snapshot.taker_fill_count,
                ),
            )
            cutoff = snapshot.ts - self.retention_days * 86400
            con.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))

    def activity(self, window: str) -> dict[str, Any]:
        now = int(time.time())
        if window == "all":
            since = 0
        else:
            since = now - WINDOW_SECONDS[window]
        with self._lock, self._connect() as con:
            rows = con.execute(
                "SELECT * FROM samples WHERE ts >= ? ORDER BY ts ASC", (since,)
            ).fetchall()
        if not rows:
            return {
                "window": window,
                "samples": 0,
                "note": "No telemetry history is available for this window yet.",
            }
        first = rows[0]
        latest = rows[-1]
        anchor_values = [float(row["anchor_value"]) for row in rows]
        return {
            "window": window,
            "samples": len(rows),
            "from": _iso(int(first["ts"])),
            "to": _iso(int(latest["ts"])),
            "anchor_value_start": first["anchor_value"],
            "anchor_value_end": latest["anchor_value"],
            "anchor_value_change": float(latest["anchor_value"]) - float(first["anchor_value"]),
            "anchor_pnl_start": first["anchor_pnl"],
            "anchor_pnl_end": latest["anchor_pnl"],
            "anchor_pnl_change": float(latest["anchor_pnl"]) - float(first["anchor_pnl"]),
            "anchor_value_min": min(anchor_values),
            "anchor_value_max": max(anchor_values),
            "cycles_completed": int(latest["cycle"]) - int(first["cycle"]),
            "maker_fills_added": int(latest["maker_fills"]) - int(first["maker_fills"]),
            "taker_fills_added": int(latest["taker_fills"]) - int(first["taker_fills"]),
            "valuation_basis": "persisted_anchor_price",
            "live_market_price_used": False,
            "note": (
                "History is derived from persisted bot snapshots. Changes are anchor-valued "
                "portfolio changes, not a live market mark-to-market calculation."
            ),
        }

    def info(self) -> dict[str, Any]:
        with self._lock, self._connect() as con:
            row = con.execute(
                "SELECT COUNT(*) AS count, MIN(ts) AS oldest, MAX(ts) AS newest FROM samples"
            ).fetchone()
        return {
            "path": str(self.path),
            "samples": int(row["count"] or 0),
            "oldest": _iso(int(row["oldest"])) if row["oldest"] else None,
            "newest": _iso(int(row["newest"])) if row["newest"] else None,
            "retention_days": self.retention_days,
        }


class TelemetryApp:
    def __init__(
        self,
        *,
        config_path: Path,
        state_path: Path,
        token: str,
        history_path: Path,
        fresh_seconds: int,
        sample_seconds: int,
        checkpoint_seconds: int,
        retention_days: int,
    ):
        if not token:
            raise TelemetryError("Telemetry bearer token is empty")
        self.config_path = config_path
        self.state_path = state_path
        self.token = token
        self.fresh_seconds = max(30, fresh_seconds)
        self.sample_seconds = max(10, sample_seconds)
        self.checkpoint_seconds = max(self.sample_seconds, checkpoint_seconds)
        self.history = HistoryStore(history_path, retention_days)
        self.last_sample_error: str | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)

    def snapshot(self) -> Snapshot:
        return _read_snapshot(self.config_path, self.state_path, self.fresh_seconds)

    def start(self) -> None:
        self._record_once()
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _record_once(self) -> None:
        try:
            self.history.maybe_record(self.snapshot(), self.checkpoint_seconds)
            self.last_sample_error = None
        except Exception as exc:  # keep telemetry HTTP alive for diagnostics
            self.last_sample_error = str(exc)

    def _sample_loop(self) -> None:
        while not self._stop.wait(self.sample_seconds):
            self._record_once()

    def diagnostics(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "service": "kucoin-grid-bot-telemetry",
            "read_only_bot_state": True,
            "live_market_price_used": False,
            "history": self.history.info(),
            "last_sample_error": self.last_sample_error,
        }
        try:
            snapshot = self.snapshot()
            service = _bot_service_status()
            service_active = service.get("active")
            payload.update({
                "ok": snapshot.initialized and service_active is not False,
                "initialized": snapshot.initialized,
                "state_fresh": snapshot.fresh,
                "state_age_seconds": round(snapshot.state_age_seconds, 1),
                "state_modified_at": _iso(snapshot.state_mtime),
                "cycle": snapshot.cycle,
                "bot_service": service,
            })
            warnings: list[str] = []
            if not snapshot.initialized:
                warnings.append("Bot state is not initialized.")
            if service_active is False:
                warnings.append("kucoin-grid-bot.service is not active.")
            if not snapshot.fresh:
                warnings.append(
                    "Accounting state has not changed recently "
                    f"({snapshot.state_age_seconds:.0f}s old); this can be normal "
                    "between fills, so service state is checked separately."
                )
            if snapshot.anchor_price <= 0:
                warnings.append("Anchor price is not positive; valuation is unavailable.")
            if self.last_sample_error:
                warnings.append(f"History sampler error: {self.last_sample_error}")
            payload["warnings"] = warnings
        except Exception as exc:
            payload.update({"ok": False, "error": str(exc), "warnings": [str(exc)]})
        return payload


class Handler(BaseHTTPRequestHandler):
    server_version = "KuCoinGridBotTelemetry/1"

    @property
    def app(self) -> TelemetryApp:
        return self.server.app  # type: ignore[attr-defined]

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.app.token}"
        return hmac.compare_digest(supplied, expected)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._send(401, {"error": "unauthorized"})
            return
        parsed = urlsplit(self.path)
        try:
            if parsed.path in {"/", "/status"}:
                self._send(200, _status_payload(self.app.snapshot(), _bot_service_status()))
                return
            if parsed.path == "/performance":
                self._send(200, _performance_payload(self.app.snapshot()))
                return
            if parsed.path == "/activity":
                query = parse_qs(parsed.query)
                window = str((query.get("window") or ["day"])[0]).casefold()
                if window not in {*WINDOW_SECONDS, "all"}:
                    self._send(400, {"error": "window must be day, week, month, or all"})
                    return
                self._send(200, self.app.history.activity(window))
                return
            if parsed.path in {"/health", "/diagnostics"}:
                self._send(200, self.app.diagnostics())
                return
            self._send(404, {"error": "not found"})
        except TelemetryError as exc:
            self._send(503, {"error": str(exc)})
        except Exception as exc:
            self._send(500, {"error": f"telemetry failure: {exc}"})

    def do_POST(self) -> None:  # noqa: N802
        self._send(405, {"error": "read-only service; only GET is allowed"})

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: TelemetryApp):
        super().__init__(address, Handler)
        self.app = app


def _read_token(path: Path) -> str:
    env_token = os.environ.get("KUCOIN_TELEMETRY_TOKEN", "").strip()
    if env_token:
        return env_token
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise TelemetryError(f"Could not read telemetry token: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only KuCoin grid-bot telemetry sidecar")
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN)
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY)
    parser.add_argument("--fresh-seconds", type=int, default=DEFAULT_FRESH_SECONDS)
    parser.add_argument("--sample-seconds", type=int, default=DEFAULT_SAMPLE_SECONDS)
    parser.add_argument("--checkpoint-seconds", type=int, default=DEFAULT_CHECKPOINT_SECONDS)
    parser.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS)
    args = parser.parse_args()

    app = TelemetryApp(
        config_path=args.config,
        state_path=args.state,
        token=_read_token(args.token_file),
        history_path=args.history,
        fresh_seconds=args.fresh_seconds,
        sample_seconds=args.sample_seconds,
        checkpoint_seconds=args.checkpoint_seconds,
        retention_days=args.retention_days,
    )
    server = Server((args.listen, args.port), app)
    app.start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        app.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
