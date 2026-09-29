from __future__ import annotations

import re


_REDACTIONS = (
    (re.compile(r"(?i)(\b(?:authorization\s*:\s*bearer)\s+)[^\s\"']+"), r"\1[REDACTED]"),
    (
        re.compile(
            r"(?i)(\b(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|API_KEY|ACCESS_TOKEN|SECRET_KEY|PASSWORD|CLIENT_SECRET)\b\s*[=:]\s*[\"']?)[^\s,;\"']+"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"), "[REDACTED_TOKEN]"),
)


def redact_text(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text
