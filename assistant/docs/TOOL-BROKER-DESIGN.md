# Tool broker design for our Odysseus fork

## Why replace the current selector incrementally

Current Odysseus tool selection combines semantic top-K retrieval with hard-coded
keyword force-includes, deterministic domain seeding, special follow-up regexes,
route/provider-specific schema pruning, and MCP keyword gates.  Each layer tries
to repair a failure mode from the layer before it.  It reduces prompt size, but
capability availability can become dependent on phrasing, language, embedding
health, or provider heuristics.

Our replacement keeps the useful idea — do not dump every schema into every
small local model prompt — but changes the authority model.

## Proposed flow

```text
user turn / active UI state / prior tool use
                 |
                 v
        typed conversation state
                 |
                 v
        capability suggestions  <---- semantic ranking (advisory)
                 |
                 v
controller permission filter  ---- HARD AUTHORITY ----
                 |
                 v
            ToolBroker
      core + sticky + suggested
                 |
                 v
       visible tool schemas
                 |
                 v
               Qwen
                 |
          structured tool call
                 |
                 v
controller executes / approves / denies
```

## Core tools

Keep a very small recovery set visible on every agent turn, subject to normal
permissions.  Candidates:

- `discover_tools` / `request_capability`
- `ask_user`
- `update_plan`
- memory recall/proposal entry point where appropriate
- native reminder creation once integrated

Core-visible does **not** mean unrestricted.

## Sticky capabilities

A successful tool use or explicit controller state activates a capability for
the conversational task. Example:

```text
"What's on my agenda tomorrow?" -> calendar.read activated
"Move the first one to 10."      -> calendar read/write remain candidates
"How is Immich doing?"           -> topic changes; homelab capability activates
```

We should expire/clear sticky state on a clear topic boundary, new session, or
explicit reset — not by failing to see a repeated keyword.

## Discovery fallback

If the selected set is insufficient, Qwen gets a cheap discovery tool. It asks
for a capability description; Python returns only tools already allowed for that
user/agent. Discovery never grants permissions.

## Provider contract

Each endpoint/model route should expose explicit capability metadata:

- native structured tool calls supported?
- parallel tool calls supported?
- reasoning/thinking channel behavior?
- correct way to disable thinking for tool rounds?
- streaming tool deltas supported?
- tool-result role/format?
- fallback model permitted/pinned?

A one-time runtime capability probe may populate/cache this metadata, but a 404
from an unrelated endpoint must not silently disable tools.

## Tool-round invariant

Once a run starts, subsequent rounds inherit the selected/active capability set
unless the controller deliberately changes it. Tool results should not cause a
fresh keyword-only reroute.

## Final-answer normalization

Provider adapters should normalize `content`, reasoning/thinking fields and tool
call deltas into one internal response structure. Telegram/UI should not need to
know Ollama/Qwen quirks. A tool-complete run must either yield a visible final
answer, an explicit error, or a controller event — never silently end with empty
content.

## Future sub-agents

The broker accepts an execution context containing `agent_id` and
`delegated_by`. A future homelab agent can receive a different permission pool
than the main agent without changing tool implementations. No sub-agent runtime
is implemented in v0.2.0.
