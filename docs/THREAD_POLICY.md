# Thread Policy

## Principle

Supervisor V2 separates:

- session continuity
- Codex thread continuity
- ChatGPT context continuity

ChatGPT continuity is carried by the kernel through persisted state and artifacts, not by depending on one visible browser chat.

## Session Rules

Per session:

- at most one active turn
- at most one active worker
- the next turn may start only after the previous turn is validated and committed

Session states:

- `manual_bootstrap`
- `running`
- `paused`
- `blocked_human`
- `stopped`
- `completed`

## Codex Thread Modes

Supervisor V2 supports exactly two Codex thread modes.

### `resume_current`

Default when a valid current thread already exists and there is no hard reason to reset context.

Rules:

- requires `current_codex_thread_id`
- must not silently fall back to a fresh thread
- observed thread id must match the current recorded thread
- after a successful Codex turn, optional compaction may run only when explicitly enabled for the session/runtime

### `start_fresh`

Used when:

- no valid current thread exists
- the thread is not safely resumable
- the kernel explicitly chooses a fresh start
- recovery or context hygiene requires a reset

Rules:

- must produce a new observed thread id
- must not silently reuse the prior thread when a fresh run was requested
- after a successful Codex turn, the new observed thread becomes the current thread; optional compaction may run only when explicitly enabled

## Optional Codex Post-Turn Compaction

Supervisor V2 keeps Codex continuity in one recorded thread whenever ChatGPT chooses `resume_current`.

The public `core-safe` default path must remain terminal-first and must not require the Codex `app-server`. Operators who want post-turn compaction can enable it by using the `macos-app`-gated `allow_app` Codex execution mode or by setting `BRIDGE_V2_CODEX_AUTO_COMPACT=1`.

When enabled, the Codex worker runs native Codex `app-server` compaction after a successful turn:

- resume the observed thread with `thread/resume`
- request `thread/compact/start`
- wait for `thread/compacted` or the matching completed compact turn
- write `codex_compaction` into the worker result artifact

This is a native Codex runtime step, not a browser/control-panel or visible Codex.app UI path. If compaction is explicitly enabled and then fails, the worker fails closed so an uncompacted result cannot be misreported as compacted.

## ChatGPT Continuity

Supervisor V2 does not require a visible same-chat browser conversation.

The ChatGPT worker receives explicit context:

- `session_id`
- `repo_path`
- `workspace_path`
- `operator_goal`
- `session_summary`
- last committed Codex result
- recent artifact manifest
- optional operator notes

This keeps continuity local, explicit, and recoverable.

## Human Intervention Rules

### Operator interjection during active Codex

The bound ChatGPT chat is the orchestrator surface, so an operator may need to
add a manual note there. During an active Codex run this is hazardous because it
can move ChatGPT to a newer assistant turn before the pending Codex return packet
has been delivered.

If that happens, fail closed but preserve progress:

- do not discard, summarize, or replace the undelivered return packet
- do not start a new Codex turn from the shifted assistant turn
- treat the exact `return_packet_id` as the idempotency key
- verify the bound chat URL still matches the session
- render or recover the preserved packet from the latest run artifacts
- deliver that exact packet once through explicit operator recovery
- normalize the session and run report to delivered only after the packet id is
  visible or a delivery attempt records `status=delivered`

The safer operator pattern is to avoid manual messages in the bound ChatGPT chat
while Codex is running unless the message is intended as an intervention. For
side notes, use a separate chat or wait until the return packet is delivered.

### `paused`

The session is intentionally paused and may resume automatically after an explicit `session resume`.

Pause is drain-first while an active turn is in progress. If Codex is running,
ChatGPT is generating, or a return packet is being posted, a pause request must
record the operator intent and let the current turn complete before transitioning
to `paused`. It must not terminate `codex exec`, cancel a ChatGPT generation, or
discard a pending return packet. Use an explicit emergency/repair action, not
normal pause, when the operator truly needs to interrupt a live process.

### `blocked_human`

Automatic continuation is unsafe.

To continue safely, the operator must explicitly re-enter manual control, for example by queuing a new bootstrap turn.

### `stopped`

The operator stopped the session. No further automatic progress should happen.

Stop is also drain-first during an active turn. Normal stop behaves like
`stop after cycle`: finish the current Codex/ChatGPT/return-packet boundary,
then transition to `stopped`/`completed`. A stop request must not kill the live
Codex process merely because the control panel state changed.

### `completed`

The autonomous session ended cleanly. No further work is expected unless the operator intentionally starts a new manual/bootstrap cycle.

## Recovery Rules

After a crash or restart:

- recover from persisted state only
- treat expired leases as stale
- reconcile from artifacts when possible
- fail closed when result state is ambiguous
- never auto-advance from an uncommitted turn

## Legacy Note

Older v1 terms such as `same_thread`, `fork_thread`, `new_thread`, and `bridge-control` are legacy concepts only.

They are not part of the V2 default policy.
