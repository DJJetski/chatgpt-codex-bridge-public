from __future__ import annotations

_SUPERPOWER_SKILL_NAMES = {
    "using-superpowers",
    "executing-plans",
    "systematic-debugging",
}
_DEFAULT_SKILL_HINTS = (
    "token-efficiency",
    "using-superpowers",
    "executing-plans",
    "systematic-debugging",
)
_CODEX_SKILLS_ROOT_HINT = "${CODEX_HOME:-$HOME/.codex}/skills"
_AGENT_STACK_ROOT_HINT = "$HOME/.agent-stack/repos"


def codex_exec_capability_guidance_text() -> str:
    return "\n".join(codex_exec_capability_guidance_lines())


def codex_exec_capability_guidance_lines() -> list[str]:
    return [
        "Codex exec capability notes:",
        "- Codex exec runs as a terminal subprocess. Use app/plugin tool namespaces only when this runtime actually exposes them; otherwise use shell-visible local routes such as repo-native CLIs, osascript, screencapture, Codex app-server, Keychain, normal browser automation, and local helper scripts.",
        "- Treat injected GrapeRoot or hook-first context as the initial orientation pack when present. Run workspace-graph only if a callable workspace-graph surface is actually available; if it is absent, do one targeted fallback read and keep moving.",
        "- Browser Use and Computer Use should be used when exposed by the runtime. If they are not exposed in codex exec, do not retry imaginary tool calls; use the equivalent real-session terminal/app automation path before declaring GUI, browser, login, auth, or permission blockers.",
        f"- Skill root aliases from Codex app prompts are not filesystem directories. Do not try paths like {_CODEX_SKILLS_ROOT_HINT}/r0/<skill>/SKILL.md or {_CODEX_SKILLS_ROOT_HINT}/.system/<skill>/SKILL.md unless that exact path already exists.",
        "- Skill path hints for codex exec on this machine:",
        *[_skill_hint_line(name) for name in _DEFAULT_SKILL_HINTS],
        f"- To locate any other skill, prefer: rg --files {_CODEX_SKILLS_ROOT_HINT} {_AGENT_STACK_ROOT_HINT} | rg '/<skill>/SKILL\\.md$'",
        "- For JSON parsing in shell commands, redirect command output to a temp file and parse that file. Do not pipe data into `python3 - <<'PY'`; the heredoc consumes stdin and the pipeline data will not reach Python.",
    ]


def _skill_hint_line(skill_name: str) -> str:
    return f"  - {skill_name}: {_preferred_skill_path(skill_name)}"


def _preferred_skill_path(skill_name: str) -> str:
    codex_skill_path = f"{_CODEX_SKILLS_ROOT_HINT}/{skill_name}/SKILL.md"
    if skill_name in _SUPERPOWER_SKILL_NAMES:
        return f"{codex_skill_path} (fallback: {_AGENT_STACK_ROOT_HINT}/superpowers/skills/{skill_name}/SKILL.md)"
    return codex_skill_path
