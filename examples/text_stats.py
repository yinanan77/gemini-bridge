#!/usr/bin/env python3
"""Simple Chinese-script text statistics tool using Python 3 standard library only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Optional, Sequence

BASIC_HAN_START = "\u4e00"
BASIC_HAN_END = "\u9fff"


def count_text(text: str) -> Dict[str, int]:
    """Return exact codepoint, non-whitespace, line, and basic-Han character counts.

    Metrics:
    - ``characters``: total number of Python ``str`` Unicode codepoints (``len(text)``).
    - ``non_whitespace``: codepoints where ``not ch.isspace()``.
    - ``lines``: line count defined as ``len(text.splitlines())``.
    - ``han_characters``: codepoints in the basic CJK Unified Ideographs block
      ``U+4E00..U+9FFF`` only.

    Raises:
        TypeError: If ``text`` is not a ``str``.
    """
    if not isinstance(text, str):
        raise TypeError(f"text must be str, got {type(text).__name__}")

    if not text:
        return {
            "characters": 0,
            "non_whitespace": 0,
            "lines": 0,
            "han_characters": 0,
        }

    non_whitespace = 0
    han_characters = 0
    for ch in text:
        if not ch.isspace():
            non_whitespace += 1
        if BASIC_HAN_START <= ch <= BASIC_HAN_END:
            han_characters += 1

    return {
        "characters": len(text),
        "non_whitespace": non_whitespace,
        "lines": len(text.splitlines()),
        "han_characters": han_characters,
    }


def _read_utf8_source(source: str) -> str:
    """Read UTF-8 text verbatim (preserving CRLF) from stdin ('-') or a file path."""
    if not isinstance(source, str) or not source:
        raise ValueError("Input path must be a non-empty file path or '-'.")

    if source == "-":
        stdin_buffer = getattr(sys.stdin, "buffer", None)
        if stdin_buffer is not None:
            raw_bytes = stdin_buffer.read()
            return raw_bytes.decode("utf-8")
        return sys.stdin.read()

    path = Path(source)
    raw_bytes = path.read_bytes()
    return raw_bytes.decode("utf-8")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="text_stats.py",
        description=(
            "Compute codepoint, non-whitespace, line, and basic-Han (U+4E00..U+9FFF) "
            "statistics for a UTF-8 file or stdin ('-')."
        ),
    )
    parser.add_argument(
        "source",
        help="Path to a UTF-8 text file, or '-' to read UTF-8 text from stdin.",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point returning an integer process exit code."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else 1

    try:
        text = _read_utf8_source(args.source)
        stats = count_text(text)
    except UnicodeDecodeError as exc:
        label = "stdin" if args.source == "-" else args.source
        print(f"error: invalid UTF-8 input ({label}): {exc.reason}", file=sys.stderr)
        return 1
    except OSError as exc:
        reason = exc.strerror or str(exc)
        print(f"error: cannot read '{args.source}': {reason}", file=sys.stderr)
        return 1
    except (TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
