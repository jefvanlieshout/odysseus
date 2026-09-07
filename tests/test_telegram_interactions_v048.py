from pathlib import Path
import importlib.util
import sys

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "assistant" / "telegram" / "bot.py"
INTERACTIONS = ROOT / "assistant" / "telegram" / "interactions.py"


def _load_interactions():
    spec = importlib.util.spec_from_file_location("telegram_interactions_v048", INTERACTIONS)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_ask_user_options_survive_normalization():
    m = _load_interactions()
    event = {
        "type": "ask_user",
        "question": "Which name shall I go by?",
        "options": [
            {"label": "Iris", "description": "Greek: rainbow"},
            {"label": "Nia", "description": "Greek: gift"},
            {"label": "Echo", "description": "Greek nymph"},
            {"label": "Keep Atlas", "description": "No change"},
        ],
    }
    interaction = m.normalize_interaction(event)
    assert interaction is not None
    assert interaction.kind == "ask_user"
    assert [o.label for o in interaction.options] == ["Iris", "Nia", "Echo", "Keep Atlas"]
    assert "Iris — Greek: rainbow" in m.interaction_text(interaction)


def test_callback_payloads_fit_telegram_limit():
    m = _load_interactions()
    interaction = m.normalize_interaction({
        "type": "ask_user",
        "question": "Pick one",
        "options": [{"label": f"Option {i}", "description": "x" * 200} for i in range(6)],
    })
    markup = m.interaction_keyboard(interaction)
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert len(buttons) == 6
    assert all(len(b.callback_data.encode("utf-8")) <= 64 for b in buttons)
    assert m.decode_callback(buttons[0].callback_data) == (interaction.interaction_id, 0)


def test_free_text_ask_user_has_no_keyboard():
    m = _load_interactions()
    interaction = m.normalize_interaction({
        "type": "ask_user",
        "question": "What should I call the project?",
    })
    assert interaction is not None
    assert m.interaction_keyboard(interaction) is None


def test_approval_event_becomes_approve_deny_choice():
    m = _load_interactions()
    interaction = m.normalize_interaction({
        "type": "approval_required",
        "question": "Delete the event?",
    })
    assert interaction.kind == "approval"
    assert [o.label for o in interaction.options] == ["Approve", "Deny"]


def test_bridge_does_not_duplicate_ask_user_question_into_final_text():
    source = BOT.read_text(encoding="utf-8")
    assert "full_response.append(question.strip())" not in source
    assert "interaction = parsed" in source
    assert "CallbackQueryHandler(interaction_callback" in source

def test_live_sse_data_wrapper_preserves_ask_user_options():
    m = _load_interactions()
    interaction = m.normalize_interaction({
        "type": "ask_user",
        "data": {
            "question": "Just a demo! Which of these would you pick?",
            "options": [
                {"label": "Orange", "description": "Your favorite color, obviously."},
                {"label": "Cheese", "description": "A Belgian classic."},
                {"label": "Cat", "description": "The best companion."},
                {"label": "Surprise me", "description": "Let Atlas pick for you."},
            ],
            "multi": False,
        },
    })

    assert interaction is not None
    assert interaction.question == "Just a demo! Which of these would you pick?"
    assert [o.label for o in interaction.options] == [
        "Orange",
        "Cheese",
        "Cat",
        "Surprise me",
    ]
    assert "Cheese — A Belgian classic." in m.interaction_text(interaction)

    markup = m.interaction_keyboard(interaction)
    assert markup is not None
    assert len([button for row in markup.inline_keyboard for button in row]) == 4

def test_live_tool_approval_uses_explicit_approve_deny_buttons():
    m = _load_interactions()

    interaction = m.normalize_interaction({
        "type": "ask_user",
        "data": {
            "kind": "tool_approval",
            "approval_id": "approval-123",
            "question": "Allow this tool to read private Fastmail data?",
        },
    })

    assert interaction is not None
    assert interaction.kind == "approval"
    assert interaction.interaction_id == "approval-123"
    assert interaction.question == "Allow this tool to read private Fastmail data?"
    assert [o.label for o in interaction.options] == ["Approve", "Deny"]

    markup = m.interaction_keyboard(interaction)
    assert markup is not None

    buttons = [
        button
        for row in markup.inline_keyboard
        for button in row
    ]

    assert [button.text for button in buttons] == ["Approve", "Deny"]
    assert all(
        len(button.callback_data.encode("utf-8")) <= 64
        for button in buttons
    )

