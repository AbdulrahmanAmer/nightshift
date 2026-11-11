"""The one way a value becomes a PowerShell string literal (standard library only: setup imports it).

PowerShell ends a single-quoted string at ANY of five characters, not only at the plain apostrophe: U+0027,
U+2018, U+2019, U+201A and U+201B (its tokenizer asks `IsSingleQuote`), and it reads two of them in a row
inside a literal as one character of the string. So a literal is exact when each of the five is written
twice. A Windows user named O'Brien or O<U+2019>Brien, whose name is in the Desktop, `%LOCALAPPDATA%` and
`%TEMP%`, then reaches every generated script unchanged and can end no string early.

Every module that builds PowerShell text from a path, a name or any other value calls `ps_literal` and
nothing else. `tests/test_ps_quoting.py::test_there_is_one_quoting_function` scans the product's Python for a
second way and fails on: a helper named like the old ones; a quote character doubled with `.replace()` or
`.sub()`; a `.format()` or `%` template (a string or a module-level name) with a quote character directly
before a field; and a quote character directly before a value in an f-string or a `+` chain whose literal
text looks like PowerShell (a variable, a static call, a Verb-Noun command, `&` or a `-Parameter` before a
quote, a quote beside a comma or parenthesis) or is a bare quote. It does not see a value quoted in text
with none of those shapes, or text assembled in a loop, a join or a call it does not follow.
"""

from __future__ import annotations

#: The characters PowerShell treats as a single quote (`CharTraits.IsSingleQuote`).
QUOTE_CHARS = "'‘’‚‛"


def ps_literal(value: str) -> str:
    """`value` as a complete single-quoted PowerShell literal: the outer quotes included, every character of
    `QUOTE_CHARS` written twice, so the string PowerShell reads is exactly `value`.

    A NUL raises `ValueError`: no Windows path or name holds one, and a literal that PowerShell would cut
    short is worse than a refusal (the caller's own failure path reports it, as for any failed script).
    """
    if not isinstance(value, str):
        raise TypeError(f"ps_literal takes a str, not {type(value).__name__}")
    if "\x00" in value:
        raise ValueError("a PowerShell literal cannot hold a NUL character")
    out = value
    for ch in QUOTE_CHARS:
        out = out.replace(ch, ch + ch)
    return "'" + out + "'"
