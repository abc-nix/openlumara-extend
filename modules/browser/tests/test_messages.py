# Guard for user-visible text (2026-10-31).
#
# Measured on the core modules: 146 failure messages, median 30 chars, p90 54,
# max 109. The model pays for every one of these on every failure, so this
# module keeps the same budget; a test is the only way that stays true as the
# module grows. Collected by syntax only (no imports, no stubs, no browser):
# result(..., success=False) arguments, raised errors that reach the error
# boundary, and the "note" strings returned inside results.
#
# Run: python3 -m unittest discover -s modules/browser/tests -v

import ast
import unittest
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parents[1]
FILES = ("module.py", "page.py", "cdp.py", "util.py")
MAX_CHARS = 110


def _render(node):
    """Literal text of a message, with {…} standing for each interpolation."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(_render(v) if not isinstance(v, ast.FormattedValue) else "{…}"
                       for v in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _render(node.left), _render(node.right)
        return None if left is None or right is None else left + right
    return None


def _messages():
    """(file, line, text) for every user-visible message in the module."""
    for name in FILES:
        tree = ast.parse((MODULE_DIR / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                fname = fn.attr if isinstance(fn, ast.Attribute) else (
                    fn.id if isinstance(fn, ast.Name) else "")
                if fname == "result" and node.args:
                    fails = any(k.arg == "success"
                                and isinstance(k.value, ast.Constant)
                                and k.value.value is False for k in node.keywords)
                    text = _render(node.args[0])
                    if fails and text:
                        yield name, node.lineno, text
                elif fname in ("RuntimeError", "ValueError") and node.args:
                    text = _render(node.args[0])
                    if text:
                        yield name, node.lineno, text
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    wanted = (
                        isinstance(target, ast.Name)
                        and target.id in ("fallback_note", "grid_note")
                    ) or (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value == "note"
                    )
                    if wanted:
                        text = _render(node.value)
                        if text:
                            yield name, node.lineno, text


class MessageLengthTests(unittest.TestCase):
    def test_every_message_stays_inside_the_core_budget(self):
        long = [f"{name}:{line} ({len(text)}) {text}"
                for name, line, text in _messages() if len(text) > MAX_CHARS]
        self.assertEqual(long, [], "user-visible text over 110 chars")

    def test_the_scan_finds_messages(self):
        # a broken collector would silently pass the check above
        self.assertGreater(len(list(_messages())), 20)


if __name__ == "__main__":
    unittest.main()
