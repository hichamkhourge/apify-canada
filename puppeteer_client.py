"""Python client for the Puppeteer checkout worker (browser/checkout.js).

The worker is a long-lived Node process speaking JSON lines: one command per
line on its stdin, one reply per line on its stdout. Its stderr (the browser
step's log) is streamed straight into this process's stdout so the run log
reads the same as when the browser was driven from Python.

Errors reported by the worker are raised as ``WorkerError`` carrying the
worker's ``type`` (``cloudflare_blocked``, ``checkout_not_loaded``,
``network_error``, ...) and ``diag`` dict; iptvvcanada_automation maps them
onto its own exception types.
"""
import itertools
import json
import os
import queue
import shutil
import subprocess
import sys
import threading

BROWSER_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser")
WORKER_SCRIPT = os.path.join(BROWSER_DIR, "checkout.js")

# Env vars forwarded to the worker (it reads its config from the environment).
_FORWARDED_ENV = (
    "IPTVV_BASE_URL",
    "IPTVV_CART_URL",
    "IPTVV_TRIAL_PRODUCT_ID",
    "IPTVV_PROXY_CHECK_URL",
    "IPTVV_CLOUDFLARE_WAIT_SECONDS",
    "IPTVV_PAGE_LOAD_RETRIES",
    "IPTVV_DEBUG_DIR",
    "HEADLESS",
    "CHROME_BINARY",
)

# Generous per-command ceilings; the worker enforces its own finer timeouts.
DEFAULT_TIMEOUT = 300


class WorkerError(RuntimeError):
    """An error reported by (or about) the Puppeteer worker."""

    def __init__(self, error_type, message, diag=None):
        super().__init__(message)
        self.type = error_type
        self.diag = diag or {}


class PuppeteerCheckout:
    """Drive the IPTVV checkout through the Node/Puppeteer worker."""

    def __init__(self, proxy_server=None, env_overrides=None):
        self.proxy_server = proxy_server
        self.env_overrides = env_overrides or {}
        self._proc = None
        self._replies = queue.Queue()
        self._ids = itertools.count(1)

    # ── process lifecycle ──────────────────────────────────────────────
    def start(self):
        node = shutil.which(os.getenv("NODE_BINARY", "node"))
        if not node:
            raise WorkerError("launch_failed", "Node.js not found on PATH (needed for the Puppeteer checkout)")
        if not os.path.isdir(os.path.join(BROWSER_DIR, "node_modules")):
            raise WorkerError(
                "launch_failed",
                f"Puppeteer dependencies missing; run `npm ci` in {BROWSER_DIR}",
            )

        env = {k: v for k, v in os.environ.items()}
        for key in _FORWARDED_ENV:
            if key in self.env_overrides:
                env[key] = str(self.env_overrides[key])
        env["BROWSER_PROXY_SERVER"] = self.proxy_server or ""

        self._proc = subprocess.Popen(
            [node, WORKER_SCRIPT],
            cwd=BROWSER_DIR,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        return self.call("launch", timeout=120)

    def _pump_stdout(self):
        for line in self._proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._replies.put(json.loads(line))
            except ValueError:
                # Stray non-JSON output (a dependency printing to stdout): just log it.
                print(f"[browser] {line}", flush=True)
        self._replies.put(None)  # EOF sentinel: worker exited.

    def _pump_stderr(self):
        for line in self._proc.stderr:
            sys.stdout.write(line)
            sys.stdout.flush()

    def close(self):
        if not self._proc:
            return
        try:
            if self._proc.poll() is None:
                try:
                    self.call("close", timeout=20)
                except Exception:
                    pass
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
                try:
                    self._proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        finally:
            self._proc = None

    # Selenium-compatible alias so existing cleanup code (driver.quit()) keeps working.
    quit = close

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ── RPC ────────────────────────────────────────────────────────────
    def call(self, cmd, timeout=DEFAULT_TIMEOUT, **args):
        if not self._proc or self._proc.poll() is not None:
            raise WorkerError("worker_dead", f"Puppeteer worker is not running (command: {cmd})")
        msg_id = next(self._ids)
        try:
            self._proc.stdin.write(json.dumps({"id": msg_id, "cmd": cmd, "args": args}) + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise WorkerError("worker_dead", f"Puppeteer worker pipe closed: {exc}")

        while True:
            try:
                reply = self._replies.get(timeout=timeout)
            except queue.Empty:
                raise WorkerError("timeout", f"Puppeteer worker did not answer '{cmd}' within {timeout}s")
            if reply is None:
                raise WorkerError("worker_dead", f"Puppeteer worker exited while running '{cmd}'")
            if reply.get("id") != msg_id:
                continue  # Stale reply from an earlier timed-out command.
            if reply.get("ok"):
                return reply.get("result")
            err = reply.get("error") or {}
            raise WorkerError(err.get("type", "error"), err.get("message", "unknown worker error"), err.get("diag"))

    # ── typed commands ─────────────────────────────────────────────────
    def browser_ip(self):
        return self.call("browser_ip", timeout=90)

    def open_trial_checkout(self):
        return self.call("open_trial_checkout")

    def preflight(self):
        return self.call("preflight")

    def fill(self, email, user_data=None):
        return self.call("fill", email=email, user_data=user_data or {})

    def captcha_info(self):
        return self.call("captcha_info", timeout=30)

    def submit(self, token=None):
        return self.call("submit", token=token)
