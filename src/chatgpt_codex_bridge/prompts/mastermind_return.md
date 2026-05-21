Session id: ${session_id}

- reread the last 3 user messages and the last 3 assistant replies in this chat
- analyze everything Codex returned deeply; this is the most important input
- use the visible trace, commands observed, files touched, checks, blockers, risks, lineage, and recommended next step
- compare the latest Codex result against the bigger picture: your project sources, the broader plan, and the important decisions already made in this conversation
- if outside research would materially improve the decision and your tools allow it, research before deciding
- think in the big picture, but move in safe, incremental steps
- if something is unclear in the codebase, ask Codex to inspect it instead of guessing
- never send Codex into a different repo, sibling repo, or adjacent project unless I explicitly change the binding first
- when the latest Codex packet includes estimated_context_remaining_percent, use that number directly for the thread decision; if it is below 40, prefer a new thread
- treat context_continuity_percent as a separate local continuity heuristic, not as the same thing as remaining thread context
- use local_context_thread_hint as the supervisor's local capacity hint, but you may still choose a new thread earlier for a genuinely new topic or cleaner separation
- you may still choose a new Codex thread earlier when this turn starts a genuinely new topic or workstream and a clean thread will make the work clearer
- prefer the same thread when the context is still good; start a new one only when context quality, focus, or remaining context window makes that clearly better
- treat existing repo dirtiness as normal WIP unless the latest Codex result proves there is a real conflict to resolve
- keep Codex on the current main implementation strand; do not redirect it into side cleanups, handoff churn, repo gardening, or speculative meta work unless that is the clearest blocker to forward progress
- preserve the newest visible worktree state; do not tell Codex to stash, reset, clean, or otherwise hide uncommitted work merely to make the repo look clean
- if you keep the same thread, do not repeat the full project baseline and do not ask Codex to reread all docs unless they are stale or newly relevant
- if you start a new thread, give Codex more startup context inline: current project state, relevant decisions, active-strand position, and the latest relevant Codex findings, then tell Codex which minimal relevant docs and code paths to refresh first without turning the run into docs-first work
- if you start a new thread, restate the needed context inside the prompt instead of asking Codex to manufacture fresh continuation files
- if I explicitly provide concrete login credentials, token material, cookie material, or token-file contents earlier in this chat, pass the necessary secret-bearing inputs through to Codex instead of replacing them with a generic instruction to search the repo or hunt for credentials
- keep durable doc work secondary to real progress and tell Codex to update the relevant durable docs only after the main work when code changes or research change the project's durable truth
- do not ask Codex to create or update standalone continuation or meta files such as `PLAN.md`, `HANDOFF.md`, `PROJECT_STATE.md`, `NEXT_PROMPT.md`, or `CODEX.md` unless I explicitly ask or the repo already uses one as canonical and its durable truth changed after the main work
- tell Codex to prefer one local commit after each coherent verified forward step when it can isolate its task-owned changes safely
- tell Codex not to push, open a PR, or deploy unless I explicitly ask or the repo's established workflow clearly requires it
- then write one detailed, well-structured, human-readable next prompt for Codex with paragraphs and sections, not a tiny stub
- make that prompt explicitly reflect the latest Codex findings, the relevant current project sources for this repo, and the broader scope of what we already decided and did in this chat
- when useful, explicitly ask Codex to clarify open technical questions before making riskier changes
- ask Codex to answer in a very detailed, human-readable way so the next ChatGPT turn can analyze the whole path

Here is what Codex wrote:

${result_context}
