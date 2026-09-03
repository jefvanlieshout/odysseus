# Odysseus fork architecture audit — 2026-09-03

This is a broad static architecture/code-path audit of the current public
Odysseus `dev` sources and current public issue reports, cross-checked against
our Telegram + Brain + notification integration. It is **not** a formal security
audit and does not claim that every line of the repository was manually reviewed.

In our project shorthand:

- **Linux fix** = solve the real ownership/state/interface problem with explicit contracts.
- **Windows fix** = add another phrase/regex/special case around a brittle behavior.

The labels below describe architecture style, not operating-system support.

## Executive result

| Area | Result | Direction |
|---|---|---|
| Tool execution / authorization | Strong | Keep and extend |
| Tool discovery / visibility | Brittle | Replace incrementally with ToolBroker |
| Conversation tool continuity | Workaround-heavy | Typed sticky capability state |
| Ollama/Qwen provider adaptation | Brittle | Explicit provider capability contract |
| MCP | Useful plugin boundary | Keep for external plugins; do not depend on it for core controller capabilities |
| Model -> Python authority boundary | Mostly good | Make it universal |
| RAG/tool-index dependency | Too coupled | Embeddings rank tools; they must not decide whether capability exists |
| Final tool-round response | Provider-specific failure modes | Normalize inside provider/agent layer |
| File/RAG traversal | Improving upstream | Keep central prune policy |
| Brain integration | Correct pre-fork patch model | Fold patches into our owned fork over time |
| Telegram bridge | Good gateway, defensive fallbacks | Keep thin; remove need for upstream-response workarounds |
| Upstream maintenance | Manageable | Stable `upstream/main`, isolated sync branch, tests before promotion |

## Findings

### F-01 — Tool visibility is a stack of repair heuristics — HIGH

Current `src/tool_index.py` uses top-K semantic retrieval but then layers large
English keyword maps and force-includes on top. `src/agent_loop.py` adds domain
classification, low-signal handling, recent-context concatenation, special
notes/calendar follow-up regexes, forced tool sets and route-specific schema
pruning.

This is understandable historically, but it means a capability can disappear
because of wording rather than permission or actual task state.

**Our fix:** retain semantic ranking as advisory, replace availability with a
typed ToolBroker + explicit conversation capability state + discovery fallback.

### F-02 — Follow-up continuity is reconstructed from recent text — HIGH

Odysseus now concatenates a few recent user turns to tool retrieval specifically
to stop follow-ups from losing e.g. calendar tools. This is a pragmatic patch,
but the real state is "calendar capability is active in this task", not "one of
the previous 600 characters contained calendar-ish text".

**Our fix:** sticky capability state activated by actual intent/tool use and
cleared by explicit task/session transitions.

### F-03 — English regex/keywords are execution-critical in several paths — HIGH

Keyword hints, low-signal classification and the intent-without-action supervisor
are strongly English-oriented. Public issues show non-English requests/follow-ups
can lose tools or end agent runs early.

**Our fix:** no language-specific text gate may be the sole availability or
loop-continuation mechanism. Structured model/tool state wins; semantic intent
can suggest, never authorize/erase capability.

### F-04 — Tool index/embedding health can affect capability availability — HIGH

Tool RAG is useful for prompt size, but historical/current reports show cold,
slow or absent embedding services can collapse the visible tool set. A ranking
service should not become a capability dependency.

**Our fix:** core/sticky/explicit-context tools are deterministic. Embeddings only
rank extra permitted candidates.

### F-05 — Native tool support is too provider-heuristic-sensitive — CRITICAL for us

Qwen via Ollama can work very well with native structured tool calls, while
fallback text parsing is much less reliable. Public reports show endpoint
`supports_tools` state and provider probes can leave tools selected but send
`tools=0`, or force text parsing that misses Qwen tool formats.

**Our fix:** explicit provider capability metadata + optional one-time probe;
prefer native structured calling. Text/fenced parsing becomes an explicitly
selected compatibility adapter, not the default guess.

### F-06 — Qwen/Ollama thinking can swallow the visible post-tool answer — CRITICAL for us

Current reports reproduce the exact failure we saw through Telegram: tool call
finishes, second model round puts the answer in a reasoning field and leaves
normal content empty on Ollama's OpenAI-compatible endpoint.

**Our fix:** provider-level response normalization and explicit thinking-control
contract for tool rounds. Do not patch Telegram to guess from tool output.

### F-07 — MCP schemas can be discovered/configured but absent on an active turn — HIGH

Public reports show MCP tools can appear connected while Tool-RAG/provider route
filtering prevents schemas from reaching the model. Some non-API/model paths also
gate MCP schemas based on words in the current message.

**Our fix:** MCP remains a plugin transport, but its tools enter the same typed
registry/broker as built-ins. Registration and permission are separate from
visibility; active capabilities remain sticky.

### F-08 — Intent-without-action regex is a compatibility shim — MEDIUM

The loop has a supervisor that detects English phrases like "let me check..."
when a model narrates an action without calling a tool, then nudges it. Useful as
telemetry/last-resort compatibility, but it should not be central control logic.

**Our fix:** keep a bounded loop-breaker, but rely on native tool calls, explicit
step state and provider capability correctness first.

### F-09 — Top-K alone can crowd out required tools — MEDIUM

A fixed small K is attractive for local models but can omit a critical tool when
many semantically similar tools compete.

**Our fix:** reserve deterministic slots for core, active/sticky and explicit
context capabilities; semantic ranking fills the remaining budget. Discovery is
the recovery path.

### F-10 — Generic integration escape hatches need capability labels — MEDIUM

Generic API/MCP/app tools are valuable, but they can make the model search for
workarounds when a named tool is missing and can blur policy intent.

**Our fix:** keep generic integrations privileged and capability-scoped. Named
controller tools are preferred for core assistant behavior.

### F-11 — Tool registry/schema/dispatcher drift is possible — MEDIUM

Public reports show examples where a tool is advertised/indexed but not actually
dispatched. Multiple parallel registries are a consistency risk.

**Our fix:** move toward one canonical ToolDescriptor/registry from which prompt
metadata, schemas, policy metadata and dispatch registration are derived/tested.

### F-12 — Final-response fallback belongs upstream of gateways — HIGH

Our Telegram bridge correctly logs tool events and defensively returns the last
tool output / an explicit no-text message when Odysseus returns nothing. That is
good defensive gateway behavior, but fixing the final answer there would hide
the provider/agent-loop defect.

**Our fix:** keep Telegram thin. Normalize model rounds in the fork.

### F-13 — Brain's source patch installer becomes unnecessary after the fork — MEDIUM

The Brain integration deliberately used a fail-closed anchored source patcher
before we owned Odysseus. That was the right pre-fork strategy. Once our fork is
the source of truth, repeatedly patching our own source at install time becomes
extra indirection.

**Our fix:** gradually fold the Brain adapter seams into normal fork source while
keeping Brain itself a separate authoritative service. Retain reversible upstream
comparison/tests rather than runtime patching.

### F-14 — Security/authority controls are worth preserving — STRONG

Odysseus has explicit owner/admin restrictions, disabled-tool checks, approval
paths and safeguards around shell/code-execution surfaces. Its security guidance
also treats shell, model serving, MCP, email, calendar, API and similar tools as
privileged. These are principled controller-side controls.

**Our action:** preserve and extend these checks. The ToolBroker must never become
an authorization engine.

### F-15 — Untrusted tool-result boundary is correct — STRONG

Tool results are treated as untrusted context before returning to the model.
This is the correct direction for prompt-injection resilience.

**Our action:** preserve; add provenance/correlation IDs in our execution context.

### F-16 — RAG directory pruning was a good root-cause upstream fix — STRONG

An earlier bug indexed `.git`, `.obsidian`, `node_modules`, etc. Current `index_walk.py`
centralizes hidden/junk directory pruning so vector and keyword indexes share one
policy. This is exactly the style we want: one source of truth rather than two
separate workarounds.

**Our action:** keep it; prefer shared policy modules elsewhere too.

## What we should NOT do

- Do not make every tool always visible just to avoid routing bugs.
- Do not solve calendar continuity by adding 40 more misspellings/keywords.
- Do not let `agent_id` grant permission.
- Do not let Qwen write directly to Proxmox/Telegram/SQLite.
- Do not make Chroma/embeddings a hard dependency for safe core capabilities.
- Do not fork 200 upstream files if a small hook + owned module is enough.
- Do not auto-merge/deploy upstream `dev` into production.

## What we should preserve

- Python/controller authority over execution.
- Approval + permission checks around mutating/destructive tools.
- Tool-output untrusted-context treatment.
- Modular sidecar services (Brain, events, future Proxmox monitor).
- Good trace logs and explicit error events.
- Git history + safe updater/rollback.

## Implementation roadmap

### v0.2.0 — fork foundation (this update)

- establish safe `upstream`/`origin` Git workflow;
- add isolated upstream sync branch tooling;
- add ExecutionContext contract;
- add ToolBroker contract + tests;
- record this audit/design;
- **no live agent-loop behavior change yet**.

### v0.2.1 — provider/tool-round correctness

- explicit endpoint capability model for our active Ollama/Qwen route;
- prefer native structured tool calls;
- normalize post-tool reasoning/content behavior;
- make empty final answer an explicit agent error, not a gateway mystery;
- integrate reminders as a native controller capability instead of relying on MCP.

### v0.2.2 — ToolBroker production integration

- canonical descriptor registry;
- permission pool -> broker visibility;
- core recovery/discovery tool;
- sticky capabilities based on typed task state/tool use;
- semantic ranking only fills spare tool budget;
- preserve same tool context across tool rounds.

### v0.2.3 — Proxmox read-only connector

- native Proxmox HTTPS API token;
- no host daemon / no Qwen on Proxmox;
- verified state -> events independently of Qwen;
- read-only controller tools for Qwen diagnostics.

### Later

- conditional natural-language reminders tied to verified conditions;
- capability-scoped agent identities and eventual sub-agents;
- upstream generic fixes contributed back when appropriate.

## Upstream references reviewed

Public Odysseus paths/issues used during this audit include:

- `src/agent_loop.py`
- `src/tool_index.py`
- `src/tool_execution.py`
- `src/llm_core.py`
- `src/index_walk.py`
- `SECURITY.md`
- GitHub issues/discussions: #3668, #3794, #4048, #4663, #5187, #5190,
  #5192, #5466, #5503, #5557, #5559, #5707, #5824 / PR #5823,
  discussion #3280, discussion #4237.
