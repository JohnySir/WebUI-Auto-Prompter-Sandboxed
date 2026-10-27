import unittest

from prompter import READ_STATE_JS, browser_state, capture_scheme, parse_prompts, read_state


class CaptureSchemeTests(unittest.TestCase):
    def test_safe_with_no_browser(self):
        # no CDP endpoint running — must return None, never raise
        self.assertIsNone(capture_scheme("http://127.0.0.1:1"))


class ParsePromptsTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(parse_prompts("a\n\nb\r\nc"), ["a", "b", "c"])

    def test_blank_and_whitespace(self):
        self.assertEqual(parse_prompts("  x  \n   \n\ny"), ["x", "y"])

    def test_empty(self):
        self.assertEqual(parse_prompts("\n   \n"), [])

    def test_every_line_is_a_prompt(self):
        self.assertEqual(parse_prompts("# tag, detail"), ["# tag, detail"])


class BrowserStateTests(unittest.TestCase):
    # interrupt visible wins regardless of anything else
    def test_busy(self):
        self.assertEqual(browser_state(True, False, False), "BUSY")
        self.assertEqual(browser_state(True, True, True), "BUSY")

    # generate visible + enabled = ready to click
    def test_ready(self):
        self.assertEqual(browser_state(False, True, True), "READY")

    # generate visible but button disabled (e.g. mid-load) — never clickable
    def test_generate_disabled(self):
        self.assertEqual(browser_state(False, True, False), "UNKNOWN")

    # nothing visible (wrong tab, page loading) — never clickable
    def test_unknown(self):
        self.assertEqual(browser_state(False, False, True), "UNKNOWN")
        self.assertEqual(browser_state(False, False, False), "UNKNOWN")


class FakePage:
    def __init__(self, interrupt, generate, enabled, bomb=False):
        self.s = {"interrupt": interrupt, "generate": generate, "enabled": enabled}
        self.bomb = bomb

    def evaluate(self, script):
        assert script == READ_STATE_JS, "read_state must use the shared probe"
        if self.bomb:
            raise RuntimeError("Target page, context or browser has been closed")
        return self.s


class ReadStateTests(unittest.TestCase):
    def test_ready_ui(self):
        self.assertEqual(read_state(FakePage(False, True, True)), "READY")

    def test_busy_ui(self):
        self.assertEqual(read_state(FakePage(True, False, True)), "BUSY")

    def test_malformed_probe_is_unknown(self):
        self.assertEqual(read_state(FakePage(None, None, None)), "UNKNOWN")

    def test_dead_browser_raises(self):
        with self.assertRaises(RuntimeError):
            read_state(FakePage(None, None, None, bomb=True))


if __name__ == "__main__":
    unittest.main()
