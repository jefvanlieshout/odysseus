import json

from src import context_compactor
from src.tools import proxmox as proxmox_tool


def test_downsample_samples_bounds_rrd_but_keeps_endpoints():
    rows = [{"timestamp": i, "cpu_percent": i / 10} for i in range(1000)]
    sampled = proxmox_tool._downsample_samples(rows)
    assert len(sampled) == 48
    assert sampled[0]["timestamp"] == 0
    assert sampled[-1]["timestamp"] == 999


def test_context_trim_preserves_latest_user_and_tool_exchange():
    user_text = "Summarize LXC 109 resource usage over the last day."
    messages = [
        {"role": "system", "content": "You are Atlas."},
        {"role": "user", "content": user_text},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_rrd",
                    "type": "function",
                    "function": {
                        "name": "proxmox",
                        "arguments": json.dumps(
                            {
                                "action": "guest_metrics",
                                "guest": "109",
                                "timeframe": "day",
                            }
                        ),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call_rrd",
            "content": "x" * 600_000,
        },
    ]

    trimmed = context_compactor.trim_for_context(
        messages,
        context_length=27_200,
        reserve_tokens=1024,
    )

    assert any(
        m.get("role") == "user" and m.get("content") == user_text
        for m in trimmed
    )
    assert any(
        m.get("role") == "assistant" and m.get("tool_calls")
        for m in trimmed
    )
    assert any(m.get("role") == "tool" for m in trimmed)
    assert context_compactor.estimate_tokens(trimmed) <= 26_176
