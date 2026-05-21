from __future__ import annotations

DEFAULT_HIGHEST_MODEL = "gpt-5.5"
DEFAULT_HIGHEST_REASONING_EFFORT = "xhigh"

DEFAULT_CHATGPT_MODEL = DEFAULT_HIGHEST_MODEL
DEFAULT_CHATGPT_REASONING_EFFORT = DEFAULT_HIGHEST_REASONING_EFFORT
DEFAULT_CODEX_MODEL = DEFAULT_HIGHEST_MODEL
DEFAULT_CODEX_REASONING_EFFORT = DEFAULT_HIGHEST_REASONING_EFFORT

CODEX_MODEL_OPTIONS: tuple[tuple[str, str], ...] = (
    ("gpt-5.5", "gpt-5.5 (Highest)"),
    ("gpt-5.4", "gpt-5.4"),
    ("gpt-5.4-mini", "gpt-5.4-mini"),
    ("gpt-5.3-codex-spark", "ChatGPT 5.3 Spark Codex"),
)

CODEX_REASONING_EFFORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("low", "Low"),
    ("medium", "Medium"),
    ("high", "High"),
    ("xhigh", "Extra High"),
)
