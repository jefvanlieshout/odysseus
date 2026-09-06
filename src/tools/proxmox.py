"""Read-only Proxmox VE monitoring tool for Gwen."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, Optional

import httpx

from src.integrations import (
    _PinnedAsyncTransport,
    _join_integration_url,
    _normalize_integration_base_url,
    _validated_ips,
    load_integrations,
)
from src.url_safety import _default_resolver, check_outbound_url

_MAX_TASKS = 100
_WARN_STORAGE_PERCENT = 85.0
_MAX_RRD_RETURNED_SAMPLES = 48


def _parse_args(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if not raw:
        return {"action": "status"}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"action": raw}
    return value if isinstance(value, dict) else {"action": "status"}


def _find_proxmox_integration() -> Optional[Dict[str, Any]]:
    candidates = []
    for item in load_integrations():
        if not isinstance(item, dict) or not item.get("enabled", True):
            continue
        preset = str(item.get("preset") or "").strip().casefold()
        name = str(item.get("name") or "").strip().casefold()
        if preset == "proxmox":
            return item
        if "proxmox" in name:
            candidates.append(item)
    return candidates[0] if len(candidates) == 1 else None


def _safe_error_body(response: httpx.Response) -> str:
    return (response.text or "").strip().replace("\x00", "")[:800]


async def _pve_get(
    integration: Dict[str, Any],
    path: str,
    params: Optional[Dict[str, Any]] = None,
) -> tuple[Any, Optional[str]]:
    if not path.startswith("/api2/json/"):
        return None, "Internal Proxmox path rejected."

    try:
        base = _normalize_integration_base_url(integration.get("base_url", ""))
    except ValueError as exc:
        return None, str(exc)

    for suffix in ("/api2/json", "/api2"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break

    token = str(integration.get("api_key") or "").strip()
    if not token.startswith("PVEAPIToken="):
        return None, (
            "Proxmox credential must be the full Authorization value "
            "'PVEAPIToken=user@realm!tokenid=SECRET'."
        )

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
        url, block_private=block_private, resolver=_recording_resolver
    )
    if not ok:
        return None, f"Proxmox URL rejected: {reason}"

    pinned_ips = _validated_ips(resolved_ips)
    if not pinned_ips:
        return None, "Proxmox host did not resolve to a usable address."

    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            transport=_PinnedAsyncTransport(pinned_ips),
        ) as client:
            response = await client.get(
                url,
                params=params,
                headers={
                    "Authorization": token,
                    "Accept": "application/json",
                },
            )
    except httpx.TimeoutException:
        return None, "Proxmox request timed out."
    except httpx.ConnectError as exc:
        return None, (
            "Could not establish the Proxmox HTTPS connection. If pveproxy uses "
            "its default private CA, trust the Proxmox CA in Odysseus rather than "
            f"disabling TLS verification. ({exc})"
        )
    except httpx.RequestError as exc:
        return None, f"Proxmox request failed: {exc}"

    if response.status_code >= 400:
        body = _safe_error_body(response)
        suffix = f" — {body}" if body else ""
        return None, f"Proxmox API returned HTTP {response.status_code}{suffix}"

    try:
        decoded = response.json()
    except ValueError:
        return None, "Proxmox returned a non-JSON response."

    if not isinstance(decoded, dict) or "data" not in decoded:
        return None, "Proxmox returned an unexpected response shape."
    return decoded.get("data"), None


def _task_status_bucket(task: Dict[str, Any]) -> str:
    # Keep warning-only task completions separate from actual failures.
    status = str((task or {}).get("status") or "").strip()
    if not status:
        return "unknown"
    upper = status.upper()
    if upper == "OK":
        return "ok"
    if upper.startswith("WARNINGS:"):
        return "warning"
    return "failed"


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _pct(used: Any, total: Any) -> float:
    total_num = _num(total)
    if total_num <= 0:
        return 0.0
    return round((_num(used) / total_num) * 100.0, 1)


def _cpu_pct(value: Any) -> float:
    raw = _num(value)
    return round(raw * 100.0, 1) if raw <= 1.0 else round(raw, 1)


def _resource_summary(resource: Dict[str, Any]) -> Dict[str, Any]:
    kind = str(resource.get("type") or "")
    result: Dict[str, Any] = {
        "type": kind,
        "status": resource.get("status"),
        "node": resource.get("node"),
    }

    if kind == "node":
        result.update({
            "name": resource.get("node") or resource.get("name"),
            "cpu_percent": _cpu_pct(resource.get("cpu")),
            "cpu_threads": resource.get("maxcpu"),
            "memory_used": resource.get("mem"),
            "memory_total": resource.get("maxmem"),
            "memory_percent": _pct(resource.get("mem"), resource.get("maxmem")),
            "uptime_seconds": resource.get("uptime"),
        })
    elif kind in {"qemu", "lxc"}:
        result.update({
            "vmid": resource.get("vmid"),
            "name": resource.get("name") or f"{kind}-{resource.get('vmid')}",
            "cpu_percent": _cpu_pct(resource.get("cpu")),
            "cpu_limit": resource.get("maxcpu"),
            "memory_used": resource.get("mem"),
            "memory_total": resource.get("maxmem"),
            "memory_percent": _pct(resource.get("mem"), resource.get("maxmem")),
            "disk_used": resource.get("disk"),
            "disk_total": resource.get("maxdisk"),
            "uptime_seconds": resource.get("uptime"),
            "template": bool(resource.get("template")),
        })
    elif kind == "storage":
        result.update({
            "storage": resource.get("storage"),
            "disk_used": resource.get("disk"),
            "disk_total": resource.get("maxdisk"),
            "disk_percent": _pct(resource.get("disk"), resource.get("maxdisk")),
            "shared": bool(resource.get("shared")),
        })
    return result


def _guest_identity(guest: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "vmid": guest.get("vmid"),
        "name": guest.get("name"),
        "type": guest.get("type"),
        "node": guest.get("node"),
    }


def _guest_status_summary(status: Any) -> Dict[str, Any]:
    data = status if isinstance(status, dict) else {}
    summary: Dict[str, Any] = {
        "status": data.get("status"),
        "uptime_seconds": data.get("uptime"),
        "cpu_percent": _cpu_pct(data.get("cpu")),
        "cpu_count": data.get("cpus") or data.get("maxcpu"),
        "memory_used": data.get("mem"),
        "memory_total": data.get("maxmem"),
        "memory_percent": _pct(data.get("mem"), data.get("maxmem")),
        "swap_used": data.get("swap"),
        "swap_total": data.get("maxswap"),
        "swap_percent": _pct(data.get("swap"), data.get("maxswap")),
        "disk_used": data.get("disk"),
        "disk_total": data.get("maxdisk"),
        "disk_percent": _pct(data.get("disk"), data.get("maxdisk")),
        "network_in_bytes": data.get("netin"),
        "network_out_bytes": data.get("netout"),
        "disk_read_bytes": data.get("diskread"),
        "disk_write_bytes": data.get("diskwrite"),
        "tags": data.get("tags"),
    }
    pressure = {
        key.removeprefix("pressure"): data.get(key)
        for key in (
            "pressurecpusome",
            "pressurecpufull",
            "pressurememorysome",
            "pressurememoryfull",
            "pressureiosome",
            "pressureiofull",
        )
        if data.get(key) is not None
    }
    if pressure:
        summary["pressure"] = pressure

    warnings: list[str] = []
    if summary["memory_percent"] >= 90:
        warnings.append(f"Memory usage is {summary['memory_percent']:.1f}%.")
    if summary["disk_percent"] >= 90:
        warnings.append(f"Disk usage is {summary['disk_percent']:.1f}%.")
    if summary["swap_percent"] >= 50:
        warnings.append(f"Swap usage is {summary['swap_percent']:.1f}%.")
    summary["warnings"] = warnings
    return summary


def _rrd_sample_summary(sample: Any) -> Dict[str, Any]:
    row = sample if isinstance(sample, dict) else {}
    return {
        "timestamp": row.get("time"),
        "cpu_percent": _cpu_pct(row.get("cpu")),
        "memory_used": row.get("mem"),
        "memory_total": row.get("maxmem"),
        "memory_percent": _pct(row.get("mem"), row.get("maxmem")),
        "disk_used": row.get("disk"),
        "disk_total": row.get("maxdisk"),
        "disk_percent": _pct(row.get("disk"), row.get("maxdisk")),
        "network_in_rate": row.get("netin"),
        "network_out_rate": row.get("netout"),
        "disk_read_rate": row.get("diskread"),
        "disk_write_rate": row.get("diskwrite"),
    }


def _metric_summary(samples: list[Dict[str, Any]]) -> Dict[str, Any]:
    def values(key: str) -> list[float]:
        return [_num(sample.get(key)) for sample in samples if sample.get(key) is not None]

    cpu = values("cpu_percent")
    memory = values("memory_percent")
    disk = values("disk_percent")
    netin = values("network_in_rate")
    netout = values("network_out_rate")
    return {
        "samples": len(samples),
        "cpu_percent_avg": round(sum(cpu) / len(cpu), 2) if cpu else 0.0,
        "cpu_percent_max": round(max(cpu), 2) if cpu else 0.0,
        "memory_percent_avg": round(sum(memory) / len(memory), 2) if memory else 0.0,
        "memory_percent_max": round(max(memory), 2) if memory else 0.0,
        "disk_percent_max": round(max(disk), 2) if disk else 0.0,
        "network_in_rate_max": max(netin) if netin else 0.0,
        "network_out_rate_max": max(netout) if netout else 0.0,
    }



def _downsample_samples(
    samples: list[Dict[str, Any]],
    limit: int = _MAX_RRD_RETURNED_SAMPLES,
) -> list[Dict[str, Any]]:
    """Return an evenly-spaced bounded timeline while preserving endpoints.

    The full RRD series remains available for aggregate calculations. Only the
    timeline included in the model-facing tool result is bounded.
    """
    if limit <= 0 or not samples:
        return []
    if len(samples) <= limit:
        return list(samples)
    if limit == 1:
        return [samples[-1]]

    last = len(samples) - 1
    indexes = [round(i * last / (limit - 1)) for i in range(limit)]
    selected: list[Dict[str, Any]] = []
    seen: set[int] = set()
    for index in indexes:
        if index in seen:
            continue
        seen.add(index)
        selected.append(samples[index])
    return selected


async def _cluster_resources(
    integration: Dict[str, Any],
) -> tuple[list[Dict[str, Any]], Optional[str]]:
    data, error = await _pve_get(integration, "/api2/json/cluster/resources")
    if error:
        return [], error
    if not isinstance(data, list):
        return [], "Proxmox cluster resources response was not a list."
    return [x for x in data if isinstance(x, dict)], None


def _resolve_guest(
    resources: Iterable[Dict[str, Any]],
    query: Any,
) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    needle = str(query or "").strip()
    if not needle:
        return None, "guest is required (VMID or guest name)."

    guests = [r for r in resources if r.get("type") in {"qemu", "lxc"}]
    exact = [
        r for r in guests
        if str(r.get("vmid")) == needle
        or str(r.get("name") or "").casefold() == needle.casefold()
    ]
    if len(exact) == 1:
        return exact[0], None

    partial = [
        r for r in guests
        if needle.casefold() in str(r.get("name") or "").casefold()
    ]
    if len(partial) == 1:
        return partial[0], None
    if not exact and not partial:
        return None, f"No Proxmox guest matched {needle!r}."

    choices = exact or partial
    names = ", ".join(
        f"{r.get('vmid')} ({r.get('name') or r.get('type')})"
        for r in choices[:10]
    )
    return None, f"Guest name is ambiguous: {names}"


_SENSITIVE_CONFIG_TOKENS = (
    "password",
    "passwd",
    "secret",
    "token",
    "private",
    "sshkeys",
)


def _sanitize_config(value: Any) -> Any:
    if isinstance(value, dict):
        clean: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            folded = key_text.casefold()
            if any(token in folded for token in _SENSITIVE_CONFIG_TOKENS):
                clean[key_text] = "[redacted]"
            else:
                clean[key_text] = _sanitize_config(item)
        return clean
    if isinstance(value, list):
        return [_sanitize_config(item) for item in value]
    return value


def _json_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "output": json.dumps(payload, indent=2, ensure_ascii=False),
        "exit_code": 0,
        "untrusted_content": True,
    }


async def do_proxmox(
    content: str,
    owner: Optional[str] = None,
) -> Dict[str, Any]:
    args = _parse_args(content)
    action = str(args.get("action") or "status").strip().casefold()

    integration = _find_proxmox_integration()
    if not integration:
        return {
            "error": (
                "No enabled Proxmox integration is configured. Add the 'Proxmox VE' "
                "integration in Odysseus with base URL https://HOST:8006 and a "
                "privilege-separated PVEAuditor API token."
            ),
            "exit_code": 1,
        }

    resource_actions = {
        "status", "nodes", "guests", "storages",
        "guest_status", "guest_config", "guest_details", "guest_metrics",
        "diagnostics",
    }
    if action in resource_actions:
        resources, error = await _cluster_resources(integration)
        if error:
            return {"error": error, "exit_code": 1}

    if action == "nodes":
        return _json_result({
            "action": action,
            "nodes": [
                _resource_summary(r)
                for r in resources
                if r.get("type") == "node"
            ],
        })

    if action == "guests":
        guests = [
            _resource_summary(r)
            for r in resources
            if r.get("type") in {"qemu", "lxc"}
        ]
        guests.sort(
            key=lambda g: (
                str(g.get("node") or ""),
                int(g.get("vmid") or 0),
            )
        )
        return _json_result({"action": action, "guests": guests})

    if action == "storages":
        return _json_result({
            "action": action,
            "storages": [
                _resource_summary(r)
                for r in resources
                if r.get("type") == "storage"
            ],
        })

    if action in {"guest_status", "guest_config", "guest_details", "guest_metrics"}:
        guest, error = _resolve_guest(
            resources,
            args.get("guest") or args.get("vmid") or args.get("name"),
        )
        if error:
            return {"error": error, "exit_code": 1}
        assert guest is not None

        kind = str(guest.get("type"))
        node = str(guest.get("node"))
        vmid = str(guest.get("vmid"))
        identity = _guest_identity(guest)

        if action in {"guest_status", "guest_config"}:
            suffix = "status/current" if action == "guest_status" else "config"
            data, error = await _pve_get(
                integration,
                f"/api2/json/nodes/{node}/{kind}/{vmid}/{suffix}",
            )
            if error:
                return {"error": error, "exit_code": 1}
            return _json_result({
                "action": action,
                "guest": identity,
                "data": _sanitize_config(data)
                if action == "guest_config"
                else data,
            })

        if action == "guest_details":
            status, status_error = await _pve_get(
                integration,
                f"/api2/json/nodes/{node}/{kind}/{vmid}/status/current",
            )
            config, config_error = await _pve_get(
                integration,
                f"/api2/json/nodes/{node}/{kind}/{vmid}/config",
            )
            if status_error:
                return {"error": status_error, "exit_code": 1}
            if config_error:
                return {"error": config_error, "exit_code": 1}
            return _json_result({
                "action": action,
                "guest": identity,
                "summary": _guest_status_summary(status),
                "status": status,
                "config": _sanitize_config(config),
            })

        timeframe = str(args.get("timeframe") or "day").strip().casefold()
        if timeframe not in {"hour", "day", "week", "month", "year"}:
            return {
                "error": "timeframe must be hour, day, week, month, or year.",
                "exit_code": 1,
            }
        consolidation = str(args.get("cf") or "AVERAGE").strip().upper()
        if consolidation not in {"AVERAGE", "MAX"}:
            return {"error": "cf must be AVERAGE or MAX.", "exit_code": 1}
        data, error = await _pve_get(
            integration,
            f"/api2/json/nodes/{node}/{kind}/{vmid}/rrddata",
            params={"timeframe": timeframe, "cf": consolidation},
        )
        if error:
            return {"error": error, "exit_code": 1}
        raw_samples = data if isinstance(data, list) else []
        samples = [
            _rrd_sample_summary(sample)
            for sample in raw_samples
            if isinstance(sample, dict)
        ]
        returned_samples = _downsample_samples(samples)
        return _json_result({
            "action": action,
            "guest": identity,
            "timeframe": timeframe,
            "cf": consolidation,
            "summary": _metric_summary(samples),
            "sample_count": len(samples),
            "returned_sample_count": len(returned_samples),
            "timeline_downsampled": len(returned_samples) < len(samples),
            "samples": returned_samples,
        })

    if action == "tasks":
        try:
            limit = max(
                1,
                min(int(args.get("limit") or 50), _MAX_TASKS),
            )
        except (TypeError, ValueError):
            limit = 50
        # This endpoint exposes no query parameters on supported PVE versions.
        # Fetch the recent cluster task list as-is, then apply our bounded
        # presentation limit locally.
        data, error = await _pve_get(
            integration,
            "/api2/json/cluster/tasks",
        )
        if error:
            return {"error": error, "exit_code": 1}
        tasks = data if isinstance(data, list) else []
        return _json_result({
            "action": action,
            "tasks": tasks[:limit],
        })

    if action in {"status", "diagnostics"}:
        nodes = [
            _resource_summary(r)
            for r in resources
            if r.get("type") == "node"
        ]
        guests = [
            _resource_summary(r)
            for r in resources
            if r.get("type") in {"qemu", "lxc"}
        ]
        storages = [
            _resource_summary(r)
            for r in resources
            if r.get("type") == "storage"
        ]

        payload: Dict[str, Any] = {
            "action": action,
            "summary": {
                "nodes_total": len(nodes),
                "nodes_online": sum(
                    1 for n in nodes
                    if n.get("status") == "online"
                ),
                "guests_total": len(guests),
                "guests_running": sum(
                    1 for g in guests
                    if g.get("status") == "running"
                ),
                "guests_stopped": sum(
                    1 for g in guests
                    if g.get("status") == "stopped"
                ),
                "storages_total": len(storages),
            },
            "nodes": nodes,
            "stopped_guests": [
                {
                    "vmid": g.get("vmid"),
                    "name": g.get("name"),
                    "type": g.get("type"),
                    "node": g.get("node"),
                }
                for g in guests
                if g.get("status") == "stopped"
                and not g.get("template")
            ],
            "storage": storages,
        }

        if action == "diagnostics":
            warnings = []
            for node in nodes:
                if node.get("status") != "online":
                    warnings.append(
                        f"Node {node.get('name')} is "
                        f"{node.get('status') or 'not online'}."
                    )
            for storage in storages:
                used = _num(storage.get("disk_percent"))
                if used >= _WARN_STORAGE_PERCENT:
                    warnings.append(
                        f"Storage {storage.get('storage')} on "
                        f"{storage.get('node')} is {used:.1f}% full."
                    )

            # /cluster/tasks is a parameterless GET on PVE; bound and filter
            # the returned recent task list locally instead of sending an
            # unsupported `limit` query parameter.
            task_data, task_error = await _pve_get(
                integration,
                "/api2/json/cluster/tasks",
            )
            recent_tasks = (
                task_data if isinstance(task_data, list) else []
            )
            warning_tasks = [
                task
                for task in recent_tasks
                if isinstance(task, dict)
                and _task_status_bucket(task) == "warning"
            ]
            failed_tasks = [
                task
                for task in recent_tasks
                if isinstance(task, dict)
                and _task_status_bucket(task) == "failed"
            ]
            if task_error:
                warnings.append(
                    f"Could not read recent tasks: {task_error}"
                )
            if warning_tasks:
                warnings.append(
                    f"{len(warning_tasks)} recent Proxmox task(s) "
                    "completed with warnings."
                )
            if failed_tasks:
                warnings.append(
                    f"{len(failed_tasks)} recent Proxmox task(s) failed."
                )

            payload["warnings"] = warnings
            payload["recent_warning_tasks"] = warning_tasks[:10]
            payload["recent_failed_tasks"] = failed_tasks[:10]

        return _json_result(payload)

    return {
        "error": (
            "Unknown Proxmox action. Use status, nodes, guests, guest_status, "
            "guest_config, guest_details, guest_metrics, storages, tasks, or diagnostics."
        ),
        "exit_code": 1,
    }
