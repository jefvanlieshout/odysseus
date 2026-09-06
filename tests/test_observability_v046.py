import asyncio
import importlib.util
import json
import sys
from pathlib import Path
import tempfile

from src.tools import proxmox as proxmox_tool
from src.tools import kucoin_bot as kucoin_tool


def _load_telemetry_module():
    path = Path(__file__).resolve().parents[1] / "deploy" / "kucoin-grid-bot-telemetry" / "telemetry.py"
    spec = importlib.util.spec_from_file_location("kucoin_grid_bot_telemetry", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _guest_resources():
    return [
        {
            "type": "lxc",
            "vmid": 109,
            "name": "kucoin-grid-bot",
            "node": "pve-jnode0",
            "status": "running",
        }
    ]


def test_proxmox_guest_details_combines_status_config_and_derives_percentages(monkeypatch):
    monkeypatch.setattr(proxmox_tool, "_find_proxmox_integration", lambda: {"name": "Proxmox"})

    async def fake_resources(_integration):
        return _guest_resources(), None

    async def fake_get(_integration, path, params=None):
        assert params is None
        if path.endswith("/status/current"):
            return {
                "status": "running",
                "cpu": 0.25,
                "cpus": 2,
                "mem": 512,
                "maxmem": 1024,
                "disk": 100,
                "maxdisk": 400,
                "netin": 123,
                "netout": 456,
                "pressurecpusome": "0.00",
                "tags": "gridbot;paper-trading",
            }, None
        if path.endswith("/config"):
            return {"cores": 2, "cipassword": "secret", "net0": "name=eth0,bridge=vmbr0"}, None
        raise AssertionError(path)

    monkeypatch.setattr(proxmox_tool, "_cluster_resources", fake_resources)
    monkeypatch.setattr(proxmox_tool, "_pve_get", fake_get)
    result = asyncio.run(proxmox_tool.do_proxmox(json.dumps({"action": "guest_details", "guest": "109"})))
    assert result["exit_code"] == 0
    payload = json.loads(result["output"])
    assert payload["summary"]["cpu_percent"] == 25.0
    assert payload["summary"]["memory_percent"] == 50.0
    assert payload["summary"]["disk_percent"] == 25.0
    assert payload["config"]["cipassword"] == "[redacted]"


def test_proxmox_guest_metrics_uses_bounded_rrd_endpoint(monkeypatch):
    monkeypatch.setattr(proxmox_tool, "_find_proxmox_integration", lambda: {"name": "Proxmox"})

    async def fake_resources(_integration):
        return _guest_resources(), None

    seen = {}

    async def fake_get(_integration, path, params=None):
        seen["path"] = path
        seen["params"] = params
        return [
            {"time": 1, "cpu": 0.10, "mem": 25, "maxmem": 100, "disk": 20, "maxdisk": 100, "netin": 10, "netout": 20},
            {"time": 2, "cpu": 0.20, "mem": 50, "maxmem": 100, "disk": 21, "maxdisk": 100, "netin": 30, "netout": 40},
        ], None

    monkeypatch.setattr(proxmox_tool, "_cluster_resources", fake_resources)
    monkeypatch.setattr(proxmox_tool, "_pve_get", fake_get)
    result = asyncio.run(proxmox_tool.do_proxmox(json.dumps({"action": "guest_metrics", "guest": "109", "timeframe": "day"})))
    payload = json.loads(result["output"])
    assert seen["path"].endswith("/nodes/pve-jnode0/lxc/109/rrddata")
    assert seen["params"] == {"timeframe": "day", "cf": "AVERAGE"}
    assert payload["summary"]["cpu_percent_avg"] == 15.0
    assert payload["summary"]["cpu_percent_max"] == 20.0
    assert payload["summary"]["memory_percent_max"] == 50.0


def test_kucoin_telemetry_auth_accepts_header_and_bearer_modes():
    header = {
        "api_key": "Bearer secret-token",
        "auth_type": "header",
        "auth_header": "Authorization",
    }
    bearer = {
        "api_key": "secret-token",
        "auth_type": "bearer",
    }
    legacy = {
        "api_key": "Bearer secret-token",
    }

    assert kucoin_tool._telemetry_authorization_value(header) == (
        "Bearer secret-token", None
    )
    assert kucoin_tool._telemetry_authorization_value(bearer) == (
        "Bearer secret-token", None
    )
    assert kucoin_tool._telemetry_authorization_value(legacy) == (
        "Bearer secret-token", None
    )


def test_kucoin_bot_tool_is_action_bounded_and_read_only(monkeypatch):
    monkeypatch.setattr(
        kucoin_tool,
        "_find_kucoin_bot_integration",
        lambda: {"name": "KuCoin Grid Bot Telemetry"},
    )
    seen = {}

    async def fake_get(_integration, path, params=None):
        seen["path"] = path
        seen["params"] = params
        return {"anchor_pnl": "19.37", "live_market_price_used": False}, None

    monkeypatch.setattr(kucoin_tool, "_telemetry_get", fake_get)
    result = asyncio.run(
        kucoin_tool.do_kucoin_bot(json.dumps({"action": "activity", "window": "week"}))
    )
    payload = json.loads(result["output"])
    assert seen == {"path": "/activity", "params": {"window": "week"}}
    assert payload["read_only"] is True
    assert payload["live_market_price_used"] is False

    blocked = asyncio.run(kucoin_tool.do_kucoin_bot(json.dumps({"action": "place_order"})))
    assert blocked["exit_code"] == 1


def test_telemetry_uses_anchor_accounting_without_live_price():
    telemetry = _load_telemetry_module()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        config = root / "config.yaml"
        state = root / "state.json"
        config.write_text('symbol: KCS-USDC\ninitial_capital: "400"\nstate_file: ' + str(state) + '\n')
        state.write_text(json.dumps({
            "initialized": True,
            "symbol": "KCS-USDC",
            "cycle": 2661,
            "anchor_price": "7.232",
            "managed_base": "30.45708199",
            "managed_quote": "199.1077258149",
            "target_coin_value": "220.26636010337",
            "portfolio_high": "440.33272030274",
            "maker_fill_count": 1913,
            "taker_fill_count": 37,
            "gross_fees_base": "0",
            "gross_fees_quote": "0.8634825231",
            "kcs_fee_ledger_paid": "0.09441801",
            "kcs_fee_ledger_refunded": "0",
            "fee_ledger_base_adjustment": "0",
            "fee_ledger_quote_adjustment": "0",
            "reported_maker_fee_rate": "0.001",
            "reported_taker_fee_rate": "0.001",
        }))
        snap = telemetry._read_snapshot(config, state, 300)
        payload = telemetry._performance_payload(snap)
        assert payload["live_market_price_used"] is False
        assert payload["valuation_basis"] == "persisted_anchor_price"
        assert payload["initial_capital"] == "400"
        assert payload["anchor_portfolio_value"].startswith("419.37")
        assert payload["anchor_pnl"].startswith("19.37")
        assert payload["peak_pnl"].startswith("40.33")
        assert payload["drawdown_from_peak"].startswith("-20.959")


def test_telemetry_history_is_derived_and_separate_from_bot_state():
    telemetry = _load_telemetry_module()
    with tempfile.TemporaryDirectory() as tmp:
        history = telemetry.HistoryStore(Path(tmp) / "telemetry.sqlite3")
        config = Path(tmp) / "config.yaml"
        state = Path(tmp) / "state.json"
        config.write_text('initial_capital: "400"\nstate_file: ' + str(state) + '\n')
        state.write_text(json.dumps({
            "initialized": True,
            "symbol": "KCS-USDC",
            "cycle": 1,
            "anchor_price": "7",
            "managed_base": "30",
            "managed_quote": "200",
            "target_coin_value": "200",
            "portfolio_high": "420",
        }))
        snap = telemetry._read_snapshot(config, state, 300)
        history.maybe_record(snap, checkpoint_seconds=1)
        info = history.info()
        assert info["samples"] == 1
        assert state.exists()
        assert json.loads(state.read_text())["cycle"] == 1
