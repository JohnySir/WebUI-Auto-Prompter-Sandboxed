"""WEBUI Prompter (sandboxed) — batch-drive the image-generation dashboard
from a prompt file in a dedicated, Playwright-controlled Chrome window.

The WebUI owns the UI and generation; this script only writes the prompt
field and clicks Generate in its own sandbox browser
(profile: .sandbox-profile, CDP port 9222). One prompt per line in a
.txt/.md file; press Start in the terminal window.
"""

from __future__ import annotations

import argparse
import json
import queue
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:7860"
CDP_PORT = 9222
CDP_ROOT = f"http://127.0.0.1:{CDP_PORT}"
PROFILE = Path(__file__).with_name(".sandbox-profile")

POLL = 0.4  # seconds between button-state checks
START_TIMEOUT = 20.0  # max wait for the click to visibly start a generation
UNKNOWN_WARN = 10.0  # seconds before warning the txt2img elements are not visible
AGENT_DELAY = 0.15  # settle time after focusing / Change-event dispatch

PROMPT_SEL = "#txt2img_prompt textarea"
# forge classic puts the elem_id on the <button> itself (not a wrapper div), so
# #txt2img_generate is the clickable button and #txt2img_generate button matches nothing.
GENERATE_SEL = "#txt2img_generate"
INTERRUPT_SEL = "#txt2img_interrupt"

# single no-wait DOM query: locator is_enabled()/visibility auto-waits would
# stall the 0.4 s poll loop (and --check) while the button is hidden.
READ_STATE_JS = """() => {
  const vis = id => { const el = document.getElementById(id); return !!el && el.getClientRects().length > 0; };
  const gen = document.getElementById('txt2img_generate');
  return {interrupt: vis('txt2img_interrupt'),
          generate: vis('txt2img_generate'),
          enabled: !!gen && !gen.disabled && gen.tagName === 'BUTTON'};
}"""


# --------------------------------------------------------------------------
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------

def parse_prompts(text: str) -> list[str]:
    """One non-empty line = one prompt."""
    return [line.strip() for line in text.splitlines() if line.strip()]


def browser_state(interrupt_visible, generate_visible, button_enabled) -> str:
    """Map element visibility to READY / BUSY / UNKNOWN (BUSY wins, conservative)."""
    if interrupt_visible:
        return "BUSY"
    if generate_visible and button_enabled:
        return "READY"
    return "UNKNOWN"


def read_state(page) -> str:
    """Poll the three sandbox selectors on the current page (no auto-waiting).
    Raises if the page/connection is dead — callers turn that into a run error."""
    s = page.evaluate(READ_STATE_JS) or {}
    return browser_state(s.get("interrupt"), s.get("generate"), s.get("enabled"))


def idle_probe(url: str) -> str:
    """Connection probe used when idle (no Playwright on the UI thread)."""
    try:
        with urllib.request.urlopen(f"{CDP_ROOT}/json/list", timeout=0.2) as r:
            tabs = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return "UNKNOWN"
    if any(t.get("type") == "page" and t.get("url", "").startswith(url) for t in tabs):
        return "READY"
    return "UNKNOWN"


# --------------------------------------------------------------------------
# Browser (sandbox Chrome) session
# --------------------------------------------------------------------------

class PromptSetupError(Exception):
    pass


def launch_browser(pw, url: str) -> None:
    exe = pw.chromium.executable_path
    if not Path(exe).exists():
        raise PromptSetupError("Playwright Chromium not installed — run: python -m playwright install chromium")
    flags = [
        exe,
        f"--user-data-dir={PROFILE}",
        f"--remote-debugging-port={CDP_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
        "--hide-crash-restore-bubble",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        url,
    ]
    kw: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(flags, **kw)
    except OSError as e:
        raise PromptSetupError(f"Could not launch the sandbox browser: {e}") from e


def cdp_endpoint_ok() -> tuple[bool, str]:
    """(reachable, browser-string) for the fixed CDP port."""
    try:
        with urllib.request.urlopen(f"{CDP_ROOT}/json/version", timeout=0.2) as r:
            info = json.loads(r.read().decode("utf-8", "replace"))
        return True, str(info.get("Browser", ""))
    except Exception:
        return False, ""


def ensure_browser(pw, url: str) -> None:
    """Start the sandbox browser if its CDP endpoint is not up."""
    ok, browser = cdp_endpoint_ok()
    if ok and not any(tag in browser for tag in ("Chrome", "Chromium", "Edge")):
        raise PromptSetupError(
            f"Port {CDP_PORT} is owned by another process ({browser or 'unknown'}) — free it or change CDP_PORT.")
    if ok:
        return
    launch_browser(pw, url)
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if cdp_endpoint_ok()[0]:
            return
        time.sleep(0.2)
    raise PromptSetupError(
        "Sandbox browser started but the CDP endpoint did not open within 10 s.")


def capture_scheme(url: str = DEFAULT_URL) -> str | None:
    """Ask the dashboard tab its real prefers-color-scheme over raw CDP,
    BEFORE Playwright attaches (its attach forces a light emulation)."""
    from websockets.sync.client import connect as ws_connect

    cmd = json.dumps({"id": 1, "method": "Runtime.evaluate",
                      "params": {"expression":
                                 "matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'",
                                 "returnByValue": True}})
    try:
        ws = next(t["webSocketDebuggerUrl"]
                  for t in json.loads(urllib.request.urlopen(
                      f"{CDP_ROOT}/json/list", timeout=2).read())
                  if t.get("type") == "page" and t.get("url", "").startswith(url))
        with ws_connect(ws, open_timeout=3) as w:
            w.send(cmd)
            while True:
                msg = json.loads(w.recv(timeout=3))
                if msg.get("id") == 1:
                    return msg["result"]["result"].get("value")
    except Exception:
        return None


def pin_scheme(cdp, page, scheme: str | None) -> None:
    """Playwright's CDP attach emulates prefers-color-scheme: light by default,
    which flips the dashboard's media-query dark theme to white; pin it back."""
    if scheme in ("dark", "light"):
        page.emulate_media(color_scheme=scheme)


def connect_page(cdp, url: str):
    """Attach to the tab showing the dashboard. During a fresh browser start the
    command-line tab may still be about:blank — navigate a blank/missing tab to
    the URL with retries before giving up (never opens a stream of tabs)."""
    contexts = cdp.contexts
    ctx = contexts[0] if contexts else cdp.new_context()
    page = next((p for p in ctx.pages if p.url.startswith(url)), None)
    if page is not None:
        return page
    page = next((p for p in ctx.pages if p.url in ("about:blank", "")), None)
    if page is None:
        page = ctx.new_page()
    last = None
    for _ in range(3):
        try:
            page.goto(url, timeout=30_000)
            return page
        except Exception as e:  # noqa: BLE001 — each attempt can fail while startup races
            last = e
            if page.is_closed():
                page = ctx.new_page()
            time.sleep(1.0)
    raise PromptSetupError(f"The dashboard did not load ({last}). Is the webui running?")


def open_session(url: str):
    """Playwright handle + connection + page; caller closes both at the end."""
    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    try:
        ensure_browser(pw, url)
        scheme = capture_scheme()
        cdp = pw.chromium.connect_over_cdp(CDP_ROOT)
        try:
            page = connect_page(cdp, url)
            pin_scheme(cdp, page, scheme)
        except Exception:
            cdp.close()
            raise
    except Exception:
        pw.stop()
        raise
    return pw, cdp, page


# --------------------------------------------------------------------------
# Automation worker
# --------------------------------------------------------------------------

class StopRequested(Exception):
    pass


class PromptError(Exception):
    pass


class RunState:
    """Shared automation state; scalar updates are GIL-atomic, UI only reads."""

    def __init__(self, total: int):
        self.total = total
        self.phase = "IDLE"
        self.browser = "UNKNOWN"
        self.index = 0
        self.current = ""
        self.submitted = 0
        self.completed = 0
        self.failed = 0
        self.gen_started: float | None = None
        self.last_duration: float | None = None
        self.running = False

    def snapshot(self) -> dict:
        return {
            "phase": self.phase, "browser": self.browser, "index": self.index,
            "total": self.total, "current": self.current, "submitted": self.submitted,
            "completed": self.completed, "failed": self.failed,
            "gen_started": self.gen_started, "last_duration": self.last_duration,
            "running": self.running,
        }


def wait_ready(page, state: RunState, stop: threading.Event, log) -> None:
    unknown_since = None
    warned = False
    while not stop.is_set():
        s = read_state(page)
        state.browser = s
        if s == "READY":
            return
        if s == "UNKNOWN":
            now = time.time()
            if unknown_since is None:
                unknown_since = now
            elif not warned and now - unknown_since > UNKNOWN_WARN:
                log("warn", "txt2img controls not visible — open the txt2img tab in the sandbox browser")
                warned = True
        else:
            unknown_since, warned = None, False
        time.sleep(POLL)
    raise StopRequested()


def set_prompt(page, ta, prompt: str) -> None:
    ta.fill(prompt)
    time.sleep(AGENT_DELAY)
    ta.evaluate("el => el.dispatchEvent(new Event('change', {bubbles: true}))")


def check_prompt_text(ta, want: str) -> None:
    got = (ta.input_value() or "").replace("\r\n", "\n").strip()
    want = want.replace("\r\n", "\n").strip()
    if got == want:
        return
    if want.startswith(got):
        raise PromptError(
            f"Prompt box truncated the fill: expected {len(want)} chars, field holds {len(got)}. Not generating.")
    raise PromptError(
        f"Prompt box content mismatch after fill (expected {len(want)} chars, got {len(got)}). Not generating.")


def fill_and_verify(page, ta, prompt: str, log) -> None:
    """Fill via Playwright (no OS clipboard); compare, then fall back to
    keyboard typing once if fill did not stick."""
    set_prompt(page, ta, prompt)
    try:
        check_prompt_text(ta, prompt)
    except PromptError:
        log("warn", "fill() did not stick — retrying via focus + keyboard typing")
        ta.click()
        time.sleep(AGENT_DELAY)
        page.keyboard.press("Control+a")
        time.sleep(AGENT_DELAY)
        page.keyboard.insert_text(prompt)
        time.sleep(AGENT_DELAY)
        ta.evaluate("el => el.dispatchEvent(new Event('change', {bubbles: true}))")
        check_prompt_text(ta, prompt)  # truncation/mismatch now stops for good
    log("info", f"Fill verified ({len(prompt.strip())} chars)")


def run_worker(state: RunState, url: str, prompts: list[str],
               stop: threading.Event, logq) -> None:
    def log(level: str, msg: str) -> None:
        logq.put((level, msg))

    try:
        state.running = True
        log("info", f"Run started — {len(prompts) - state.index} prompt(s) queued")
        pw, cdp, page = open_session(url)
        try:
            idx = state.index
            while idx < len(prompts) and not stop.is_set():
                prompt = prompts[idx]
                state.phase = "WAITING"
                state.index = idx
                state.current = prompt
                log("info", f"Prompt {idx + 1}/{len(prompts)} — waiting for Generate button")
                wait_ready(page, state, stop, log)

                state.phase = "PASTING"
                ta = page.locator(PROMPT_SEL)
                if not (ta.is_visible() and ta.is_editable()):
                    raise PromptError("Prompt box not visible/editable — open the txt2img tab in the sandbox browser.")
                fill_and_verify(page, ta, prompt, log)
                if stop.is_set():
                    log("warn", "Stopped before clicking Generate — prompt left in the box")
                    break
                if read_state(page) != "READY":
                    log("warn", "Button no longer ready after fill; waiting…")
                    wait_ready(page, state, stop, log)
                if (ta.input_value() or "").replace("\r\n", "\n").strip() != prompt.replace("\r\n", "\n").strip():
                    raise PromptError("Prompt field changed between fill and click (page reloaded?) — not generating.")

                page.locator(GENERATE_SEL).click()
                state.submitted += 1
                idx += 1
                state.index = idx
                log("info", "Generate clicked")
                state.phase = "STARTING"
                state.gen_started = time.time()

                deadline = time.time() + START_TIMEOUT
                started = False
                while time.time() < deadline and not stop.is_set():
                    s = read_state(page)
                    state.browser = s
                    if s == "BUSY":
                        started = True
                        break
                    time.sleep(POLL)
                if stop.is_set():
                    break
                if not started:
                    raise PromptError(
                        "Generate button did not start generating after click — stopping (no retry, it could hit Interrupt).")

                state.phase = "GENERATING"
                while not stop.is_set():
                    s = read_state(page)
                    state.browser = s
                    if s == "READY":
                        state.completed += 1
                        state.last_duration = time.time() - state.gen_started
                        log("info", f"Generation finished in {state.last_duration:.0f}s")
                        break
                    time.sleep(POLL)

            if stop.is_set():
                state.phase = "STOPPED"
                log("warn", "Run stopped. The browser's current generation (if any) is not interrupted.")
            elif idx >= len(prompts):
                state.phase = "DONE"
                log("info", f"All prompts done — submitted {state.submitted}, completed {state.completed}.")
        finally:
            # disconnect only; the browser, its tabs and any running generation stay alive
            cdp.close()
            pw.stop()
    except StopRequested:
        state.phase = "STOPPED"
        log("warn", "Run stopped.")
    except PromptSetupError as e:
        state.failed += 1
        state.phase = "ERROR"
        log("error", str(e))
    except PromptError as e:
        state.failed += 1
        state.phase = "ERROR"
        log("error", str(e))
        log("error", "Fix the issue, then press Start to retry this prompt, or Quit.")
    except Exception as e:  # noqa: BLE001 — keep the worker alive for the UI
        state.failed += 1
        state.phase = "ERROR"
        log("error", f"Unexpected error: {e}")
        log("error", traceback.format_exc().splitlines()[-1])
    finally:
        state.running = False


# --------------------------------------------------------------------------
# Preflight (--check)
# --------------------------------------------------------------------------

def do_check(args) -> int:
    try:
        pw, cdp, page = open_session(args.url)
    except PromptSetupError as e:
        print(f"✗ {e}")
        return 1
    try:
        try:
            page.wait_for_selector(PROMPT_SEL, state="attached", timeout=15_000)
        except Exception:
            print(f"✗ Dashboard did not load at {args.url} — is the webui running?")
            return 1
        ok = True

        def report(label, good: bool, extra: str = "") -> None:
            nonlocal ok
            if not good:
                ok = False
            print(f"  {label}: {'✓' if good else '✗'}{extra}")

        print(f"Tab: {page.url} — {page.title()!r}")
        s = page.evaluate(READ_STATE_JS) or {}
        ta = page.locator(PROMPT_SEL)
        report(f"Prompt box found {ta.count()}", ta.count() > 0, f"  ({PROMPT_SEL})")
        if ta.count() > 0:
            report(f"Prompt box visible  {ta.is_visible()}", ta.is_visible())
            report(f"Prompt box editable {ta.is_editable()}", ta.is_editable())
        report(f"Generate visible  {s.get('generate')}", bool(s.get("generate")), f"  ({GENERATE_SEL})")
        report(f"Generate enabled  {s.get('enabled')}", bool(s.get("enabled")))
        report(f"Interrupt visible {s.get('interrupt')}", not s.get("interrupt"), "  (False is good = idle)")
        state = read_state(page)
        print(f"State: {state}")
        if state == "UNKNOWN":
            print("  UNKNOWN: if the txt2img tab is not the active tab, open it before starting.")
        return 0 if ok else 1
    except Exception as e:  # noqa: BLE001 — a dead page/connection is a check failure too
        print(f"✗ {e}")
        return 1
    finally:
        cdp.close()
        pw.stop()


# --------------------------------------------------------------------------
# Textual terminal window
# --------------------------------------------------------------------------

from textual.app import App, ComposeResult  # noqa: E402
from textual.containers import Horizontal, Vertical  # noqa: E402
from textual.widgets import Button, Footer, Header, ProgressBar, RichLog, Static  # noqa: E402
from rich.text import Text  # noqa: E402

PHASE_STYLE = {
    "IDLE": "grey70",
    "WAITING": "cyan",
    "PASTING": "yellow",
    "STARTING": "yellow",
    "GENERATING": "orange_red1",
    "STOPPED": "yellow",
    "DONE": "green",
    "ERROR": "red",
}
BROWSER_LABEL = {"READY": "Ready", "BUSY": "Generating", "UNKNOWN": "Not ready"}


class PrompterApp(App):
    CSS = """
    #body { height: 1fr; }
    #status { padding: 0 1; height: auto; }
    #prompt { padding: 0 1; height: auto; }
    #stats { padding: 0 1; height: auto; color: $text-muted; }
    #progress { padding: 0 1; margin-bottom: 1; }
    #log { height: 1fr; border-top: solid $primary; }
    #buttons { height: 3; padding: 0 1; }
    #buttons Button { margin-right: 1; min-width: 12; }
    """
    BINDINGS = [
        ("f8", "start", "Start"),
        ("f9", "stop_run", "Stop"),
        ("f10", "quit_app", "Quit"),
    ]
    TITLE = "WEBUI Prompter (sandboxed)"

    def __init__(self, url: str, prompts: list[str], file_path: str):
        super().__init__()
        self.url = url
        self.prompts = prompts
        self.file_path = file_path
        self.state = RunState(total=len(prompts))
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.logq = queue.Queue()
        self._tick = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="body"):
            yield Static("", id="status")
            yield ProgressBar(total=len(self.prompts), show_eta=False, id="progress")
            yield Static("", id="prompt")
            yield Static("", id="stats")
            yield RichLog(id="log", max_lines=400, wrap=True)
        with Horizontal(id="buttons"):
            yield Button("Start", id="start", variant="success")
            yield Button("Open Browser", id="open", variant="primary")
            yield Button("Stop", id="stop", variant="error", disabled=True)
            yield Button("Refresh", id="refresh", variant="default")
            yield Button("Quit", id="quit", variant="default")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.2, self.refresh_ui)
        self.logq.put(("info", f"Prompt file: {self.file_path} ({len(self.prompts)} prompts)"))
        self.logq.put(("info", f"Dashboard: {self.url}   Profile: {PROFILE}"))
        long = [i for i, p in enumerate(self.prompts) if len(p) > 8000]
        if long:
            self.logq.put(("warn", f"{len(long)} prompt(s) over 8000 chars (first: #{long[0] + 1}) — watch for truncation warnings"))
        self.logq.put(("info", "Press Start (or F8). Stop (F9) halts scheduling; it never interrupts a running generation."))
        self.logq.put(("info", "No browser yet? Press Open Browser — it launches the sandbox Chrome at the dashboard."))
        self.logq.put(("info", "After a full round Start lights up again for another pass; Refresh reloads prompts.txt (queue resets)."))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "start":
            self.action_start()
        elif event.button.id == "open":
            self.action_open_browser()
        elif event.button.id == "refresh":
            self.action_refresh()
        elif event.button.id == "stop":
            self.action_stop_run()
        elif event.button.id == "quit":
            self.action_quit_app()

    def action_open_browser(self) -> None:
        btn = self.query_one("#open", Button)
        if getattr(btn, "disabled", False) or (self.worker and self.worker.is_alive()):
            return
        btn.disabled = True
        self.logq.put(("info", f"Opening sandbox browser at {self.url}…"))
        threading.Thread(target=self._open_browser_task, daemon=True).start()

    def _open_browser_task(self) -> None:
        from playwright.sync_api import sync_playwright

        pw = sync_playwright().start()
        try:
            try:
                ensure_browser(pw, self.url)
                scheme = capture_scheme(self.url)
                cdp = pw.chromium.connect_over_cdp(CDP_ROOT)
                try:
                    page = connect_page(cdp, self.url)
                    pin_scheme(cdp, page, scheme)
                    self.logq.put(("info", f"Browser is open — dashboard tab ready: {page.url} — {page.title()!r}"))
                except PromptSetupError as e:
                    # browser is open; the tab sits at its error page until the webui is up
                    self.logq.put(("warn",
                                   f"Browser is open, but the dashboard did not load ({e}). "
                                   "Start the webui, then refresh the tab or press Open Browser again."))
                finally:
                    cdp.close()
            finally:
                pw.stop()
        except Exception as e:  # noqa: BLE001 — surfaced to the UI log
            self.logq.put(("error", f"Could not open the browser: {e}"))
        finally:
            try:
                self.call_from_thread(self._open_browser_done)
            except Exception:  # app already closed — button state doesn't matter
                pass

    def _open_browser_done(self) -> None:
        self.query_one("#open", Button).disabled = False

    def action_start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if self.state.index >= len(self.prompts):
            if self.state.phase != "DONE":
                return
            # a finished round re-arms for another full pass
            self.state.index = 0
            self.state.submitted = 0
            self.state.completed = 0
            self.state.phase = "IDLE"
        self.stop_event.clear()
        self.worker = threading.Thread(
            target=run_worker,
            args=(self.state, self.url, self.prompts, self.stop_event, self.logq),
            daemon=True,
        )
        self.worker.start()

    def action_stop_run(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.logq.put(("warn", "Stop requested…"))

    def action_refresh(self) -> None:
        if self.worker and self.worker.is_alive():
            self.logq.put(("warn", "Stop the run before refreshing the prompt file."))
            return
        try:
            prompts = parse_prompts(Path(self.file_path).read_text(encoding="utf-8-sig"))
        except UnicodeDecodeError:
            self.logq.put(("error", f"{self.file_path} is not valid UTF-8 — resave it as UTF-8."))
            return
        except OSError as e:
            self.logq.put(("error", f"Could not read {self.file_path}: {e}"))
            return
        if not prompts:
            self.logq.put(("error", f"{self.file_path} has no prompts (one non-empty line per prompt)."))
            return
        self.prompts = prompts
        st = self.state
        st.total = len(prompts)
        st.index = 0
        st.current = ""
        st.submitted = 0
        st.completed = 0
        st.gen_started = None
        st.phase = "IDLE"
        self.logq.put(("info", f"Prompt file refreshed — {len(prompts)} prompts, queue reset for the next run."))

    def action_quit_app(self) -> None:
        self.stop_event.set()
        self.exit()

    def refresh_ui(self) -> None:
        st = self.state.snapshot()
        while True:
            try:
                level, msg = self.logq.get_nowait()
            except queue.Empty:
                break
            style = {"info": "grey70", "warn": "yellow", "error": "red"}.get(level, "white")
            self.query_one("#log", RichLog).write(Text(f"{time.strftime('%H:%M:%S')}  {msg}", style=style))

        if not st["running"]:
            self._tick += 1
            if self._tick % 10 == 0:  # ~2s idle connection probe (no Playwright on UI thread)
                self.state.browser = idle_probe(self.url)
                st = self.state.snapshot()

        phase_style = PHASE_STYLE.get(st["phase"], "white")
        browser = st["browser"]
        status = Text.assemble(
            ("● ", phase_style),
            (st["phase"], f"bold {phase_style}"),
            (f"   Browser: {BROWSER_LABEL.get(browser, browser)}",
             {"READY": "green", "BUSY": "orange_red1", "UNKNOWN": "red"}.get(browser, "white")),
        )
        if st["phase"] == "GENERATING" and st["gen_started"]:
            status.append(f"   {time.time() - st['gen_started']:.0f}s", style="orange_red1")
        status.append(f"   Submitted {st['submitted']} / {st['total']}", style="white")
        self.query_one("#status", Static).update(status)

        pb = self.query_one("#progress", ProgressBar)
        pb.total = st["total"] or 1
        pb.progress = st["completed"]

        idx = min(st["index"], max(st["total"] - 1, 0))
        cur = st["current"] or (self.prompts[idx] if self.prompts else "")
        shown = cur if len(cur) <= 240 else cur[:240] + "…"
        self.query_one("#prompt", Static).update(Text.assemble(
            (f"#{idx + 1} ", "grey70"),
            (shown or "—", "white"),
            (f"  ({len(cur)} chars)", "grey70"),
        ))

        last = f"   last {st['last_duration']:.0f}s" if st["last_duration"] else ""
        self.query_one("#stats", Static).update(f"finished {st['completed']}   failed {st['failed']}{last}")

        self.query_one("#start", Button).disabled = st["running"] or (
            st["index"] >= st["total"] and st["phase"] != "DONE")
        self.query_one("#stop", Button).disabled = not st["running"]


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout.reconfigure(errors="replace")  # ✓/✗ survive a cp1252 console
    ap = argparse.ArgumentParser(
        description="Batch-generate images from a prompt file in a sandboxed Playwright browser.")
    ap.add_argument("--file", help="prompt file (.txt/.md); default: prompts.txt")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"dashboard URL; default: {DEFAULT_URL}")
    ap.add_argument("--check", action="store_true", help="preflight only: selectors and current state")
    args = ap.parse_args()

    if args.check:
        sys.exit(do_check(args))

    file_path = Path(args.file or "prompts.txt")
    if not file_path.exists():
        sys.exit(f"Prompt file not found: {file_path}\nCreate it (one prompt per line) or pass --file PATH.")
    try:
        prompts = parse_prompts(file_path.read_text(encoding="utf-8-sig"))
    except UnicodeDecodeError:
        sys.exit(f"{file_path} is not valid UTF-8 — resave it as UTF-8.")
    if not prompts:
        sys.exit(f"{file_path} has no prompts (one non-empty line per prompt).")

    app = PrompterApp(args.url, prompts, str(file_path))
    try:
        app.run()
    finally:
        app.stop_event.set()
        if app.worker is not None:
            app.worker.join(timeout=3)


if __name__ == "__main__":
    main()
