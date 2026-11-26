#!/usr/bin/env python3
"""Unit tests for examples/text_stats.py using Python 3 standard library only."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

EXAMPLES_DIR = Path(__file__).resolve().parent
SCRIPT_PATH = EXAMPLES_DIR / "text_stats.py"

if str(EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLES_DIR))

import text_stats  # noqa: E402


EXPECTED_KEYS = {"characters", "non_whitespace", "lines", "han_characters"}


class CountTextTests(unittest.TestCase):
    def test_empty_input(self) -> None:
        self.assertEqual(
            text_stats.count_text(""),
            {
                "characters": 0,
                "non_whitespace": 0,
                "lines": 0,
                "han_characters": 0,
            },
        )

    def test_chinese_and_english(self) -> None:
        sample = "你好 Gemini!\n第二行"
        self.assertEqual(
            text_stats.count_text(sample),
            {
                "characters": 14,
                "non_whitespace": 12,
                "lines": 2,
                "han_characters": 5,
            },
        )

        # Chinese punctuation is non-whitespace but not in U+4E00..U+9FFF.
        punct_sample = "中文，标点！Hello。"
        self.assertEqual(
            text_stats.count_text(punct_sample),
            {
                "characters": 12,
                "non_whitespace": 12,
                "lines": 1,
                "han_characters": 4,
            },
        )

    def test_whitespace_and_newlines(self) -> None:
        ws_only = " \t\u3000\n\r\n"
        self.assertEqual(
            text_stats.count_text(ws_only),
            {
                "characters": 6,
                "non_whitespace": 0,
                "lines": 2,
                "han_characters": 0,
            },
        )

        self.assertEqual(
            text_stats.count_text("\n"),
            {
                "characters": 1,
                "non_whitespace": 0,
                "lines": 1,
                "han_characters": 0,
            },
        )

        self.assertEqual(
            text_stats.count_text("a\n"),
            {
                "characters": 2,
                "non_whitespace": 1,
                "lines": 1,
                "han_characters": 0,
            },
        )

        self.assertEqual(
            text_stats.count_text("a\n\nb"),
            {
                "characters": 4,
                "non_whitespace": 2,
                "lines": 3,
                "han_characters": 0,
            },
        )

    def test_crlf(self) -> None:
        self.assertEqual(
            text_stats.count_text("第一行\r\n第二行\r\n"),
            {
                "characters": 10,
                "non_whitespace": 6,
                "lines": 2,
                "han_characters": 6,
            },
        )
        self.assertEqual(
            text_stats.count_text("abc\r\ndef"),
            {
                "characters": 8,
                "non_whitespace": 6,
                "lines": 2,
                "han_characters": 0,
            },
        )

    def test_emoji_and_non_basic_han(self) -> None:
        # Single codepoint emoji, ZWJ emoji sequence (3 codepoints), and flag (2 codepoints).
        self.assertEqual(
            text_stats.count_text("😀"),
            {
                "characters": 1,
                "non_whitespace": 1,
                "lines": 1,
                "han_characters": 0,
            },
        )
        self.assertEqual(
            text_stats.count_text("👩\u200d💻"),
            {
                "characters": 3,
                "non_whitespace": 3,
                "lines": 1,
                "han_characters": 0,
            },
        )
        self.assertEqual(
            text_stats.count_text("🇨🇳"),
            {
                "characters": 2,
                "non_whitespace": 2,
                "lines": 1,
                "han_characters": 0,
            },
        )

        # Exact boundaries of basic Han U+4E00..U+9FFF.
        boundary_text = "\u4dff\u4e00\u9fff\ua000"
        self.assertEqual(
            text_stats.count_text(boundary_text),
            {
                "characters": 4,
                "non_whitespace": 4,
                "lines": 1,
                "han_characters": 2,
            },
        )

        # Extension A (U+3400), Extension B (U+20000), Compatibility Ideograph (U+F900).
        non_basic_han = "\u3400\U00020000\uf900"
        self.assertEqual(
            text_stats.count_text(non_basic_han),
            {
                "characters": 3,
                "non_whitespace": 3,
                "lines": 1,
                "han_characters": 0,
            },
        )

        mixed = "你好😀\u3400\U00020000\uf900"
        self.assertEqual(
            text_stats.count_text(mixed),
            {
                "characters": 6,
                "non_whitespace": 6,
                "lines": 1,
                "han_characters": 2,
            },
        )

    def test_invalid_python_input_types(self) -> None:
        invalid_values = [
            None,
            b"hello",
            "你好".encode("utf-8"),
            bytearray(b"abc"),
            123,
            3.14,
            True,
            False,
            ["你好"],
            ("你好",),
            {"text": "你好"},
        ]
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(TypeError):
                    text_stats.count_text(value)  # type: ignore[arg-type]


class CliTests(unittest.TestCase):
    def _run_cli(
        self, args: list[str], *, stdin_bytes: bytes | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args],
            input=stdin_bytes,
            capture_output=True,
            check=False,
        )

    def _assert_valid_json_stats(
        self, proc: subprocess.CompletedProcess[bytes], expected: dict[str, int]
    ) -> None:
        self.assertEqual(proc.returncode, 0, msg=proc.stderr.decode("utf-8", errors="replace"))
        self.assertEqual(proc.stderr, b"")
        stdout_text = proc.stdout.decode("utf-8")
        data = json.loads(stdout_text)
        self.assertEqual(set(data.keys()), EXPECTED_KEYS)
        for value in data.values():
            self.assertIs(type(value), int)
        self.assertEqual(data, expected)

    def _assert_concise_error(self, proc: subprocess.CompletedProcess[bytes]) -> None:
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
        stderr_text = proc.stderr.decode("utf-8", errors="replace").strip()
        self.assertTrue(stderr_text)
        self.assertNotIn("Traceback", stderr_text)

    def test_cli_valid_stdin(self) -> None:
        proc = self._run_cli(["-"], stdin_bytes="你好 Gemini!\n第二行".encode("utf-8"))
        self._assert_valid_json_stats(
            proc,
            {
                "characters": 14,
                "non_whitespace": 12,
                "lines": 2,
                "han_characters": 5,
            },
        )

        empty_proc = self._run_cli(["-"], stdin_bytes=b"")
        self._assert_valid_json_stats(
            empty_proc,
            {
                "characters": 0,
                "non_whitespace": 0,
                "lines": 0,
                "han_characters": 0,
            },
        )

        # CRLF bytes must be preserved rather than normalized to LF.
        crlf_proc = self._run_cli(["-"], stdin_bytes="第一行\r\n第二行\r\n".encode("utf-8"))
        self._assert_valid_json_stats(
            crlf_proc,
            {
                "characters": 10,
                "non_whitespace": 6,
                "lines": 2,
                "han_characters": 6,
            },
        )

    def test_cli_valid_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sample_file = Path(tmpdir) / "sample.txt"
            sample_file.write_bytes("你好😀\r\nExtA:\u3400".encode("utf-8"))
            proc = self._run_cli([str(sample_file)])
            self._assert_valid_json_stats(
                proc,
                {
                    "characters": 11,
                    "non_whitespace": 9,
                    "lines": 2,
                    "han_characters": 2,
                },
            )

            empty_file = Path(tmpdir) / "empty.txt"
            empty_file.write_bytes(b"")
            empty_proc = self._run_cli([str(empty_file)])
            self._assert_valid_json_stats(
                empty_proc,
                {
                    "characters": 0,
                    "non_whitespace": 0,
                    "lines": 0,
                    "han_characters": 0,
                },
            )

    def test_cli_malformed_utf8_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            bad_file = Path(tmpdir) / "bad.bin"
            bad_file.write_bytes(b"\xff\xfe\xfd")
            proc = self._run_cli([str(bad_file)])
            self._assert_concise_error(proc)

    def test_cli_malformed_utf8_stdin(self) -> None:
        proc = self._run_cli(["-"], stdin_bytes=b"\xe4\xbd")
        self._assert_concise_error(proc)

    def test_cli_missing_file_and_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "does-not-exist.txt"
            proc_missing = self._run_cli([str(missing)])
            self._assert_concise_error(proc_missing)

            proc_dir = self._run_cli([tmpdir])
            self._assert_concise_error(proc_dir)

    def test_cli_invalid_arguments(self) -> None:
        for bad_args in ([], ["a.txt", "b.txt"], [""]):
            with self.subTest(bad_args=bad_args):
                proc = self._run_cli(bad_args)
                self._assert_concise_error(proc)


if __name__ == "__main__":
    unittest.main()
