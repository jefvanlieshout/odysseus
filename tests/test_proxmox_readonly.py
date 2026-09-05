import asyncio
import json

from src.tools import proxmox as proxmox_tool


def test_proxmox_config_redacts_sensitive_fields():
    clean = proxmox_tool._sanitize_config({
        "name": "demo",
        "cipassword": "secret",
        "sshkeys": "ssh-rsa AAA...",
        "nested": {"api_token": "abc", "cores": 4},
    })
    assert clean["name"] == "demo"
    assert clean["cipassword"] == "[redacted]"
    assert clean["sshkeys"] == "[redacted]"
    assert clean["nested"]["api_token"] == "[redacted]"
    assert clean["nested"]["cores"] == 4


def test_proxmox_status_is_normalized(monkeypatch):
    monkeypatch.setattr(
        proxmox_tool,
        "_find_proxmox_integration",
        lambda: {"id": "pve", "preset": "proxmox"},
    )

    async def fake_resources(_integration):
        return [
            {
                "type": "node",
                "node": "pve",
                "status": "online",
                "cpu": 0.25,
                "mem": 25,
                "maxmem": 100,
            },
            {
                "type": "lxc",
                "vmid": 101,
                "name": "immich",
                "node": "pve",
                "status": "running",
                "mem": 10,
                "maxmem": 20,
            },
            {
                "type": "storage",
                "storage": "local-zfs",
                "node": "pve",
                "status": "available",
                "disk": 50,
                "maxdisk": 100,
            },
        ], None

    monkeypatch.setattr(
        proxmox_tool,
        "_cluster_resources",
        fake_resources,
    )

    result = asyncio.run(
        proxmox_tool.do_proxmox(
            '{"action":"status"}',
            owner="magical",
        )
    )
    assert result["exit_code"] == 0
    payload = json.loads(result["output"])
    assert payload["summary"]["nodes_online"] == 1
    assert payload["summary"]["guests_running"] == 1
    assert payload["nodes"][0]["cpu_percent"] == 25.0


def test_native_proxmox_function_call_converts():
    from src.agent_tools import function_call_to_tool_block

    block = function_call_to_tool_block(
        "proxmox",
        '{"action":"nodes"}',
    )
    assert block is not None
    assert block.tool_type == "proxmox"
    assert json.loads(block.content) == {"action": "nodes"}
