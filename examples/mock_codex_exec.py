#!/usr/bin/env python3
"""Safe local stub for testing `execute-codex` without calling Codex."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    last_message_path: Path | None = None
    for index, value in enumerate(args):
        if value in {"-o", "--output-last-message"} and index + 1 < len(args):
            last_message_path = Path(args[index + 1])
            break

    prompt = sys.stdin.read()
    if last_message_path is not None:
        last_message_path.write_text(
            "\n".join(
                [
                    "Mock Codex execution completed.",
                    f"Prompt characters: {len(prompt)}",
                    "Files touched: README.md",
                    "Checks run: python3 -m unittest discover -s tests",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    print(json.dumps({"type": "thread.started", "thread_id": "mock-thread"}))
    print(json.dumps({"type": "turn.started"}))
    print(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_0",
                    "type": "agent_message",
                    "text": "Mock Codex execution completed.",
                },
            }
        )
    )
    print(json.dumps({"type": "turn.completed"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
