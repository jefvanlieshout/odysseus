# Jarvis / Odysseus Project Handoff

> **Purpose:** Canonical handoff for future ChatGPT sessions, collaborators, and Atlas itself.
> Read this before making project changes. Keep it updated when architecture, conventions, safety rules, or major milestones change.

## 1. Project identity

- **Overall project:** Jarvis
- **Application/platform:** Odysseus
- **Assistant/persona:** Atlas
- **Local reasoning/memory subsystem:** Brain

Historical/internal names may still exist, such as `jarvis-brain`, `JARVIS_BRAIN_API_KEY`, `gwen@pve`, or old filenames. Do not rename them blindly if doing so risks compatibility.

### Naming rule

User-facing references should use **Atlas**.

Internal identifiers should be renamed only when:
1. the rename improves clarity,
2. compatibility impact is understood,
3. migrations/config changes are handled,
4. tests cover the change.

Avoid global search/replace across the repository.

## 2. Core architectural principle

> **Atlas/Qwen reasons and talks; Python/controller is authority on what actually happened.**

Atlas must never claim that a tool ran, a file changed, a memory was saved, an event was created, an approval was accepted, or remote state changed unless the controller/tool result confirms it.

The model may propose actions. Python/controller decides whether they are allowed and records the authoritative outcome.

## 3. Development philosophy

### Prefer Linux fixes over Windows fixes

When debugging:

- Find the **root cause**.
- Fix the layer that is actually wrong.
- Avoid accumulating special cases, deny-lists, retries, or defensive wrappers as the primary solution.
- A guard is acceptable as **defense-in-depth** after the root cause is fixed.
- Do not hide a bug merely because the symptom disappears.

Example:

Bad:
```text
web search produced "we"
→ blacklist "we"
```

Good:
```text
trace logs
→ discover legacy prefetch treated string "false" as truthy
→ normalize toggle semantics and disable legacy prefetch in agent mode
```

### Iteration style

For low-risk changes:
- make useful, reasonably sized increments,
- use targeted tests,
- move quickly.

Use heavier safety/audit gates for:
- destructive operations,
- schema migrations,
- persistent-state changes,
- authentication/security changes,
- secrets,
- irreversible actions.

### Git hygiene

- Stage only files that actually changed.
- Avoid `git add -A`.
- Keep commits coherent and descriptive.
- Do not commit secrets, API keys, tokens, generated voice references, or private runtime state.

## 4. User / environment conventions

Primary development host:
- Linux / CachyOS
- Fish shell

Main repository:
```text
~/odysseus/odysseus
```

Current development branch:
```text
feature/v0.4.2-brain-control
```

Fork:
```text
git@github.com:jefvanlieshout/odysseus.git
```

Upstream:
```text
https://github.com/odysseus-dev/odysseus.git
```

Temporary helper/download files should go under:
```text
/home/jef/Downloads/old assistant stuff/
```

Quote that path because it contains spaces.

## 5. Security rules

Never ask the user to paste or print secrets.

Known sensitive values include:
- Brain API key
- Proxmox token secret
- Telegram bot token
- telemetry bearer token
- KuCoin credentials
- Odysseus access tokens

### Telegram logging

Telegram Bot API URLs contain the bot token.

Keep:
```python
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
```

Do not restore INFO-level HTTP logging in the Telegram bridge without ensuring tokens are redacted.

## 6. Brain runtime

Local Qwen runs through llama.cpp.

There is no separate Qwen container.

Brain worker runs alongside Odysseus and historically uses:
```text
jarvis-brain
jarvis-brain-worker
JARVIS_BRAIN_API_KEY
```

Treat these as compatibility-sensitive internal infrastructure unless a deliberate migration is planned.

Brain URL:
```text
http://jarvis-brain:8765
```

Brain API key file:
```text
~/.local/state/odysseus-brain-shadow/api-key
```

Never print its contents.

Brain worker networking:
```text
network_mode: service:odysseus
```

### Rebuild Odysseus with Brain overlay

Fish-compatible:

```fish
cd ~/odysseus/odysseus

set brain_key (string trim (cat ~/.local/state/odysseus-brain-shadow/api-key))

env JARVIS_BRAIN_API_KEY="$brain_key" docker compose     -f docker-compose.yml     -f docker/gpu.nvidia.yml     -f docker-compose.brain-autonomous.yml     build odysseus

env JARVIS_BRAIN_API_KEY="$brain_key" docker compose     -f docker-compose.yml     -f docker/gpu.nvidia.yml     -f docker-compose.brain-autonomous.yml     up -d --no-deps --force-recreate odysseus

env JARVIS_BRAIN_API_KEY="$brain_key" docker compose     -f docker-compose.yml     -f docker/gpu.nvidia.yml     -f docker-compose.brain-autonomous.yml     up -d --no-deps --force-recreate jarvis-brain-worker

set -e brain_key
```

## 7. Completed milestones

### v0.4.3 — Read-only self inspection

Added `inspect_code`.

Capabilities:
- status
- tree
- search
- read
- diff

Safety:
- read-only
- confined paths
- blocks runtime secrets
- blocks path escapes
- blocks symlinks
- blocks binaries / oversized files

Host repository is exposed read-only at:
```text
/workspace/odysseus
```

Runtime application source:
```text
/app
```

### v0.4.4 — Brain memory viewer + Proxmox read-only

Proxmox:
```text
host: 192.168.1.247
node: pve-jnode0
token identity: gwen@pve
role: PVEAuditor
```

The token identity still contains the historical name `gwen`. Do not rename it casually; treat it as an external credential identity.

Read-only means read-only. Do not bypass approval/security policy merely because a tool cannot mutate state.

### v0.4.5 — Tool contracts

Read-only MCP tools may still require approval when capabilities include:
- `READ_PRIVATE`
- `NETWORK_EGRESS`

Do not bypass those gates.

### v0.4.6 — Observability

Added:
- Proxmox node/guest details
- metrics
- RRD history fix
- task/error visibility
- KuCoin read-only telemetry

Future watcher idea:
- guest down / recovered
- node unreachable
- metric thresholds
- bot stale
- service unhealthy

Python detects events; Atlas interprets and explains.

### v0.4.7 — Telegram local voice

Voice modes:
```text
/voice auto
/voice on
/voice off
/voice
```

Behavior:
- `auto`: typed → text only, voice → text + voice
- `on`: typed/voice → text + voice
- `off`: text only
- mode persists per Telegram chat
- TTS failure must never suppress text
- only final visible assistant response goes to TTS

TTS:
- Chatterbox Nano
- official ResembleAI source
- pinned commit: `5de7a54aa4e5e2baadb0182dde554908b48b85c2`
- CPU
- clean speech, no `[chuckle]`
- speed 1.0

Private voice reference:
```text
/home/jef/Downloads/old assistant stuff/gwen-new-voice-clone-reference.wav
```

Do not commit the WAV. Its filename is historical and may remain unchanged unless manually migrated.

Telegram audio path:
```text
WAV → OGG/Opus → Telegram sendVoice
```

Codec:
- 48 kHz
- mono
- libopus
- 48k VBR
- application=voip

TTS endpoint:
```text
http://chatterbox-nano-tts:8881/v1/audio/speech
```

Chatterbox container remains read-only.

Writable cache:
```text
NUMBA_CACHE_DIR=/tmp/numba-cache
```

tmpfs:
```text
/tmp:size=512m,mode=1777
```

### v0.4.8 — Interaction reliability

Completed / validated:
- Telegram `ask_user` structured payload support
- descriptions preserved
- inline buttons preserved
- free-text continuation
- nested SSE `event["data"]` handled
- approval rendering with explicit Approve / Deny
- Telegram neutral protocol dataclasses separated from python-telegram-bot transport
- callback payloads fit Telegram's 64-byte limit

Important real SSE shape:
```json
{
  "type": "ask_user",
  "data": {
    "question": "...",
    "options": [...]
  }
}
```

Telegram must normalize the nested `data` object.

Approval behavior must preserve controller semantics.
Never auto-approve.

## 8. Recent root-cause fix — random web searches

Symptom:
Normal conversational messages caused unrelated web results such as:
- dictionary definitions of “we”
- Wikipedia “We”
- WeTransfer
- WhatsApp Web

Examples:
- `🍊 Orange`
- `✅ Approve`
- “very nice!”
- unrelated approval / Proxmox discussion

### Root cause

This was **not Atlas choosing `web_search`**.

A legacy pre-agent automatic web-prefetch path ran before `[agent-intent]`.

The streaming endpoint receives form values as strings:
```python
use_web = form_data.get("use_web")
```

Therefore:
```python
bool("false") is True
```

The normal agent tool policy already normalized this correctly using explicit true-like semantics, but the legacy context-prefetch path used raw Python truthiness.

That caused:
```text
use_web="false"
→ legacy prefetch still ran
→ helper LLM asked to extract a search query
→ Qwen reasoning leaked into helper response
→ reasoning text was sent to SearXNG
→ garbage search results appeared
```

### Correct architecture

Plain chat:
```text
explicit use_web=true
→ legacy prefetch may run
```

Agent mode:
```text
legacy prefetch OFF
→ Tool Broker decides whether web is relevant
→ Atlas gets web_search/web_fetch only when appropriate
```

Fix:
- normalize web toggle with explicit `tool_toggle_enabled`
- legacy prefetch only in plain chat mode
- defense-in-depth in `ChatProcessor` so agent mode cannot invoke legacy automatic search

Do **not** solve this with a blacklist for words such as `we`, `I`, `name`, etc.

## 9. Telegram interaction architecture

Keep protocol and transport separated.

Protocol layer:
```text
assistant/telegram/interactions.py
```

Should remain pure Python and must not depend on python-telegram-bot.

Important neutral types:
```python
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
```

Telegram transport conversion belongs in:
```text
assistant/telegram/bot.py
```

Callback format:
```text
odyask:<interaction_id>:<index>
```

Must remain <= 64 bytes.

## 10. Approval semantics

Approval is a controller/security concern, not a UI trick.

Flow:
```text
Atlas requests action
→ controller determines approval requirement
→ UI/Telegram renders Approve / Deny
→ user explicitly chooses
→ controller processes decision
→ action may proceed
```

Rules:
- never auto-approve
- never infer approval from casual wording
- do not bypass `READ_PRIVATE`
- do not bypass `NETWORK_EGRESS`
- do not bypass destructive-operation approval
- do not bypass memory-write approval
- do not claim success until controller confirms it

## 11. Memory authority

Memory is authoritative only when the memory/controller layer confirms persistence.

Atlas may say:
```text
“I can save that.”
“I’ve proposed this memory.”
```

Atlas must not say:
```text
“I saved that.”
```

unless a confirmed tool/controller result proves the write succeeded.

This invariant should eventually be tested explicitly.

## 12. Current v0.4.8 remaining work

Likely remaining:
1. Memory-authority messaging / result integrity
2. Stream/final-response deduplication if still reproducible
3. Broader interaction reliability tests
4. Commit and document v0.4.8

For duplicate responses:
- reproduce first
- inspect agent output/SSE/message IDs
- determine whether duplication originates in:
  - agent loop
  - SSE transport
  - message persistence
  - frontend rendering
  - Telegram adapter
- do not patch Telegram unless Telegram is proven to be the source

### Live validation — 2026-09-07

Validated against the rebuilt running stack:

- Atlas correctly identifies itself as Atlas.
- `Atlas, inspect your own source code...` routes through `inspect_code`.
- `inspect_code` correctly reported branch `feature/v0.4.2-brain-control`.
- Normal conversational input such as `🍊 Orange` no longer triggers legacy automatic web search.
- No `Starting comprehensive search` occurred before `[agent-intent]` for the tested conversational turns.
- Relevant v0.4.6-v0.4.8 regression batch passed: **40 tests passed**.
- Telegram bridge and Odysseus were rebuilt successfully after the changes.

Observed future optimization:

Low-signal conversational turns still run tool-RAG and may retrieve semantically noisy candidate tools. Atlas correctly ignored them during live testing and made zero tool calls, so this is not currently a correctness bug. Investigate later as a prompt/tool-selection efficiency improvement rather than mixing it into the web-prefetch fix.

Atlas user-facing identity rename is complete. Historical compatibility identifiers such as `gwen@pve`, `gwen-control-*`, and the existing private voice-reference filename remain intentionally unchanged.

## 13. Atlas rename plan

Do not global-replace `Gwen`.

### Category A — Change now

User-facing:
- prompts
- personality text
- UI labels
- README wording
- Telegram welcome/help text
- assistant-facing examples
- documentation
- tests that assert visible assistant name

### Category B — Inspect before changing

Potentially compatibility-sensitive:
- environment variable names
- container/service names
- database fields
- migration identifiers
- API schemas
- serialized state keys
- telemetry field names
- configuration keys
- external credential identities
- filenames containing `gwen`

Examples likely to remain historical for now:
```text
jarvis-brain
JARVIS_BRAIN_API_KEY
gwen@pve
gwen-new-voice-clone-reference.wav
```

### Inventory command

Run from repository root:

```fish
rg -n -i     --hidden     --glob '!.git/**'     --glob '!data/**'     --glob '!*.db'     '\bgwen\b|gwen[-_]|[-_]gwen\b'
```

Review each hit before editing.

## 14. Testing philosophy

Prefer focused regression tests for bugs that actually occurred.

A good regression test should encode:
1. the real production input shape,
2. the bad behavior,
3. the intended invariant.

Examples:
- nested SSE `ask_user.data`
- Telegram callback <=64 bytes
- string `"false"` must not enable web prefetch
- agent mode must never run legacy automatic web prefetch

After focused tests pass, run adjacent suites.

Do not rely solely on mocks if a live behavior can cheaply be verified.

## 15. Useful runtime checks

### Telegram bridge

From:
```text
~/odysseus/odysseus/assistant/telegram
```

Rebuild only Telegram:
```fish
docker compose build telegram-bridge
docker compose up -d --no-deps --force-recreate telegram-bridge
docker compose logs --tail=60 telegram-bridge
```

### Chatterbox health from bridge

```fish
docker exec odysseus-telegram-bridge python -c '
import httpx
r = httpx.get("http://chatterbox-nano-tts:8881/health", timeout=10)
print(r.status_code)
print(r.json())
'
```

### Watch interesting Odysseus agent/web logs

```fish
docker logs -f odysseus-odysseus-1 2>&1     | rg --line-buffered     'Starting comprehensive search|agent-intent|tool-rag|web_search|Agent round'
```

## 16. How to start a new chat

At the beginning of a new ChatGPT session:

1. Upload or paste this file.
2. Say:

```text
This is the current Jarvis/Odysseus project handoff.
Read it first and treat it as the project baseline.
We are continuing from the current branch and current working tree.
Do not assume the remote GitHub branch contains uncommitted local changes.
```

3. If there are uncommitted changes, also provide:

```fish
cd ~/odysseus/odysseus
git status --short
git diff --stat
git diff --check
```

If the next task depends on exact local code, provide the relevant `rg`, `sed`, `git diff`, or test output rather than relying on the remote fork.

## 17. End-of-session handoff checklist

Before ending a substantial development session, update this file with:
- current milestone/version
- what changed
- files changed
- tests run and result
- known regressions
- unresolved design decisions
- next recommended step
- important runtime commands
- new security invariants
- new compatibility constraints

Then:
```fish
git status --short
git diff --check
```

Commit the handoff update together with the relevant feature when practical.

## 18. Project direction

Long-term goals include:
- persistent day-to-day assistant
- long-term and episodic memory
- memory consolidation
- provenance-aware retrieval
- safe tool use
- homelab/server assistance
- calendar/email
- Telegram/Discord interfaces
- monitoring and proactive notifications
- agents
- safe autonomous actions

The system should become more capable **without transferring authority away from deterministic controller code**.

Atlas may become smarter and more autonomous in reasoning.

Python remains final authority for:
- what tools exist,
- what actions are permitted,
- what actually executed,
- what state changed,
- what memory was persisted,
- what requires approval.

## 19. Golden rules

1. **Atlas reasons; Python decides what happened.**
2. **Fix root causes, not symptoms.**
3. **Prefer Linux-native fixes over workaround piles.**
4. **Read-only means read-only.**
5. **Never expose secrets.**
6. **Never auto-approve sensitive actions.**
7. **Tests should reproduce real failures.**
8. **Do not blindly rename compatibility-sensitive identifiers.**
9. **Do not trust remote GitHub to contain local uncommitted work.**
10. **Keep this handoff current.**
