"""Read-only KuCoin grid-bot telemetry tool for Gwen."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

import httpx

from src.integrations import (
    _PinnedAsyncTransport,
    _join_integration_url,
    _normalize_integration_base_url,
    _validated_ips,
    load_integrations,
)
from src.url_safety import _default_resolver, check_outbound_url

_ALLOWED_ACTIONS = {
    "status": "/status",
    "performance": "/performance",
    "activity": "/activity",
    "diagnostics": "/diagnostics",
}
_ALLOWED_WINDOWS = {"day", "week", "month", "all"}


def _parse_args(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if not raw:
        return {"action": "status"}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"action": raw}
    return value if isinstance(value, dict) else {"action": "status"}


def _find_kucoin_bot_integration() -> Optional[Dict[str, Any]]:
    candidates: list[Dict[str, Any]] = []
    for item in load_integrations():
        if not isinstance(item, dict) or not item.get("enabled", True):
            continue
        preset = str(item.get("preset") or "").strip().casefold()
        name = str(item.get("name") or "").strip().casefold()
        if preset == "kucoin_bot_telemetry":
            return item
        if "kucoin" in name and ("telemetry" in name or "grid bot" in name or "gridbot" in name):
            candidates.append(item)
    return candidates[0] if len(candidates) == 1 else None


def _telemetry_authorization_value(
    integration: Dict[str, Any],
) -> tuple[Optional[str], Optional[str]]:
    """Normalize supported integration auth modes to one Authorization value."""
    token = str(integration.get("api_key") or "").strip()
    if not token:
        return None, "KuCoin telemetry credential is missing."

    auth_type = str(integration.get("auth_type") or "header").strip().casefold()
    auth_header = str(integration.get("auth_header") or "Authorization").strip()

    if auth_type == "bearer":
        authorization = token if token.startswith("Bearer ") else f"Bearer {token}"
    elif auth_type == "header":
        if auth_header.casefold() != "authorization":
            return None, (
                "KuCoin telemetry header authentication must use the "
                "Authorization header."
            )
        authorization = token if token.startswith("Bearer ") else f"Bearer {token}"
    elif token.startswith("Bearer "):
        # Compatibility for integrations created before the preset carried
        # explicit auth metadata.
        authorization = token
    else:
        return None, (
            "KuCoin telemetry integration must use Authorization header or "
            "Bearer authentication."
        )

    if len(authorization) <= len("Bearer "):
        return None, "KuCoin telemetry bearer credential is empty."
    return authorization, None


def _safe_error_body(response: httpx.Response) -> str:
    return (response.text or "").strip().replace("\x00", "")[:800]


async def _telemetry_get(
    integration: Dict[str, Any],
    path: str,
    params: Optional[Dict[str, Any]] = None,
) -> tuple[Any, Optional[str]]:
    if path not in set(_ALLOWED_ACTIONS.values()):
        return None, "Internal KuCoin telemetry path rejected."

    try:
        base = _normalize_integration_base_url(integration.get("base_url", ""))
    except ValueError as exc:
        return None, str(exc)

    authorization, auth_error = _telemetry_authorization_value(integration)
    if auth_error:
        return None, auth_error
    assert authorization is not None

    url = _join_integration_url(base, path)
    block_private = os.getenv(
        "INTEGRATION_API_BLOCK_PRIVATE_IPS", "false"
    ).lower() == "true"
    resolved_ips: list[str] = []

    def _recording_resolver(host: str) -> list[str]:
        ips = _default_resolver(host)
        resolved_ips[:] = ips
        return ips

    ok, reason = check_outbound_url(
        url,
        block_private=block_private,
        resolver=_recording_resolver,
    )
    if not ok:
        return None, f"KuCoin telemetry URL rejected: {reason}"

    pinned_ips = _validated_ips(resolved_ips)
    if not pinned_ips:
        return None, "KuCoin telemetry host did not resolve to a usable address."

    try:
        async with httpx.AsyncClient(
            timeout=10.0,
            transport=_PinnedAsyncTransport(pinned_ips),
        ) as client:
            response = await client.get(
                url,
                params=params,
                headers={
                    "Authorization": authorization,
                    "Accept": "application/json",
                },
            )
    except httpx.TimeoutException:
        return None, "KuCoin telemetry request timed out."
    except httpx.ConnectError as exc:
        return None, f"Could not connect to KuCoin telemetry: {exc}"
    except httpx.RequestError as exc:
        return None, f"KuCoin telemetry request failed: {exc}"

    if response.status_code >= 400:
        body = _safe_error_body(response)
        suffix = f" — {body}" if body else ""
        return None, f"KuCoin telemetry returned HTTP {response.status_code}{suffix}"

    try:
        decoded = response.json()
    except ValueError:
        return None, "KuCoin telemetry returned a non-JSON response."
    if not isinstance(decoded, dict):
        return None, "KuCoin telemetry returned an unexpected response shape."
    return decoded, None


def _json_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "output": json.dumps(payload, indent=2, ensure_ascii=False),
        "exit_code": 0,
        "untrusted_content": True,
    }


async def do_kucoin_bot(
    content: str,
    owner: Optional[str] = None,
) -> Dict[str, Any]:
    """Read approved grid-bot telemetry; never place/cancel/change trades."""
    args = _parse_args(content)
    action = str(args.get("action") or "status").strip().casefold()
    if action not in _ALLOWED_ACTIONS:
        return {
            "error": (
                "Unknown KuCoin bot action. Use status, performance, activity, "
                "or diagnostics."
            ),
            "exit_code": 1,
        }

    integration = _find_kucoin_bot_integration()
    if not integration:
        return {
            "error": (
                "No enabled 'KuCoin Grid Bot Telemetry' integration is configured. "
                "Install the read-only telemetry sidecar in the bot LXC, then add "
                "its base URL and Bearer token in Odysseus Integrations."
            ),
            "exit_code": 1,
        }

    params: Dict[str, Any] | None = None
    if action == "activity":
        window = str(args.get("window") or "day").strip().casefold()
        if window not in _ALLOWED_WINDOWS:
            return {
                "error": "window must be day, week, month, or all.",
                "exit_code": 1,
            }
        params = {"window": window}

    data, error = await _telemetry_get(
        integration,
        _ALLOWED_ACTIONS[action],
        params=params,
    )
    if error:
        return {"error": error, "exit_code": 1}

    return _json_result({
        "action": action,
        "source": "kucoin-grid-bot-telemetry",
        "read_only": True,
        "live_market_price_used": False,
        "data": data,
    })
