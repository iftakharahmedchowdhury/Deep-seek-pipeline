#!/usr/bin/env python3
"""Local tool bridge for browser chat UIs (DeepSeek etc.). Python stdlib only.

Run:
    BRIDGE_ROOT=/path/to/project python3 server.py

Env vars:
    BRIDGE_ROOT           workspace root (default: current dir)
    BRIDGE_PORT           default 8765
    BRIDGE_TIMEOUT        seconds per command, default 60
    BRIDGE_MAX_OUT        max chars returned to the AI, default 12000
    BRIDGE_ALLOW_OUTSIDE  set to 1 to let file tools leave BRIDGE_ROOT
    BRIDGE_WRITE_ALLOW    extra folders `run` commands may write to (separate with ':')
    BRIDGE_REQUIRE_LANDLOCK  set to 1 to refuse `run` when the kernel sandbox is unavailable
    BRIDGE_UNSAFE_SHELL   set to 1 to switch the shell sandbox/policy OFF (not recommended)

Shell safety (see bridge_guard.py): every `run` command executes with no_new_privs and a Landlock
ruleset that makes the file system read-only except BRIDGE_ROOT, a private scratch dir and
BRIDGE_WRITE_ALLOW; sudo/su/docker/ssh/tmux/systemd-run... are refused with an explanation.
"""
import json
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge_guard as guard  # noqa: E402

# Tool calls run one at a time, even if two browser tabs/instances send them together.
EXEC_LOCK = threading.Lock()

ROOT = Path(os.environ.get("BRIDGE_ROOT", os.getcwd())).resolve()
PORT = int(os.environ.get("BRIDGE_PORT", "8765"))
TIMEOUT = int(os.environ.get("BRIDGE_TIMEOUT", "60"))
MAX_OUT = int(os.environ.get("BRIDGE_MAX_OUT", "12000"))
ALLOW_OUTSIDE = os.environ.get("BRIDGE_ALLOW_OUTSIDE") == "1"
UNSAFE_SHELL = os.environ.get("BRIDGE_UNSAFE_SHELL") == "1"
REQUIRE_LANDLOCK = os.environ.get("BRIDGE_REQUIRE_LANDLOCK") == "1"

# Shell sandbox: `run` may only WRITE inside these folders (reading stays possible everywhere).
SCRATCH = Path(__file__).with_name(".scratch")          # private TMPDIR for commands (not /tmp)
SCRATCH.mkdir(exist_ok=True)
EXTRA_WRITE = [Path(x).expanduser() for x in os.environ.get("BRIDGE_WRITE_ALLOW", "").split(os.pathsep) if x]
WRITE_DIRS = [ROOT, SCRATCH.resolve()] + [d.resolve() for d in EXTRA_WRITE if d.exists()]
LANDLOCK = guard.landlock_abi() > 0
POLICY = guard.Policy(ROOT, WRITE_DIRS, SCRATCH, landlock=LANDLOCK)

TOKEN_FILE = Path(__file__).with_name(".bridge_token")
if TOKEN_FILE.exists():
    TOKEN = TOKEN_FILE.read_text().strip()
else:
    TOKEN = secrets.token_urlsafe(18)
    TOKEN_FILE.write_text(TOKEN)
    TOKEN_FILE.chmod(0o600)

# Best-effort guard only. A shell command can always do more than a denylist
# can foresee, so keep the extension's "ask before run/write" mode ON.
DENY = [re.compile(p) for p in (
    r"\brm\s+(-\w+\s+)*(/|~|\$HOME)(\s|$)",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r":\(\)\s*\{",
    r"\b(shutdown|reboot|poweroff)\b",
    r"\bchmod\s+-R\s+777\s+/",
    r"(curl|wget)[^|]*\|\s*(ba)?sh",
)]


def clip(s: str) -> str:
    if len(s) <= MAX_OUT:
        return s
    half = MAX_OUT // 2
    return s[:half] + f"\n...[truncated {len(s) - MAX_OUT} chars]...\n" + s[-half:]


def safe(p: str) -> Path:
    full = (ROOT / p).resolve()
    if not ALLOW_OUTSIDE and full != ROOT and ROOT not in full.parents:
        raise PermissionError(f"path is outside workspace root: {full}")
    return full


def backup(f: Path):
    if f.exists():
        shutil.copy2(f, f.with_name(f.name + ".bak"))


def t_list_dir(a):
    d = safe(a.get("path", "."))
    guard.check_read_path(d)
    items = sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    lines = [f"{'d' if i.is_dir() else 'f'} {i.name}" for i in items[:500]]
    if len(items) > 500:
        lines.append(f"...[{len(items) - 500} more]")
    return "\n".join(lines) or "(empty)"


def t_read_file(a):
    f = safe(a["path"])
    guard.check_read_path(f)
    lines = f.read_text(errors="replace").splitlines()
    s = max(int(a.get("start", 1)), 1)
    e = min(int(a.get("end", len(lines))), len(lines))
    body = "\n".join(lines[s - 1:e])
    mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.stat().st_mtime))
    return f"# {f} lines {s}-{e} of {len(lines)} (file modified {mtime})\n{body}"


def t_run(a):
    cmd = a["cmd"]
    for p in DENY:
        if p.search(cmd):
            raise PermissionError(f"blocked by denylist: {p.pattern}")
    cwd = safe(a.get("cwd", "."))
    env = dict(os.environ, TMPDIR=str(SCRATCH))
    if UNSAFE_SHELL:
        argv, use_shell = cmd, True
    else:
        POLICY.check(cmd, cwd)  # raises PolicyError with an explanation the AI can act on
        argv, use_shell = guard.launcher_argv(WRITE_DIRS, cmd, REQUIRE_LANDLOCK), False
    proc = subprocess.Popen(
        argv, shell=use_shell, cwd=cwd, env=env, executable="/bin/bash" if use_shell else None,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, err = proc.communicate()
        return f"TIMEOUT after {TIMEOUT}s (killed)\n--- stdout ---\n{out}\n--- stderr ---\n{err}"
    return f"exit={proc.returncode}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"


def t_write_file(a):
    f = safe(a["path"])
    f.parent.mkdir(parents=True, exist_ok=True)
    backup(f)
    f.write_text(a["content"])
    return f"wrote {len(a['content'])} chars to {f}"


def t_append_file(a):
    f = safe(a["path"])
    f.parent.mkdir(parents=True, exist_ok=True)
    if f.exists():
        backup(f)
    with f.open("a") as fh:
        fh.write(a["content"])
    return f"appended {len(a['content'])} chars to {f}"


def t_replace_in_file(a):
    f = safe(a["path"])
    text = f.read_text()
    n = text.count(a["old"])
    if n != 1:
        raise ValueError(f"'old' must occur exactly once, found {n} times")
    backup(f)
    f.write_text(text.replace(a["old"], a["new"]))
    return f"replaced 1 occurrence in {f}"


TOOLS = {
    "list_dir": t_list_dir,
    "read_file": t_read_file,
    "run": t_run,
    "write_file": t_write_file,
    "append_file": t_append_file,
    "replace_in_file": t_replace_in_file,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        # Requests come from the extension's background worker (chrome-extension://).
        # Refuse anything a normal web page could send.
        if self.headers.get("Origin", "").startswith("http"):
            return self._send(403, {"error": "web origins not allowed"})
        if not secrets.compare_digest(self.headers.get("X-Bridge-Token", ""), TOKEN):
            return self._send(401, {"error": "bad token"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._send(400, {"error": f"bad json: {e}"})

        if self.path == "/info":
            return self._send(200, {"root": str(ROOT), "os": platform.platform(), "sandbox": sandbox_status()})

        if self.path == "/exec":
            tool = body.get("tool")
            fn = TOOLS.get(tool)
            if not fn:
                return self._send(200, {"ok": False, "error": f"unknown tool: {tool}"})
            try:
                with EXEC_LOCK:
                    out = fn(body)
                print(f"{time.strftime('%H:%M:%S')} [ok]   {tool}: {str(body)[:150]}")
                return self._send(200, {"ok": True, "output": clip(out)})
            except Exception as e:
                print(f"{time.strftime('%H:%M:%S')} [fail] {tool}: {e}")
                return self._send(200, {"ok": False, "error": f"{type(e).__name__}: {e}"})

        self._send(404, {"error": "not found"})


def sandbox_status():
    if UNSAFE_SHELL:
        return "OFF (BRIDGE_UNSAFE_SHELL=1)"
    kernel = "Landlock enforced (shell can only write to the allowed folders)" if LANDLOCK else \
        "Landlock UNAVAILABLE - text policy only, shell writes are not kernel-limited"
    return f"no_new_privs + {kernel}; writable: " + ", ".join(str(d) for d in WRITE_DIRS)


if __name__ == "__main__":
    print(f"Bridge root : {ROOT}")
    print(f"Shell safety: {sandbox_status()}")
    print(f"Listening   : http://127.0.0.1:{PORT}")
    print(f"Token       : {TOKEN}   (also in {TOKEN_FILE})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
