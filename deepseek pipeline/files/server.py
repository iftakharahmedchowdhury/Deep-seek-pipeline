#!/usr/bin/env python3
"""Local tool bridge for browser chat UIs (DeepSeek etc.). Python stdlib only.

Run:
    BRIDGE_ROOT=/path/to/project python3 server.py

Helper Tools:
    reading   list_dir  read_file  read_multiple_files  get_file_info  search_files  search_content
    writing   write_file  append_file  replace_in_file  create_directory  move_file
    running   run  start_process  read_process_output  interact_with_process  kill_process  list_processes

Env vars:
    BRIDGE_ROOT           workspace root (default: current dir)
    BRIDGE_PORT           default 8765
    BRIDGE_TIMEOUT        default seconds per `run` command, default 60
    BRIDGE_TIMEOUT_MAX    largest timeout a `run` call may ask for, default 600
    BRIDGE_MAX_OUT        max chars returned to the AI, default 12000
    BRIDGE_ALLOW_OUTSIDE  set to 1 to let file tools leave BRIDGE_ROOT
    BRIDGE_WRITE_ALLOW    extra folders shell commands may write to (separate with ':')
    BRIDGE_REQUIRE_LANDLOCK  set to 1 to refuse shell commands when the kernel sandbox is unavailable
    BRIDGE_UNSAFE_SHELL   set to 1 to switch the shell sandbox/policy OFF (not recommended)
    BRIDGE_TOKEN          use this token instead of the one stored in files/.bridge_token
    BRIDGE_INSTRUCTIONS_MAX  max chars of project instructions sent to the AI, default 6000

Project instructions: if the workspace contains .deepseek/instructions.md, AGENTS.md or CLAUDE.md
(first one found) its text is added to the protocol prompt.

Shell safety (see bridge_guard.py): every shell command executes with no_new_privs and a Landlock
ruleset that makes the file system read-only except BRIDGE_ROOT, a private scratch dir and
BRIDGE_WRITE_ALLOW; sudo/su/docker/ssh/tmux/systemd-run... are refused with an explanation.
"""
import atexit
import codecs
import errno
import fnmatch
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
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge_guard as guard  # noqa: E402

VERSION = "0.5.0"

# Ordinary tool calls run one at a time, even if two browser tabs/instances send them together.
EXEC_LOCK = threading.Lock()

ROOT = Path(os.environ.get("BRIDGE_ROOT", os.getcwd())).resolve()
PORT = int(os.environ.get("BRIDGE_PORT", "8765"))
TIMEOUT = int(os.environ.get("BRIDGE_TIMEOUT", "60"))
TIMEOUT_MAX = max(TIMEOUT, int(os.environ.get("BRIDGE_TIMEOUT_MAX", "600")))
MAX_OUT = int(os.environ.get("BRIDGE_MAX_OUT", "12000"))
INSTR_MAX = int(os.environ.get("BRIDGE_INSTRUCTIONS_MAX", "6000"))
ALLOW_OUTSIDE = os.environ.get("BRIDGE_ALLOW_OUTSIDE") == "1"
UNSAFE_SHELL = os.environ.get("BRIDGE_UNSAFE_SHELL") == "1"
REQUIRE_LANDLOCK = os.environ.get("BRIDGE_REQUIRE_LANDLOCK") == "1"
MAX_FILE_BYTES = 8 * 1024 * 1024      # read_file refuses bigger files (use search_content / ranges)
PROC_MAX = 8                          # simultaneously running background processes
PROC_BUF_MAX = 1_000_000              # characters of output kept per background process
INSTRUCTION_FILES = [".deepseek/instructions.md", "AGENTS.md", "CLAUDE.md", ".bridge_instructions.md"]

# Shell sandbox: shell commands may only WRITE inside these folders (reading stays possible everywhere).
SCRATCH = Path(__file__).with_name(".scratch")          # private TMPDIR for commands (not /tmp)
SCRATCH.mkdir(exist_ok=True)
EXTRA_WRITE = [Path(x).expanduser() for x in os.environ.get("BRIDGE_WRITE_ALLOW", "").split(os.pathsep) if x]
WRITE_DIRS = [ROOT, SCRATCH.resolve()] + [d.resolve() for d in EXTRA_WRITE if d.exists()]
LANDLOCK = guard.landlock_abi() > 0
POLICY = guard.Policy(ROOT, WRITE_DIRS, SCRATCH, landlock=LANDLOCK)

TOKEN_FILE = Path(__file__).with_name(".bridge_token")
if os.environ.get("BRIDGE_TOKEN"):
    TOKEN = os.environ["BRIDGE_TOKEN"].strip()
elif TOKEN_FILE.exists() and TOKEN_FILE.read_text().strip():
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

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".idea", ".mypy_cache", ".pytest_cache",
             ".tox", ".cache", "dist", "build", ".scratch", ".gradle", ".next"}
KEEP_DOT_DIRS = {".github", ".gitlab", ".deepseek", ".vscode", ".devcontainer"}   # hidden folders worth searching


def keep_dir(name: str) -> bool:
    """Search skips build/cache/vendor folders and hidden folders (.git, .kilo worktrees, .idea ...)."""
    if name in SKIP_DIRS or name.endswith(".egg-info"):
        return False
    return not name.startswith(".") or name in KEEP_DOT_DIRS


BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".tar", ".xlsx", ".xls", ".woff",
              ".woff2", ".ttf", ".eot", ".so", ".pyc", ".class", ".jar", ".mp4", ".mp3", ".sqlite", ".db", ".bak",
              ".bin", ".dll", ".exe", ".o", ".7z", ".rar", ".docx", ".pptx", ".rptdesign"}


def clip(s: str) -> str:
    if len(s) <= MAX_OUT:
        return s
    half = MAX_OUT // 2
    return s[:half] + f"\n...[truncated {len(s) - MAX_OUT} chars]...\n" + s[-half:]


def clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def safe(p: str) -> Path:
    full = (ROOT / p).resolve()
    if not ALLOW_OUTSIDE and full != ROOT and ROOT not in full.parents:
        raise PermissionError(f"path is outside workspace root: {full}")
    return full


def rel(p: Path) -> str:
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


def backup(f: Path):
    if f.exists() and f.is_file():
        shutil.copy2(f, f.with_name(f.name + ".bak"))


def load_instructions():
    for name in INSTRUCTION_FILES:
        f = ROOT / name
        try:
            if f.is_file():
                text = f.read_text(errors="replace").strip()
                if text:
                    cut = len(text) > INSTR_MAX
                    return {"source": name, "text": text[:INSTR_MAX] + ("\n...[truncated]" if cut else "")}
        except OSError:
            pass
    return None


# ------------------------------------------------------------------------------------ file tools
def t_list_dir(a):
    d = safe(a.get("path", "."))
    guard.check_read_path(d)
    items = sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    lines = [f"{'d' if i.is_dir() else 'f'} {i.name}" for i in items[:500]]
    if len(items) > 500:
        lines.append(f"...[{len(items) - 500} more]")
    return "\n".join(lines) or "(empty)"


def _read_lines(f: Path):
    size = f.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(f"file is {size} bytes (limit {MAX_FILE_BYTES}); use search_content or run with head/sed")
    return f.read_text(errors="replace").splitlines()


def t_read_file(a):
    f = safe(a["path"])
    guard.check_read_path(f)
    lines = _read_lines(f)
    s = max(int(a.get("start", 1)), 1)
    e = min(int(a.get("end", len(lines))), len(lines))
    body = "\n".join(lines[s - 1:e])
    mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(f.stat().st_mtime))
    return f"# {f} lines {s}-{e} of {len(lines)} (file modified {mtime})\n{body}"


def t_read_multiple_files(a):
    paths = a.get("paths")
    if not isinstance(paths, list) or not paths:
        raise ValueError("'paths' must be a non-empty list")
    paths = paths[:12]
    per = max(int(a.get("max_lines", 250)), 1)
    out = []
    for p in paths:
        try:
            f = safe(p)
            guard.check_read_path(f)
            lines = _read_lines(f)
            body = "\n".join(lines[:per])
            more = f"\n...[{len(lines) - per} more lines - use read_file with start/end]" if len(lines) > per else ""
            out.append(f"===== {rel(f)} ({len(lines)} lines) =====\n{body}{more}")
        except Exception as e:  # one bad path must not hide the others
            out.append(f"===== {p} =====\nERROR {type(e).__name__}: {e}")
    return "\n\n".join(out)


def t_get_file_info(a):
    f = safe(a["path"])
    guard.check_read_path(f)
    if not f.exists():
        return f"{rel(f)}: does not exist"
    st = f.stat()
    kind = "directory" if f.is_dir() else "file"
    info = [f"path: {f}", f"type: {kind}", f"size: {st.st_size} bytes",
            f"modified: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(st.st_mtime))}",
            f"permissions: {oct(st.st_mode & 0o777)}"]
    if f.is_dir():
        info.append(f"entries: {sum(1 for _ in f.iterdir())}")
    elif st.st_size <= MAX_FILE_BYTES and f.suffix.lower() not in BINARY_EXT:
        try:
            info.append(f"lines: {len(f.read_text(errors='replace').splitlines())}")
        except OSError:
            pass
    return "\n".join(info)


def _iter_files(base: Path):
    if base.is_file():
        yield base
        return
    for dp, dn, fn in os.walk(base):
        dn[:] = sorted(d for d in dn if keep_dir(d))
        for name in sorted(fn):
            p = Path(dp) / name
            if p.suffix.lower() in BINARY_EXT or p.is_symlink():
                continue
            yield p


def t_search_files(a):
    """Find files/folders by name (glob, case-insensitive): *.py, *report*, models/*.py"""
    pat = a["pattern"]
    base = safe(a.get("path", "."))
    guard.check_read_path(base)
    limit = int(clamp(a.get("max", 200), 1, 500, 200))
    out = []
    for dp, dn, fn in os.walk(base):
        dn[:] = sorted(d for d in dn if keep_dir(d))
        for name in dn + sorted(fn):
            p = Path(dp) / name
            r = rel(p)
            target = r if "/" in pat else name
            if fnmatch.fnmatch(target.lower(), pat.lower()):
                out.append(("d " if p.is_dir() else "f ") + r)
                if len(out) >= limit:
                    return "\n".join(out) + f"\n...[stopped at max={limit}]"
    return "\n".join(out) or "(no matches)"


def t_search_content(a):
    """Search text inside files (like ripgrep). Literal by default; regex=true for a regular expression."""
    pat = a["pattern"]
    base = safe(a.get("path", "."))
    guard.check_read_path(base)
    limit = int(clamp(a.get("max", 100), 1, 300, 100))
    ctx = int(clamp(a.get("context", 0), 0, 3, 0))
    glob = a.get("glob")
    ic = a.get("ignore_case")
    if ic is None:
        ic = pat == pat.lower()   # smart case
    rx = re.compile(pat if a.get("regex") else re.escape(pat), re.I if ic else 0)
    hits, files_scanned, files_hit, out = 0, 0, 0, []
    for f in _iter_files(base):
        if guard.SENSITIVE_RE.search(str(f)):
            continue
        if glob and not (fnmatch.fnmatch(f.name, glob) or fnmatch.fnmatch(rel(f), glob)):
            continue
        try:
            if f.stat().st_size > 2_000_000:
                continue
            raw = f.read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:4096]:
            continue
        files_scanned += 1
        lines = raw.decode("utf-8", "replace").splitlines()
        file_hit = False
        for i, line in enumerate(lines):
            if rx.search(line):
                if not file_hit:
                    files_hit += 1
                    file_hit = True
                for j in range(max(0, i - ctx), min(len(lines), i + ctx + 1)):
                    mark = ":" if j == i else "-"
                    out.append(f"{rel(f)}{mark}{j + 1}{mark} {lines[j][:240]}")
                if ctx:
                    out.append("--")
                hits += 1
                if hits >= limit:
                    return f"# {hits}+ matches in {files_hit} files (stopped at max={limit}; narrow the pattern or path)\n" + "\n".join(out)
    return f"# {hits} matches in {files_hit} files ({files_scanned} files scanned)\n" + ("\n".join(out) if out else "(no matches)")


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
    if a.get("replace_all"):
        if n < 1:
            raise ValueError("'old' not found")
    elif n != 1:
        raise ValueError(f"'old' must occur exactly once, found {n} times (use replace_all:true to replace every occurrence)")
    backup(f)
    f.write_text(text.replace(a["old"], a["new"]))
    return f"replaced {n} occurrence{'s' if n != 1 else ''} in {f}"


def t_create_directory(a):
    d = safe(a["path"])
    d.mkdir(parents=True, exist_ok=True)
    return f"directory ready: {d}"


def t_move_file(a):
    src, dst = safe(a["src"]), safe(a["dst"])
    if not src.exists():
        raise FileNotFoundError(f"source does not exist: {src}")
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists() and not a.get("overwrite"):
        raise FileExistsError(f"destination exists: {dst} (pass overwrite:true to replace it)")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"moved {src} -> {dst}"


# ------------------------------------------------------------------------------------ shell tools
RUNNING = {}                 # id -> Popen of the `run` commands in flight (so End task can kill them)
RUN_LOCK = threading.Lock()
_run_seq = 0


def kill_group(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            p.kill()
        except OSError:
            pass


def _spawn(cmd, cwd, interactive):
    for p in DENY:
        if p.search(cmd):
            raise PermissionError(f"blocked by denylist: {p.pattern}")
    env = dict(os.environ, TMPDIR=str(SCRATCH), GIT_TERMINAL_PROMPT="0", PAGER="cat", GIT_PAGER="cat")
    if UNSAFE_SHELL:
        argv, use_shell = cmd, True
    else:
        POLICY.check(cmd, cwd)  # raises PolicyError with an explanation the AI can act on
        argv, use_shell = guard.launcher_argv(WRITE_DIRS, cmd, REQUIRE_LANDLOCK), False
    kw = dict(shell=use_shell, cwd=cwd, env=env, executable="/bin/bash" if use_shell else None,
              start_new_session=True)
    if interactive:
        return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kw)
    # stdin is closed: a command that waits for input gets EOF instead of grabbing the server's terminal
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, **kw)


def t_run(a):
    global _run_seq
    cmd = a["cmd"]
    cwd = safe(a.get("cwd", "."))
    timeout = int(clamp(a.get("timeout", TIMEOUT), 1, TIMEOUT_MAX, TIMEOUT))
    proc = _spawn(cmd, cwd, interactive=False)
    with RUN_LOCK:
        _run_seq += 1
        rid = _run_seq
        RUNNING[rid] = proc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc)
        out, err = proc.communicate()
        return f"TIMEOUT after {timeout}s (killed)\n--- stdout ---\n{out}\n--- stderr ---\n{err}"
    finally:
        with RUN_LOCK:
            RUNNING.pop(rid, None)
    if getattr(proc, "cancelled", False):
        return f"CANCELLED by the user (End task)\n--- stdout ---\n{out}\n--- stderr ---\n{err}"
    return f"exit={proc.returncode}\n--- stdout ---\n{out}\n--- stderr ---\n{err}"


class Managed:
    """A background process started with start_process: output is buffered, input can be sent."""

    def __init__(self, popen, cmd):
        self.p, self.cmd, self.t0 = popen, cmd, time.time()
        self.chunks, self.size, self.base, self.read_pos = [], 0, 0, 0
        self.cond = threading.Condition()
        self.done = False
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        fd = self.p.stdout.fileno()
        try:
            while True:
                data = os.read(fd, 4096)
                if not data:
                    break
                text = dec.decode(data)
                if not text:
                    continue
                with self.cond:
                    self.chunks.append(text)
                    self.size += len(text)
                    while self.size > PROC_BUF_MAX and len(self.chunks) > 1:
                        first = self.chunks.pop(0)
                        self.base += len(first)
                        self.size -= len(first)
                    self.cond.notify_all()
        except OSError:
            pass
        finally:
            try:
                self.p.wait()
            except Exception:
                pass
            with self.cond:
                self.done = True
                self.cond.notify_all()

    def total(self):
        return self.base + self.size

    def read_new(self, wait):
        with self.cond:
            end = time.time() + wait
            while self.total() <= self.read_pos and not self.done:
                left = end - time.time()
                if left <= 0:
                    break
                self.cond.wait(left)
            if self.total() > self.read_pos and not self.done and wait > 0:
                self.cond.wait(0.2)           # let a burst of output finish arriving
            text = "".join(self.chunks)
            start = max(self.read_pos - self.base, 0)
            new = text[start:]
            self.read_pos = self.total()
        return new

    def status(self):
        if self.done:
            rc = self.p.poll()
            return f"exited with code {rc}"
        return "running"


PROCS = {}
PROC_GUARD = threading.Lock()


def _proc_reply(m, new):
    tail = "" if new else "(no new output)\n"
    return f"[pid {m.p.pid} {m.status()}]\n{tail}{new}"


def _get_proc(a):
    pid = int(a["pid"])
    m = PROCS.get(pid)
    if not m:
        raise KeyError(f"no background process with pid {pid} (use list_processes)")
    return m


def t_start_process(a):
    cmd = a["cmd"]
    cwd = safe(a.get("cwd", "."))
    wait = clamp(a.get("wait", 2), 0, 30, 2)
    with PROC_GUARD:
        for pid in [k for k, v in PROCS.items() if v.done and time.time() - v.t0 > 600]:
            PROCS.pop(pid, None)
        if sum(1 for v in PROCS.values() if not v.done) >= PROC_MAX:
            raise RuntimeError(f"too many background processes (max {PROC_MAX}); kill_process one first")
        m = Managed(_spawn(cmd, cwd, interactive=True), cmd)
        PROCS[m.p.pid] = m
    return _proc_reply(m, m.read_new(wait))


def t_read_process_output(a):
    m = _get_proc(a)
    return _proc_reply(m, m.read_new(clamp(a.get("wait", 3), 0, 30, 3)))


def t_interact_with_process(a):
    m = _get_proc(a)
    if m.done:
        return _proc_reply(m, m.read_new(0))
    try:
        m.p.stdin.write((a["input"] + ("" if a.get("no_newline") else "\n")).encode())
        m.p.stdin.flush()
    except (BrokenPipeError, OSError) as e:
        raise RuntimeError(f"cannot write to process: {e}")
    return _proc_reply(m, m.read_new(clamp(a.get("wait", 3), 0, 30, 3)))


def t_kill_process(a):
    m = _get_proc(a)
    last = m.read_new(0)
    kill_group(m.p)
    time.sleep(0.2)
    return f"[pid {m.p.pid} killed]\n{last}"


def t_list_processes(a):
    if not PROCS:
        return "(no background processes)"
    rows = []
    for pid, m in PROCS.items():
        rows.append(f"pid {pid} | {m.status()} | {int(time.time() - m.t0)}s | {m.cmd[:100]}")
    return "\n".join(rows)


def cancel_all(processes=True):
    """End task: kill the `run` commands in flight and (optionally) every background process."""
    n = 0
    with RUN_LOCK:
        for p in list(RUNNING.values()):
            p.cancelled = True
            kill_group(p)
            n += 1
    if processes:
        for m in list(PROCS.values()):
            if not m.done:
                kill_group(m.p)
                n += 1
    return n


def _shutdown(*_):
    cancel_all(True)


atexit.register(_shutdown)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

TOOLS = {
    "list_dir": t_list_dir, "read_file": t_read_file, "read_multiple_files": t_read_multiple_files,
    "get_file_info": t_get_file_info, "search_files": t_search_files, "search_content": t_search_content,
    "write_file": t_write_file, "append_file": t_append_file, "replace_in_file": t_replace_in_file,
    "create_directory": t_create_directory, "move_file": t_move_file,
    "run": t_run, "start_process": t_start_process, "read_process_output": t_read_process_output,
    "interact_with_process": t_interact_with_process, "kill_process": t_kill_process,
    "list_processes": t_list_processes,
}
# These wait for output, so they must not hold the global lock (other calls and End task keep working).
UNLOCKED = {"read_process_output", "interact_with_process", "kill_process", "list_processes", "start_process"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

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
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > 16 * 1024 * 1024:
                return self._send(413, {"error": "request too large"})
            raw = self.rfile.read(n) if n else b""
        except Exception as e:
            return self._send(400, {"error": f"bad request: {e}"})

        if self.path == "/ping":   # no token needed: lets the extension tell "server down" from "bad token"
            return self._send(200, {"ok": True, "app": "deepseek-bridge", "version": VERSION})

        if not secrets.compare_digest(self.headers.get("X-Bridge-Token", ""), TOKEN):
            return self._send(401, {"error": "bad token"})
        try:
            body = json.loads(raw or b"{}")
        except Exception as e:
            return self._send(400, {"error": f"bad json: {e}"})

        if self.path == "/info":
            return self._send(200, {
                "root": str(ROOT), "os": platform.platform(), "sandbox": sandbox_status(), "version": VERSION,
                "instructions": load_instructions(), "tools": sorted(TOOLS),
            })

        if self.path == "/cancel":
            n = cancel_all(bool(body.get("processes", True)))
            print(f"{time.strftime('%H:%M:%S')} [cancel] stopped {n} running command(s)/process(es)")
            return self._send(200, {"ok": True, "stopped": n})

        if self.path == "/exec":
            tool = body.get("tool")
            fn = TOOLS.get(tool)
            if not fn:
                return self._send(200, {"ok": False, "error": f"unknown tool: {tool}"})
            try:
                if tool in UNLOCKED:
                    out = fn(body)
                else:
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


def bind_server():
    """Bind the port first; if it is taken, say WHO holds it and how to fix it (instead of a traceback)."""
    try:
        return ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        if e.errno != errno.EADDRINUSE:
            raise
        who = "another program"
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{PORT}/ping", data=b"{}", method="POST")
            info = json.loads(urllib.request.urlopen(req, timeout=2).read())
            if info.get("app") == "deepseek-bridge":
                who = f"a DeepSeek bridge server (v{info.get('version', '?')}) that is already running"
        except urllib.error.HTTPError:
            who = "an OLDER bridge server (v0.4 or earlier) or another program (it answered 'bad token')"
        except Exception:
            pass
        print(f"\nCannot start: port {PORT} is already in use by {who}.\n"
              f"  - Stop it with Ctrl+C in the terminal where it runs, or find it with:\n"
              f"        ss -ltnp | grep :{PORT}        then:  kill <pid>\n"
              f"  - Or run this one on another port:  BRIDGE_PORT=8766 python3 server.py\n"
              f"    (then set the same port in the extension - see README, section Configuration)\n", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    httpd = bind_server()
    ins = load_instructions()
    print(f"DeepSeek Local Bridge server v{VERSION}")
    print(f"Bridge root : {ROOT}")
    print(f"Shell safety: {sandbox_status()}")
    print(f"Instructions: {ins['source'] if ins else '(none - add AGENTS.md or .deepseek/instructions.md to give the AI project rules)'}")
    print(f"Listening   : http://127.0.0.1:{PORT}")
    print(f"Token       : {TOKEN}   (also in {TOKEN_FILE})", flush=True)
    httpd.serve_forever()
