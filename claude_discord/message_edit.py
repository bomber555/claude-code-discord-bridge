"""Tell a session that a person edited a message it already read.

Discord's edit event carries the new text only. The old text comes from
discord.py's in-memory message cache, so it is gone after a restart or once the
message ages out. The agent is then told there is no diff instead of being shown
a guess.
"""

from __future__ import annotations

import difflib

_DIFF_CONTEXT_LINES = 1


def build_edit_prompt(before: str | None, after: str, jump_url: str) -> str:
    """Describe an edit to the agent. ``before`` is ``None`` when it is not known."""
    parts = [f"[The user edited an earlier message in this thread: {jump_url}]"]
    if before is None:
        parts.append("The earlier text is not available, so there is no diff.")
    else:
        diff = difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            lineterm="",
            n=_DIFF_CONTEXT_LINES,
        )
        # The first two lines are the ---/+++ file headers, which say nothing here.
        body = "\n".join(list(diff)[2:])
        parts.append(f"Diff:\n```diff\n{body}\n```")
    parts.append(f"Full text after the edit:\n{after}" if after else "The text is now empty.")
    return "\n\n".join(parts)
