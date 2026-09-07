from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any


CALLBACK_PREFIX = "odyask:"
MAX_CALLBACK_BYTES = 64


@dataclass(frozen=True)
class InteractionButton:
    text: str
    callback_data: str


@dataclass(frozen=True)
class InteractionKeyboard:
    inline_keyboard: tuple[tuple[InteractionButton, ...], ...]


@dataclass(frozen=True)
class InteractionOption:
    label: str
    description: str = ""


@dataclass(frozen=True)
class Interaction:
    kind: str
    question: str
    options: tuple[InteractionOption, ...]
    interaction_id: str


def _stable_id(event: dict[str, Any]) -> str:
    nested = event.get("data")
    source = nested if isinstance(nested, dict) else event

    explicit = (
        source.get("interaction_id")
        or source.get("request_id")
        or source.get("id")
        or source.get("approval_id")
        or event.get("interaction_id")
        or event.get("request_id")
        or event.get("id")
        or event.get("approval_id")
    )
    if explicit:
        return str(explicit)

    material = json.dumps(
        {
            "type": event.get("type") or source.get("type"),
            "question": source.get("question") or source.get("message"),
            "options": source.get("options"),
            "tool": source.get("tool"),
            "command": source.get("command"),
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def normalize_interaction(event: dict[str, Any]) -> Interaction | None:
    event_type = str(event.get("type") or "").strip().lower()
    nested = event.get("data")
    payload = nested if isinstance(nested, dict) else event
    payload_kind = str(payload.get("kind") or "").strip().lower()

    # Odysseus tool approvals use the same ask_user control plane.
    # Telegram must only present the choice; the user still has to
    # explicitly tap Approve or Deny.
    if event_type == "ask_user" and payload_kind == "tool_approval":
        question = str(
            payload.get("question")
            or payload.get("message")
            or "Approve this action?"
        ).strip()

        return Interaction(
            kind="approval",
            question=question,
            options=(InteractionOption("Approve"), InteractionOption("Deny")),
            interaction_id=_stable_id(event),
        )

    if event_type == "ask_user":
        question = str(payload.get("question") or payload.get("message") or "").strip()
        if not question:
            return None

        raw_options = payload.get("options") or []
        options: list[InteractionOption] = []
        if isinstance(raw_options, list):
            for item in raw_options:
                if isinstance(item, str):
                    label = item.strip()
                    if label:
                        options.append(InteractionOption(label=label))
                elif isinstance(item, dict):
                    label = str(
                        item.get("label")
                        or item.get("value")
                        or item.get("name")
                        or ""
                    ).strip()
                    description = str(item.get("description") or "").strip()
                    if label:
                        options.append(InteractionOption(label=label, description=description))

        return Interaction(
            kind="ask_user",
            question=question,
            options=tuple(options),
            interaction_id=_stable_id(event),
        )

    if event_type in {
        "approval_request",
        "approval_required",
        "confirm_action",
        "confirmation_required",
        "requires_confirmation",
    }:
        question = str(
            payload.get("question")
            or payload.get("message")
            or "Approve this action?"
        ).strip()
        return Interaction(
            kind="approval",
            question=question,
            options=(InteractionOption("Approve"), InteractionOption("Deny")),
            interaction_id=_stable_id(event),
        )

    return None


def interaction_text(interaction: Interaction) -> str:
    lines = [interaction.question]
    described = [o for o in interaction.options if o.description.strip()]
    if described:
        lines.append("")
        for option in described:
            lines.append(f"• {option.label} — {option.description}")
    return "\n".join(lines)


def _callback_data(interaction: Interaction, index: int) -> str:
    data = f"{CALLBACK_PREFIX}{interaction.interaction_id}:{index}"
    if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError("interaction callback_data exceeds Telegram's 64-byte limit")
    return data


def interaction_keyboard(interaction: Interaction) -> InteractionKeyboard | None:
    if not interaction.options:
        return None

    rows = []
    row = []
    for index, option in enumerate(interaction.options):
        row.append(
            InteractionButton(
                text=option.label[:64],
                callback_data=_callback_data(interaction, index),
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    return InteractionKeyboard(tuple(tuple(row) for row in rows))


def decode_callback(data: str) -> tuple[str, int] | None:
    if not data.startswith(CALLBACK_PREFIX):
        return None
    payload = data[len(CALLBACK_PREFIX):]
    interaction_id, sep, raw_index = payload.rpartition(":")
    if not sep or not interaction_id:
        return None
    try:
        index = int(raw_index)
    except ValueError:
        return None
    if index < 0:
        return None
    return interaction_id, index
