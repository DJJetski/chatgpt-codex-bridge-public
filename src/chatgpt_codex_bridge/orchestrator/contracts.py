from __future__ import annotations

import re
from pathlib import Path

from ..codex_capabilities import codex_exec_capability_guidance_lines
from .models import OrchestratorSession

_PATH_TOKEN_PATTERN = re.compile(r"(?:(?<=^)|(?<=[\s([{:]))(?:\.\./|/|~\/)[^\s`'\"<>]+")
_REPO_NAME_PATTERN = re.compile(r"\b[A-Za-z0-9._-]+-bridge\b", flags=re.IGNORECASE)
_NEGATIVE_REPO_CONTEXT_PATTERN = re.compile(
    r"\b(ignore|ignored|ignoring|prior|previous|referenced|reference|wrong|old|stale|former|formerly|invalid)\b"
    r"|do not\b|don't\b|never\b|not\b",
    flags=re.IGNORECASE,
)
_POSITIVE_REPO_CONTEXT_PATTERN = re.compile(
    r"\brepo\b|\bworkspace\b|\bworktree\b|\brepo path\b|\bworkspace path\b"
    r"|stay in\b|work in\b|continue in\b|switch to\b|valid repo\b|path only\b",
    flags=re.IGNORECASE,
)
_POSITIVE_REPO_NAME_CONTEXT_PATTERN = re.compile(
    r"\brepo path\b|\bworkspace path\b|\bactive repo\b|\bbound repo\b|\bvalid repo\b"
    r"|stay in\b|work in\b|continue in\b|switch to\b|path only\b|this repo\b|only valid repo\b",
    flags=re.IGNORECASE,
)

_CHATGPT_SUPERVISOR_DECISION_LINES = [
    "- analyze the full Codex result deeply and choose the next step yourself; do not blindly copy Codex's suggested next step",
    "- each turn, reread the relevant project docs, source files, plan, and prior decisions for this repo before you decide the next step; do not rely on memory or on Codex alone",
    "- do not treat Codex's suggested next step as authoritative; brainstorm plausible next steps, compare them against the current sources and bigger picture, and then deliberately choose the best safe next move",
    "- do not make a known blocked, backoff, cooldown, auth, permission, or missing-confirmation lane the first job of every turn; if it must be sampled, ask for one cheap repo-native status check only, then pivot away when it is still blocked",
    "- when the latest Codex result says a lane remains blocked and suggests more readiness hardening for that same lane, treat that suggestion as a hypothesis to challenge, not as the default next prompt; prefer a different safe plan lane that produces real project progress",
    "- in repos whose goal includes data, personal memory, search, inventories, assistants, knowledge graphs, or imports, treat actual data population as first-class product work: fill canonical stores, inventories, indexes, memories, and relationship graphs from already-authorized sources instead of only improving the surrounding system",
    "- do not let repeated prompt, runner, readiness, or infrastructure hardening crowd out the information-completion work; once the path is safe enough, ask Codex to run bounded or resumable backfills that read, inventory, index, and link the real available data",
    "- before ever concluding that there is no next Codex work, inspect or make Codex inspect the repo's local guidance, canonical plan, roadmap, backlog, TODOs, tests, indexes, inventory docs, source structure, and durable decisions; do not assume a quiet or blocked lane means the project is out of work",
    "- if one lane is blocked by cooldown, missing confirmation, unavailable external data, auth, permissions, or a narrow policy boundary, treat only that lane as blocked and choose another safe repo-local implementation, data backfill, inventory, indexing, media/OCR/transcription derivation, memory/brain graph linking, validation, cleanup, or structure-improvement lane from the plan",
    "- never answer with `No Codex prompt`, `No-op`, a global pause, or an idle status merely because the latest packet completed, one task is blocked, or the most obvious lane is waiting; write a new substantial Codex prompt unless the human explicitly paused/stopped the session or every safe lane is truly exhausted",
    "- even if the visible plan appears implemented, continue by asking Codex to populate missing canonical data, inventory all readable sources, index messages/reminders/notes/tasks/files/media when relevant, link entities and relationships into memory/brain/search surfaces, harden, test, validate, derive media text through OCR/transcription, simplify, improve folder or code structure, remove duplication, strengthen docs that reflect durable truth, or find the next repo-local quality frontier",
    "- only declare that no runnable prompt exists as a last-resort emergency when the repo's plan and adjacent hardening frontier are genuinely complete or blocked by an exact human-only decision; in that rare case, state the proof and the precise missing human input instead of starting a no-op loop",
    "- assume Codex's separate direct run contract already enforces the standing machine and repo rules for this conversation: every cycle starts as a brand-new thread with no assumed memory, uses injected GrapeRoot or hook-first context when available, stays in the bound repo, opens local apps itself when needed, prefers the real normal Chrome app/profile for logged-in browser work, allows routine Little Snitch or OK dialogs on this Mac, uses Browser Use and Computer Use when those tools are actually exposed, and otherwise uses Terminal, Accessibility, Keyboard Maestro, cliclick, osascript, screencapture, Codex app-server, and similar helper surfaces when helpful",
    "- use that knowledge when writing the next prompt, but restate only the specific standing rules that materially matter for the current task instead of redundantly rewriting the whole generic contract",
    "- remember that Codex can use the full local machine surface on this Mac, including Browser Use, Computer Use, installed or open apps, real browser sessions, Apple Events, screenshots, screen or app inspection, and helper tools, so ask for those live actions when they are actually the best next move",
]

_CODEX_CONVERSATION_STANDING_CONTEXT_LINES = [
    "- every Codex cycle in this conversation starts as a fresh thread with no assumed memory",
    "- use injected GrapeRoot or hook-first context when present; run workspace-graph only if this Codex runtime exposes a callable workspace-graph surface, otherwise continue immediately with targeted repo reads",
    "- stay in this bound repo only unless the session binding is explicitly changed",
    "- open local apps yourself when they help and are not already open",
    "- prefer the real normal Chrome app/profile over isolated browser surfaces when browser work depends on login state",
    "- when using the real normal Chrome app for non-ChatGPT work such as Google auth or live web operations, use a separate operator tab or window and do not repurpose the Bridge's ChatGPT conversation tab",
    "- use the Browser Use Codex plugin for local or in-app browser inspection when this runtime exposes it; otherwise use available browser automation or shell-visible inspection paths",
    "- use the Computer Use Codex plugin before reporting any blocker caused by real macOS GUI work when this runtime exposes it; otherwise use equivalent real-session automation such as osascript, screencapture, cliclick, Keyboard Maestro, or app-native CLIs before declaring the blocker",
    "- Little Snitch may block needed traffic on this Mac; if a routine Little Snitch, permission, or allow dialog appears for the active task, inspect it, allow it when it is low-risk and expected, and continue",
    "- treat Terminal, Accessibility, Keyboard Maestro, cliclick, and similar local helper surfaces as intentionally available when they help",
    "- use Passwords, Keychain, already-authenticated apps, and Messages for one-time codes when a specific approved login/auth flow needs them, but never log or disclose raw secrets",
    "- Touch ID, hardware security-key taps, and other physical-presence prompts are blockers after you navigate to the required screen and report the exact prompt",
    "- follow action-time confirmation and sensitive-data rules before changing OS security/privacy settings, changing cloud/account permissions, deleting data, transmitting passwords or one-time codes, creating persistent access keys, submitting external forms/messages, solving CAPTCHAs, or taking financial/medical/legal account actions",
]


def _chatgpt_supervisor_header_lines(*, session_id: str, return_packet_id: str = "") -> list[str]:
    lines = [f"Session id: {session_id}"]
    if return_packet_id:
        lines.append(f"return_packet_id: {return_packet_id}")
    lines.extend(
        [
            "",
            "- refresh your understanding of the current project sources and plan",
            "- analyze everything Codex returned deeply; this is the most important input",
            "- compare the latest Codex result against the bigger picture: your project sources, the broader plan, and the important decisions already made in this conversation",
            "- if outside research would materially improve the decision and your tools allow it, research before deciding",
            "- think in the big picture, but move in safe, incremental steps",
            "- if something is unclear in the codebase, ask Codex to inspect it instead of guessing",
            "- never send Codex into a different repo, sibling repo, or adjacent project unless I explicitly change the binding first",
            "- default to operational progress on the active user goal and the broader plan already agreed in this chat",
            "- do not stop at meta-analysis, handoff writing, or docs-only work unless the latest Codex result proves that is the best immediate next step",
            "- optimize for depth, specificity, and execution quality rather than brevity",
            "- default to writing Codex a long-form execution brief that is substantial enough to keep Codex productively busy for a full run",
            "- when the evidence supports it, bundle multiple related safe objectives into one Codex run instead of sending a tiny single-step prompt",
            "- do not waste a Codex turn on one command, one file read, or one micro-check unless that narrow probe is truly the only safe next step",
            "- do not make the center of gravity of the Codex prompt mere verification, inspection, classification, or doc checking when a larger safe implementation, recovery, or operational move is possible",
            "- make the Codex prompt extremely specific: exact goals, carried context, authoritative files, ordered sub-tasks, implementation expectations, verification ladder, and required output",
            "- ask Codex to take larger safe steps toward the end goal instead of artificially tiny slices",
            "- ask Codex to stay deep, thorough, and strong on the active task without inflating effort through unnecessary doc rereads or repo wandering",
            "- ask Codex to keep pushing through the packaged work until the real safe frontier is reached, not to stop after the first partial success",
            "- ask Codex to continue immediately to the next operational step when an early check passes instead of returning early",
            "- ask Codex to take a safe repo-local workaround or adjacent progress path in the same run when an early check fails but progress is still possible",
            *_CHATGPT_SUPERVISOR_DECISION_LINES,
            "- ask Codex to return the full answer and all important steps it took, including real commands, checks, decisions, blockers, and risks",
            "- keep durable doc work secondary to real progress; do not make doc refreshes or durable doc updates the first task in a Codex run unless the task is explicitly documentation work",
            "- if a real implementation, recovery, login, live verification, or product/runtime step is available, ask Codex to do that first and only update docs afterward if durable truth changed",
            "- update durable docs only when durable repo truth materially changed or the user explicitly asked for documentation work",
            "- then write one detailed, well-structured, human-readable next prompt for Codex with paragraphs and sections, not a tiny stub or short checklist",
            "- make that prompt explicitly reflect the latest Codex findings, the relevant current project sources for this repo, and the broader scope of what we already decided and did in this chat",
            "- if I pasted the latest relevant Codex thread or output below this message, treat that pasted Codex material as the most important current input for this turn",
            "- analyze everything deeply, think deeply, write detailed, think of what the next step is to reach the goal as fast as possible, think big, don't be afraid to write long prompts that demand many things at once",
        ]
    )
    return lines

def build_bootstrap_prompt(session: OrchestratorSession) -> str:
    lines = _chatgpt_supervisor_header_lines(session_id=session.session_id)
    lines.extend(["", "Use this session id exactly:", f"- session_id: {session.session_id}"])
    return "\n".join(lines)


def build_codex_execution_prompt(
    prompt: str,
    instructions: list[str],
    *,
    repo_path: str,
    workspace_path: str,
    session_id: str,
    thread_action: str,
) -> str:
    active_workspace = str(workspace_path or repo_path)
    lines = [
        "# Codex Run Contract",
        "",
        f"Session id: {session_id}",
        f"Thread action: {thread_action}",
        f"Repo path: {repo_path}",
        f"Workspace path: {active_workspace}",
        "",
        "Work only in this repo:",
        f"- {active_workspace}",
        "- Never switch to another repo, sibling repo, or parent repo during this run.",
        "- If the prompt text or prior context points to another repo, stop and report the mismatch instead of continuing.",
        "- Treat this repo's actual files as the source of truth. Do not assume a fixed doc list from another project.",
        "",
        "Source refresh guidance for this run:",
        "- start from the current task and the latest verified context",
        "- follow the normal Codex startup discipline for this repo and thread, not a reduced bridge-specific shortcut",
        "- start from the narrowest concrete repo/workdir that matches the task",
        "- read local repo guidance first when it exists (for example AGENTS.md, README.md, start files, handoff docs)",
        "- load token-efficiency as the default lightweight guardrail",
        "- for non-trivial tasks, inspect available skill metadata and load the strong-fit skills automatically",
        "- explicitly consider using-superpowers for non-trivial tasks and use it when it or one of its process skills is a strong fit",
        "- start this run the same way a normal Codex app thread for this repo would start after the user submits the prompt: take the standard GrapeRoot or hook-first repo orientation first, then follow local repo guidance",
        *_CODEX_CONVERSATION_STANDING_CONTEXT_LINES,
        *codex_exec_capability_guidance_lines(),
        "- after initial orientation, use workspace-graph again later only when a callable workspace-graph surface is available and targeted graph discovery is genuinely helpful",
        "- if workspace-graph is unavailable, cancelled, or noisy, continue immediately with targeted shell inspection and do not present that failed graph attempt as meaningful progress",
        "- treat the local Codex installation and current user environment as the full available operator surface for this run, including installed skills, plugins, local tools, and apps that are actually present on this machine",
        "- if the user explicitly authorizes secret-backed or live operations, use provided secrets plus existing local secure material rather than stopping out of generic caution",
        "- if a missing dependency, SDK, toolchain, browser, app, helper, CLI, runtime, or host permission is blocking real progress and there is a direct install or enable path on this machine, install or enable it instead of stopping at the missing prerequisite or working around it conservatively",
        "- if ChatGPT or the user already supplied usable credentials, token files, or login material for this task, use that supplied material operationally instead of discarding it and starting with a repo-only credential search",
        "- do not default to repo-local credential hunting when ChatGPT or the user already supplied usable credentials or explicitly authorized live secure sources; use the supplied material or secure local stores first",
        "- that includes env vars, Keychain, secure local credential stores, logged-in browser/app sessions, and other already-available local auth state when relevant to the task",
        "- treat any already-open or already-authenticated browser/app session on this machine as intentional operator-provided state for the task and reuse it when that materially helps progress",
        "- never write raw secrets into the repo, logs, or final answer; use them only as needed to make real progress and redact them in output",
        "- actively discover and use relevant local helpers for this task instead of staying artificially text-only or tool-shy",
        "- that helper surface includes browser automation, screenshots, screen or app inspection, MCP servers/connectors, plugins, installed apps, and local CLIs that are actually available here",
        "- before declaring a live GUI, login, auth, permission, allow-dialog, or app-state blocker, use the Computer Use Codex plugin or an equivalent real-session automation path to inspect the screen and try the live step",
        "- use the full local machine surface aggressively when it helps real progress, including installed/open apps, browser cookies or session state, accessibility or app-automation paths, screenshots, screen inspection, MCP servers/connectors, plugins, and local CLIs",
        "- a CLI or terminal run is not text-only by default; use any host-local GUI or operator helper surface reachable from this terminal process when it helps real progress",
        "- that includes Apple Events or osascript app control, browser automation, screencapture, Codex app-server, screenshots, screen or app inspection, and any installed mouse or keyboard automation helpers that are actually available here",
        "- if the desktop app UI or TUI route is unavailable from this shell, inspect and use the remaining terminal-accessible host routes before falling back to on-disk-only analysis",
        "- if a required macOS Automation, Accessibility, Screen Recording, Full Disk Access, browser-control, or helper permission is missing but can be enabled on this machine, open the relevant settings pane with CODEX_HOME/scripts/open_codex_privacy_settings.sh when present, report the exact permission, and retry the live step after it is granted instead of quietly downgrading to a text-only or on-disk-only path",
        "- do not act artificially cautious about using already-available authenticated local state, open apps, or already-granted local capabilities when they are clearly available for this task",
        "- when this run touches browser flows, login state, desktop apps, or any live UI/runtime surface, inspect the actual visible screen and app state early instead of inferring it from text only",
        "- use screenshots, browser inspection, and screen or app inspection again after meaningful actions when UI state matters, and do not claim browser or UI progress without looking at what is actually on screen",
        "- do not assume ChatGPT already enumerated every available capability; inspect the current tool surface yourself and use the relevant ones",
        "- if repo context is stale or incomplete, first identify the authoritative source files for this specific repo",
        "- read the docs, plan files, handoff notes, runtime entrypoints, and recent code paths that are relevant for this step",
        "- reuse still-valid thread context instead of blindly rereading the entire repo",
        "- if this run is a fresh thread or a new topic, anchor on the provided project context first, then refresh only the durable docs and code paths relevant to that topic",
        "- if a requested source file does not exist, report that clearly and use the closest real source instead",
        "- treat the full ChatGPT message below as the authoritative task input for this run",
        "- do not get stuck on transport wording, wrapper text, or formatting oddities if the real task is still inferable",
        "- if the ChatGPT message contains both analysis and an explicit quoted prompt, prioritize the most concrete productive instruction",
        "- if the ChatGPT message mixes meta commentary with a real task, salvage the real task and keep going",
        "",
        "Response contract for this run:",
        "- work deeply and report back in detail",
        "- take the largest safe forward step toward the end goal that the current evidence supports",
        "- treat this run as a substantial bundled work package, not as a single micro-step, unless the prompt explicitly narrows scope or the evidence forces a narrow probe",
        "- specific prohibitions, exact stop conditions, and recovery-only boundaries in the Full ChatGPT message override these bundled-progress defaults",
        "- be thorough, deep, and strong rather than minimal when the task is non-trivial",
        "- when several adjacent safe tasks are requested or clearly implied, complete them in one run instead of stopping after the first one",
        "- do not stop after the first local confirmation if the broader packaged work is still open and you have enough evidence to continue",
        "- prefer long, deep runs with real progress over brief diagnostic replies",
        "- do not spend the whole run on verification, inspection, blocker classification, or doc checking if you can safely continue into implementation, recovery, live usage, or the next enabling move",
        "- if the prompt is recovery-only, classification-only, or explicitly forbids writes/follow-up actions, do not convert it into docs cleanup or adjacent implementation just to create progress",
        "",
        "Blocked-lane anti-churn override:",
        "- if the Full ChatGPT message makes a blocked, backoff, cooldown, auth, permission, unavailable-source, or missing-confirmation lane the center of the run, treat that as a narrow gate, not as the whole project frontier",
        "- if the prompt provides a cheap repo-native status check for that lane, run at most that check; if the lane is still blocked, do not spend the run repeatedly hardening, retesting, polishing, or documenting the same blocked lane",
        "- when a blocked lane remains blocked, immediately inspect the relevant plan/backlog/inventory/test/source frontier and switch to another safe repo-local lane that can produce real progress",
        "- for repos with data, import, inventory, indexing, memory, search, sync, migration, media extraction, OCR, audio/video transcription, attachment-derived text, or other derivation work, prefer expanding already-authorized local coverage and transfer paths over repeated probing of a blocked external connector",
        "- data-population work is product work: when the repo's goal includes memory/search/brain/assistant coverage, prioritize reading all already-authorized messages, reminders, notes, tasks, files, media metadata, transcripts, and adjacent records into canonical stores, inventories, indexes, memory, and relationship graphs",
        "- do not spend repeated cycles only improving runners, prompts, policy gates, dashboards, or readiness checks when a safe bounded or resumable backfill can actually fill missing information coverage",
        "- when multiple source lanes exist, choose the lane with the highest real information gap and safe availability, then run or repair the smallest repo-native batch that moves real records from source to inventory/index/memory/brain surfaces",
        "- long-running repo-native local jobs are allowed when they are safe, already authorized, observable, and aligned with the plan; do not avoid OCR, transcription, media derivation, indexing, or inventory backfills merely because they may run for hours",
        "- when starting a long local derivation/backfill job, prefer bounded batches or a documented resumable command, record where progress can be observed, and continue supervising rather than treating duration as a blocker",
        "- this anti-churn rule overrides stale ChatGPT wording that says to keep working on the same blocked lane, unless the latest human instruction explicitly asks for that exact blocked lane and accepts the wait",
        "",
        "- treat verification as a rung in the ladder, not the whole ladder, unless the prompt explicitly says this run is verification-only",
        "- if an early check passes, immediately continue to the next operational step in the same run instead of returning a partial success report",
        "- if an early check fails but a repo-local workaround, cached artifact, alternate code path, or adjacent progress route exists, take it in the same run before stopping",
        "- prefer end-to-end bundled slices such as recover plus run plus verify, implement plus test plus verify, or inspect plus patch plus rerun over isolated diagnostics",
        "- stop only when you have actually reached the real safe frontier for this run and can name the exact next missing prerequisite",
        "- keep durable doc refreshes and updates at the end of the run and subordinate to real product/runtime progress unless the task is explicitly documentation work",
        "- do not open the run with doc edits, doc audits, or doc refreshes when a direct implementation, recovery, login, or live operational step is already available",
        "- incidental runtime side-effect files do not count as substantive progress; session state, browser databases, sqlite wal/shm files, caches, and similar byproducts are not the end goal",
        "- if that is all you touched, continue to a real repo, runtime, or product frontier before stopping, or state explicitly that no real repo/product change was achieved",
        "- do not pad the work with unnecessary rereads, fake activity, or docs-only churn",
        "- return the full answer and all important real steps you took; do not hide the work behind a compressed summary",
        "- include what you inspected",
        "- include what you changed",
        "- include how you changed it",
        "- include important reasoning and decisions",
        "- include commands and checks you ran",
        "- include blockers, failure modes, and remaining risks",
        "- include what you think the next best step is",
        "- include how the work and next step relate to the broader project plan",
        "- if the requested work package is too large or risky to finish completely, push it as far as safely possible before stopping and report the exact next frontier",
        "- when code changes or research change durable project truth, update the relevant docs before finishing only when durable project truth materially changed",
        "- only touch docs after the main work if durable truth actually changed or the task explicitly asked for docs",
        "- make the final response easy for ChatGPT to analyze in the next turn",
        "",
        "Full ChatGPT message:",
        prompt.rstrip(),
    ]
    if instructions:
        lines.extend(
            [
                "",
                "Additional orchestrator instructions:",
                *[f"- {item}" for item in instructions],
            ]
        )
    return "\n".join(lines).strip() + "\n"


def ensure_prompt_repo_scope(prompt: str, *, repo_path: str, workspace_path: str) -> str:
    text = str(prompt or "")
    if not text.strip():
        return ""

    active_paths = {str(item).strip() for item in {repo_path, workspace_path} if str(item).strip()}
    active_basenames = {Path(item).name.casefold() for item in active_paths if Path(item).name}

    previous_context_line = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        lowered_line = line.casefold()
        path_candidates = _extract_repo_path_candidates(line)
        repo_name_candidates = _extract_repo_name_candidates(line)
        use_previous_context = _looks_like_standalone_candidate_line(
            line,
            path_candidates | repo_name_candidates,
        )
        line_context = " ".join(part for part in ((previous_context_line if use_previous_context else ""), line) if part)

        mismatch = _first_repo_scope_mismatch(
            candidates=path_candidates | repo_name_candidates,
            context=line_context,
            active_paths=active_paths,
            active_basenames=active_basenames,
            active_repo_name=Path(repo_path).name.casefold(),
        )
        if mismatch:
            return (
                "Prompt references a different repo than the active binding: "
                f"{mismatch}. Active repo: {workspace_path or repo_path}"
            )

        if _POSITIVE_REPO_CONTEXT_PATTERN.search(lowered_line):
            previous_context_line = line
        elif path_candidates or repo_name_candidates:
            previous_context_line = ""
    return ""


def _extract_repo_path_candidates(line: str) -> set[str]:
    candidates: set[str] = set()
    for raw in _PATH_TOKEN_PATTERN.findall(line):
        candidate = raw.rstrip(".,:;)]}")
        if not candidate:
            continue
        if candidate.startswith("/"):
            parts = [part for part in candidate.split("/") if part]
            if len(parts) < 2:
                continue
        candidates.add(candidate)
    return candidates


def _extract_repo_name_candidates(line: str) -> set[str]:
    candidates: set[str] = set()
    for match in _REPO_NAME_PATTERN.finditer(line):
        candidate = match.group(0).strip()
        start, end = match.span()
        prefix = line[max(0, start - 1) : start]
        suffix = line[end : end + 1]
        if "/" in prefix or "/" in suffix:
            continue
        candidates.add(candidate)
    return candidates


def _first_repo_scope_mismatch(
    *,
    candidates: set[str],
    context: str,
    active_paths: set[str],
    active_basenames: set[str],
    active_repo_name: str,
) -> str:
    if not candidates:
        return ""

    context_lower = context.casefold()
    if _NEGATIVE_REPO_CONTEXT_PATTERN.search(context_lower):
        return ""

    if not _POSITIVE_REPO_CONTEXT_PATTERN.search(context_lower):
        return ""

    normalized_active_paths = {item.casefold() for item in active_paths}
    for candidate in sorted(candidates, key=str.casefold):
        candidate_path = str(candidate).strip()
        candidate_basename = Path(candidate_path).name.casefold() if candidate_path else ""
        candidate_casefold = candidate_path.casefold()
        is_path_like_candidate = (
            "/" in candidate_path
            or candidate_path.startswith("../")
            or candidate_path.startswith("~/")
        )
        if not is_path_like_candidate and not _POSITIVE_REPO_NAME_CONTEXT_PATTERN.search(context_lower):
            continue
        if candidate_casefold in normalized_active_paths:
            continue
        if candidate_basename and candidate_basename in active_basenames:
            continue
        if candidate_basename and candidate_basename != active_repo_name:
            return candidate_path
    return ""


def _looks_like_standalone_candidate_line(line: str, candidates: set[str]) -> bool:
    if not candidates:
        return False
    remainder = line
    for candidate in sorted(candidates, key=len, reverse=True):
        remainder = remainder.replace(candidate, " ")
    normalized = re.sub(r"[-*•:`'\"|()\[\]{}]+", " ", remainder)
    return not normalized.strip()
