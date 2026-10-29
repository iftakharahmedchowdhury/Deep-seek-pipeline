#!/usr/bin/env python3
"""Safety layer for the DeepSeek local bridge (python stdlib only).

Two layers protect the PC from whatever the AI runs through the `run` tool:

1. KERNEL layer - cannot be talked around.  Every shell command is started through this file as a
   launcher, which first sets PR_SET_NO_NEW_PRIVS (sudo / su / pkexec / setuid binaries can no longer
   gain privileges) and then applies a Landlock ruleset (Linux >= 5.13) that makes the whole file
   system read-only except the workspace root, a scratch dir and BRIDGE_WRITE_ALLOW dirs.  Child
   processes, python one-liners, scripts - everything inherits it.

2. POLICY layer - early, explained refusals.  A static scan of the command text refuses privilege
   escalation tools, escapes to daemons that run outside the sandbox, reverse shells, credential
   reads and writes/execution outside the allowed folders.  Shell is too flexible to analyse
   perfectly, so this layer is best effort; layer 1 is the real boundary (when available).
"""
import ctypes
import json
import os
import re
import sys

# =============================================================================== kernel layer
_PR_SET_NO_NEW_PRIVS = 38
_SYS_CREATE, _SYS_ADD, _SYS_RESTRICT = 444, 445, 446      # same numbers on x86_64 and arm64
_RULE_PATH_BENEATH = 1
_FS = {  # Landlock access rights we handle (only write-ish ones: reading stays unrestricted)
    "WRITE_FILE": 1 << 1, "REMOVE_DIR": 1 << 4, "REMOVE_FILE": 1 << 5, "MAKE_CHAR": 1 << 6,
    "MAKE_DIR": 1 << 7, "MAKE_REG": 1 << 8, "MAKE_SOCK": 1 << 9, "MAKE_FIFO": 1 << 10,
    "MAKE_BLOCK": 1 << 11, "MAKE_SYM": 1 << 12, "REFER": 1 << 13, "TRUNCATE": 1 << 14,
}
DEVICE_FILES = ("/dev/null", "/dev/zero", "/dev/full", "/dev/tty", "/dev/urandom")


def _libc():
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def landlock_abi():
    """Landlock ABI version supported by this kernel, 0 if unavailable (always 0 off Linux)."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        r = _libc().syscall(_SYS_CREATE, None, ctypes.c_size_t(0), ctypes.c_uint32(1))
        return int(r) if r > 0 else 0
    except Exception:
        return 0


def _handled_mask(abi):
    m = 0
    for k in ("WRITE_FILE", "REMOVE_DIR", "REMOVE_FILE", "MAKE_CHAR", "MAKE_DIR", "MAKE_REG",
              "MAKE_SOCK", "MAKE_FIFO", "MAKE_BLOCK", "MAKE_SYM"):
        m |= _FS[k]
    if abi >= 2:
        m |= _FS["REFER"]
    if abi >= 3:
        m |= _FS["TRUNCATE"]
    return m


def apply_kernel_limits(write_dirs):
    """Run inside the launcher process. Returns (no_new_privs_ok, landlock_state) where state is
    'enforced' | 'unavailable' | 'failed'."""
    libc = _libc()
    nnp = libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) == 0
    abi = landlock_abi()
    if abi <= 0:
        return nnp, "unavailable"
    handled = _handled_mask(abi)
    attr = ctypes.c_uint64(handled)
    fd = libc.syscall(_SYS_CREATE, ctypes.byref(attr), ctypes.c_size_t(8), ctypes.c_uint32(0))
    if fd < 0:
        return nnp, "unavailable"

    class PathBeneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    def allow(path, access):
        try:
            pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        except OSError:
            return
        try:
            rule = PathBeneath(access, pfd)
            libc.syscall(_SYS_ADD, ctypes.c_int(fd), ctypes.c_int(_RULE_PATH_BENEATH),
                         ctypes.byref(rule), ctypes.c_uint32(0))
        finally:
            os.close(pfd)

    for d in write_dirs:
        allow(d, handled)
    file_rights = _FS["WRITE_FILE"] | (_FS["TRUNCATE"] if abi >= 3 else 0)
    for f in DEVICE_FILES:
        allow(f, file_rights)
    r = libc.syscall(_SYS_RESTRICT, ctypes.c_int(fd), ctypes.c_uint32(0))
    os.close(fd)
    return nnp, ("enforced" if r == 0 else "failed")


def kernel_status():
    return {"landlock_abi": landlock_abi(), "landlock": landlock_abi() > 0}


def launcher_argv(write_dirs, cmd, require_landlock=False):
    """argv that runs `cmd` under bash inside the kernel sandbox."""
    cfg = json.dumps({"write_dirs": [str(d) for d in write_dirs], "require": bool(require_landlock)})
    return [sys.executable, "-I", os.path.abspath(__file__), "--exec", cfg, cmd]


def _launcher_main(argv):
    cfg = json.loads(argv[0])
    cmd = argv[1]
    if sys.platform.startswith("linux"):
        nnp, ll = apply_kernel_limits(cfg["write_dirs"])
        if not nnp:
            sys.stderr.write("bridge sandbox: could not set no_new_privs - refusing to run\n")
            os._exit(126)
        if ll == "failed" or (cfg.get("require") and ll != "enforced"):
            sys.stderr.write("bridge sandbox: Landlock not enforced (%s) - refusing to run\n" % ll)
            os._exit(126)
    elif cfg.get("require"):
        sys.stderr.write("bridge sandbox: the kernel sandbox needs Linux - refusing to run\n")
        os._exit(126)
    os.execv("/bin/bash", ["bash", "-c", cmd])


# =============================================================================== policy layer
class PolicyError(Exception):
    pass


HINT = (" Do NOT try to get around this with another command, script, symlink or tool. Tell the "
        "user exactly what you need and end your reply with NEED_USER_INPUT.")


def deny(reason):
    raise PolicyError("blocked by bridge policy: %s.%s" % (reason, HINT))


# Local privilege escalation, system administration, and ways to reach daemons / sessions that run
# OUTSIDE the sandbox (systemd --user, tmux server, docker daemon, ssh to this very machine ...).
PRIV_CMDS = {
    "sudo", "sudoedit", "su", "doas", "pkexec", "runuser", "run0", "setcap", "capsh", "visudo", "vipw",
    "vigr", "passwd", "chpasswd", "chsh", "chfn", "newgrp", "sg", "useradd", "userdel", "usermod",
    "groupadd", "groupdel", "groupmod", "adduser", "deluser", "addgroup", "delgroup",
    "mount", "umount", "nsenter", "unshare", "chroot", "pivot_root", "losetup", "cryptsetup",
    "fdisk", "sfdisk", "parted", "mkswap", "swapon", "swapoff", "modprobe", "insmod", "rmmod",
    "iptables", "ip6tables", "nft", "ufw", "firewall-cmd", "sysctl", "setenforce", "apparmor_parser",
    "aa-exec", "aa-complain", "aa-enforce", "shutdown", "reboot", "poweroff", "halt", "init",
    "telinit", "kexec", "gdb", "ltrace",
    "docker", "podman", "nerdctl", "lxc", "lxd", "ctr", "crictl",                       # root daemons
    "systemctl", "service", "systemd-run", "machinectl", "loginctl", "busctl", "gdbus", "dbus-send",
    "dbus-launch", "crontab", "at", "batch",                                           # run later/elsewhere
    "tmux", "screen", "gnome-terminal", "konsole", "x-terminal-emulator", "xdg-terminal-exec",
    "ssh", "mosh", "rsh", "rlogin", "telnet", "sftp", "scp",                            # other shells
}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "ash"}
INTERPRETERS = {"python", "python2", "python3", "pypy", "pypy3", "perl", "ruby", "node", "nodejs",
                "php", "lua", "deno", "bun", "Rscript", "awk", "gawk", "mawk"} | SHELLS
WRAPPERS = {"env", "nohup", "exec", "command", "builtin", "time", "nice", "ionice", "timeout", "setsid",
            "stdbuf", "xargs", "chrt", "taskset", "unbuffer", "watch", "strace", "script"}
_WRAP_VAL_FLAGS = {
    "env": {"-u", "-C", "-S", "--unset", "--chdir"},
    "xargs": {"-n", "-I", "-P", "-d", "-E", "-L", "-s", "-a", "--max-args", "--replace", "--delimiter", "--arg-file"},
    "nice": {"-n", "--adjustment"}, "ionice": {"-c", "-n", "-p"}, "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "stdbuf": {"-i", "-o", "-e"}, "time": {"-f", "-o"}, "taskset": {"-c"}, "watch": {"-n", "-d"},
    "strace": {"-o", "-e", "-p", "-s", "-u"}, "script": {"-c", "-t", "-T", "-I", "-O", "-B", "-E", "-o", "-e"},
}
KEYWORDS = {"{", "}", "!", "if", "then", "else", "elif", "fi", "do", "done", "while", "until"}
SKIP_SEGMENT = {"for", "case", "select", "function", "esac", "in"}
OK_DEV_TARGETS = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/zero", "/dev/full"}
EXEC_DENY_DIRS = ("/tmp", "/var/tmp", "/dev/shm")
GIT_WRITE = {"clone", "init", "checkout", "restore", "reset", "clean", "stash", "pull", "merge", "rebase",
             "commit", "add", "rm", "mv", "apply", "am", "cherry-pick", "revert", "switch", "worktree"}

SENSITIVE_RE = re.compile(
    r"(\.ssh(/|\b)|\.aws\b|\.gnupg\b|\.kube\b|\.docker/config|/etc/(shadow|gshadow|sudoers)|"
    r"\bid_(rsa|dsa|ecdsa|ed25519)\b|\.netrc\b|\.pgpass\b|"
    r"\.config/(google-chrome|chromium|BraveSoftware|microsoft-edge)|\.mozilla\b|Login Data|cookies\.sqlite|"
    r"logins\.json|key4\.db|/proc/[^/\s]+/(environ|mem)\b|/proc/self/(environ|mem)\b)", re.I)
REVERSE_SHELL_RE = re.compile(r"/dev/(tcp|udp)/|\b(nc|ncat|netcat)\b[^;|&\n]*\s-[a-zA-Z]*[ec]\b|\bsocat\b[^;|&\n]*\bexec:", re.I)
PIPE_TO_SHELL_RE = re.compile(
    r"\b(curl|wget|fetch|base64|xxd|openssl\s+enc|nc|ncat)\b[^;&\n]*\|\s*(sudo\s+)?"
    r"(ba|z|da|k)?sh\b|\b(curl|wget)\b[^;&\n]*\|\s*(sudo\s+)?python[0-9.]*\b|<\(\s*(curl|wget)\b", re.I)
WRITE_HINT_RE = re.compile(
    r"open\s*\([^)]*['\"][^'\"]*[wax+][^'\"]*['\"]|write_text|write_bytes|\.write\(|\bshutil\.|"
    r"os\.(remove|unlink|rename|replace|makedirs|mkdir|chmod|chown|symlink|link|truncate|rmdir)|rmtree|"
    r"\bunlink|fs\.(write|append|mkdir|rm|unlink|rename|copy)\w*|File\.(write|open)|file_put_contents|fopen|"
    r"copyfile|print\s*[^;}]*>\s*[\"']|>\s*[\"']?/")
INLINE_ESCALATE_RE = re.compile(r"\b(os\.system|subprocess|Popen|popen|exec[lv]p?e?|spawn|system)\b[^\n]{0,200}\b(sudo|su|pkexec|doas|systemd-run|tmux|ssh)\b")
SETUID_MODE_RE = re.compile(r"^([ugoa]*[+=][rwxXt]*s[rwxXst]*|[2467][0-7]{3})$")


class Tok:
    __slots__ = ("val", "op", "dyn")

    def __init__(self, val, op=False, dyn=False):
        self.val, self.op, self.dyn = val, op, dyn


class State:
    def __init__(self, cwd):
        self.cwd = cwd           # str path or None (unknown)
        self.stdin_code = False  # an interpreter will read code from stdin / heredoc

    def copy(self):
        s = State(self.cwd)
        return s


_REDIR_OPS = ("&>>", "<<<", "&>", ">>", ">|", ">&", "<<", "<&", "<", ">")


def tokenize(seg):
    toks, cur, has, dyn, q = [], [], False, False, None
    i, n = 0, len(seg)

    def flush():
        nonlocal cur, has, dyn
        if has or cur:
            toks.append(Tok("".join(cur), False, dyn))
        cur, has, dyn = [], False, False

    while i < n:
        c = seg[i]
        if q == "'":
            if c == "'":
                q = None
            else:
                cur.append(c)
            i += 1
            continue
        if q == '"':
            if c == '"':
                q = None
            elif c == "\\" and i + 1 < n and seg[i + 1] in '"\\$`':
                cur.append(seg[i + 1])
                i += 1
            else:
                if c in "$`":
                    dyn = True
                cur.append(c)
            i += 1
            continue
        if c in "'\"":
            q, has = c, True
            i += 1
            continue
        if c == "\\":
            if i + 1 < n:
                cur.append(seg[i + 1])
                has = True
            i += 2
            continue
        if c.isspace():
            flush()
            i += 1
            continue
        if c in "<>" or (c == "&" and seg[i + 1:i + 2] == ">"):
            if cur and "".join(cur).isdigit() and not has:
                cur, has, dyn = [], False, False       # fd prefix like 2>
            else:
                flush()
            for op in _REDIR_OPS:
                if seg.startswith(op, i):
                    toks.append(Tok(op, True))
                    i += len(op)
                    break
            continue
        if c in "$`":
            dyn = True
        cur.append(c)
        i += 1
    flush()
    if q:
        deny("could not parse the command safely (unbalanced quote)")
    return toks


def split_segments(text):
    segs, cur, q = [], [], None
    i, n = 0, len(text)
    prev = "\n"

    def cut():
        nonlocal cur
        segs.append("".join(cur))
        cur = []

    while i < n:
        c = text[i]
        if q == "'":
            cur.append(c)
            if c == "'":
                q = None
            i += 1
            continue
        if q == '"':
            cur.append(c)
            if c == "\\" and i + 1 < n:
                cur.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                q = None
            i += 1
            continue
        if c in "'\"":
            q = c
            cur.append(c)
            i += 1
            prev = c
            continue
        if c == "\\" and i + 1 < n:
            cur.append(c)
            cur.append(text[i + 1])
            i += 2
            prev = "x"
            continue
        if c == "#" and (prev.isspace() or prev in ";&|()"):
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c in ";\n()":
            cut()
            i += 1
        elif c == "|":
            cut()
            i += 2 if text[i + 1:i + 2] in ("|", "&") else 1
        elif c == "&":
            nxt = text[i + 1:i + 2]
            if nxt == "&":
                cut()
                i += 2
            elif nxt == ">" or prev in "<>":
                cur.append(c)
                i += 1
            else:
                cut()
                i += 1
        else:
            cur.append(c)
            i += 1
        prev = text[i - 1] if i - 1 < n else "\n"
    if q:
        deny("could not parse the command safely (unbalanced quote)")
    cut()
    return [s for s in segs if s.strip()]


def strip_heredocs(cmd):
    """(text without heredoc bodies, [bodies]).  Bodies are DATA unless an interpreter reads them."""
    lines, out, bodies, i = cmd.split("\n"), [], [], 0
    pat = re.compile(r"<<(?!<)-?\s*(?:(['\"])(\w+)\1|(\w+))")
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        terms = [(m.group(2) or m.group(3)) for m in pat.finditer(line)]
        for t in terms:
            body = []
            while i < len(lines) and lines[i].strip() != t:
                body.append(lines[i])
                i += 1
            i += 1  # terminator
            bodies.append("\n".join(body))
    return "\n".join(out), bodies


def extract_substitutions(text):
    """Inner text of $(...) and `...` (also inside double quotes), ignoring single-quoted parts."""
    masked, q, i = [], None, 0
    while i < len(text):
        c = text[i]
        if q == "'":
            masked.append(" " if c != "'" else c)
            if c == "'":
                q = None
        elif q == '"':
            masked.append(c)
            if c == "\\" and i + 1 < len(text):
                masked.append(text[i + 1])
                i += 1
            elif c == '"':
                q = None
        elif c in "'\"":
            q = c
            masked.append(c)
        else:
            masked.append(c)
        i += 1
    m = "".join(masked)
    inners, i = [], 0
    while i < len(m):
        if m.startswith("$(", i) and not m.startswith("$((", i):
            depth, j = 1, i + 2
            while j < len(m) and depth:
                depth += (m[j] == "(") - (m[j] == ")")
                j += 1
            inners.append(text[i + 2:j - 1])
            i = j
        elif m[i] == "`":
            j = m.find("`", i + 1)
            if j == -1:
                break
            inners.append(text[i + 1:j])
            i = j + 1
        else:
            i += 1
    return inners


class Policy:
    def __init__(self, root, write_dirs, scratch, home=None, landlock=False):
        rp = os.path.realpath
        self.root = rp(str(root))
        self.write_dirs = [rp(str(d)) for d in write_dirs]
        self.scratch = rp(str(scratch))
        self.home = rp(home or os.path.expanduser("~"))
        self.strict = not landlock     # without the kernel layer, unverifiable paths are refused

    # ------------------------------------------------------------------ path helpers
    def resolve(self, p, cwd):
        """Absolute path for a (possibly relative / ~ / $HOME) word, or None if it cannot be known."""
        for var, val in (("$HOME", self.home), ("${HOME}", self.home), ("$TMPDIR", self.scratch),
                         ("${TMPDIR}", self.scratch), ("$PWD", cwd), ("${PWD}", cwd)):
            if p == var or p.startswith(var + "/"):
                if val is None:
                    return None
                p = val + p[len(var):]
                break
        if p == "~" or p.startswith("~/"):
            p = self.home + p[1:]
        if "$" in p or "`" in p or p.startswith("~"):
            return None
        if not os.path.isabs(p):
            if cwd is None:
                return None
            p = os.path.join(cwd, p)
        return os.path.realpath(p)

    def inside(self, path, dirs=None):
        for d in (dirs or self.write_dirs):
            if path == d or path.startswith(d.rstrip("/") + "/"):
                return True
        return False

    def _target(self, tok, st, why):
        p = tok.val if isinstance(tok, Tok) else tok
        if p in OK_DEV_TARGETS or p.startswith("/dev/fd/"):
            return
        r = self.resolve(p, st.cwd)
        if r is None:
            if self.strict:
                deny("%s target '%s' uses a variable / unknown folder and cannot be verified" % (why, p))
            return
        if not self.inside(r):
            deny("%s to %s is outside the allowed folders (workspace %s). Writing there is not permitted"
                 % (why, r, self.root))

    # ------------------------------------------------------------------ entry point
    def check(self, cmd, cwd):
        text, bodies = strip_heredocs(cmd)
        st = State(os.path.realpath(str(cwd)) if cwd else None)
        self._scan(text, st, 0)
        if st.stdin_code:
            for b in bodies:
                self.scan_inline(b, st)

    def _scan(self, text, st, depth):
        if depth > 5:
            deny("command nesting is too deep to verify")
        text = text.replace("\\\n", " ")
        if REVERSE_SHELL_RE.search(text):
            deny("reverse-shell / raw network shell pattern")
        if PIPE_TO_SHELL_RE.search(text):
            deny("piping downloaded or decoded data into a shell/interpreter")
        if SENSITIVE_RE.search(text):
            deny("it touches credential / private key / browser-profile files")
        for inner in extract_substitutions(text):
            self._scan(inner, st.copy(), depth + 1)
        for seg in split_segments(text):
            self._segment(seg, st, depth)

    # ------------------------------------------------------------------ one command
    def _segment(self, seg, st, depth):
        toks = tokenize(seg)
        words, i = [], 0
        while i < len(toks):
            t = toks[i]
            if t.op:
                nxt = toks[i + 1] if i + 1 < len(toks) and not toks[i + 1].op else None
                if t.val in (">", ">>", ">|", "&>", "&>>") and nxt is not None:
                    self._target(nxt, st, "redirecting output")
                elif t.val == ">&" and nxt is not None and not (nxt.val.isdigit() or nxt.val == "-"):
                    self._target(nxt, st, "redirecting output")
                i += 2 if nxt is not None else 1
                continue
            words.append(t)
            i += 1
        while words and words[0].val in KEYWORDS:
            words.pop(0)
        if not words or words[0].val in SKIP_SEGMENT:
            return
        # leading VAR=value assignments
        while words and re.match(r"^[A-Za-z_]\w*=", words[0].val):
            name = words[0].val.split("=", 1)[0]
            if name.startswith(("LD_", "DYLD_")) or name in ("BASH_ENV", "ENV", "PROMPT_COMMAND", "PATH"):
                deny("setting %s in front of a command (environment hijack)" % name)
            words.pop(0)
        if words:
            self._words(words, st, depth)

    def _unwrap(self, base, words):
        rest, i = words[1:], 0
        vals = _WRAP_VAL_FLAGS.get(base, set())
        while i < len(rest):
            v = rest[i].val
            if base == "env" and re.match(r"^[A-Za-z_]\w*=", v):
                name = v.split("=", 1)[0]
                if name.startswith(("LD_", "DYLD_")) or name in ("BASH_ENV", "ENV", "PROMPT_COMMAND", "PATH"):
                    deny("setting %s via env (environment hijack)" % name)
                i += 1
                continue
            if v.startswith("-") and len(v) > 1:
                i += 2 if v in vals else 1
                continue
            break
        if base in ("timeout", "chrt", "taskset") and i < len(rest):
            i += 1
        return rest[i:]

    def _words(self, words, st, depth):
        for _ in range(8):
            if not words:
                return
            w = words[0]
            base = os.path.basename(w.val)
            if base in PRIV_CMDS or base.startswith("mkfs"):
                deny("'%s' is a privilege-escalation / system-administration / out-of-sandbox command" % base)
            if base in WRAPPERS:
                if base == "command" and len(words) > 1 and words[1].val in ("-v", "-V"):
                    return
                words = self._unwrap(base, words)
                continue
            break
        else:
            deny("command wrapping is too deep to verify")
        if not words:
            return
        w, args = words[0], words[1:]
        base = os.path.basename(w.val)
        if w.dyn and self.strict:
            deny("the command name is built from a variable / substitution and cannot be verified")
        if "/" in w.val:
            r = self.resolve(w.val, st.cwd)
            if r and any(r == d or r.startswith(d + "/") for d in EXEC_DENY_DIRS) and not self.inside(r):
                deny("executing a program from %s" % r)
        vals = [a.val for a in args]
        pos = [a for a in args if not (a.val.startswith("-") and len(a.val) > 1)]

        if base == "cd":
            st.cwd = (self.resolve(vals[0], st.cwd) if vals and vals[0] != "-" else (self.home if not vals else None))
            return
        if base in ("pushd", "popd"):
            st.cwd = None
            return
        if base == "kill" and any(re.fullmatch(r"-?1", v) for v in vals):
            deny("signalling pid 1 / all processes")
        if base == "chmod":
            if pos and SETUID_MODE_RE.match(pos[0].val):
                deny("setting setuid/setgid bits")
            for t in pos[1:]:
                self._target(t, st, "chmod")
            return
        if base in ("chown", "chgrp"):
            for t in pos[1:]:
                self._target(t, st, base)
            return
        if base == "eval":
            self._scan(" ".join(vals), st.copy(), depth + 1)
            return
        if base in ("source", "."):
            if pos:
                self._script(pos[0], st)
            return
        if base in INTERPRETERS:
            self._interpreter(base, args, pos, st, depth)
        self._writes(base, args, pos, vals, st, depth)

    # ------------------------------------------------------------------ interpreters
    def _script(self, tok, st):
        r = self.resolve(tok.val, st.cwd)
        if r and any(r == d or r.startswith(d + "/") for d in EXEC_DENY_DIRS) and not self.inside(r):
            deny("running a script from %s" % r)

    def _interpreter(self, base, args, pos, st, depth):
        vals = [a.val for a in args]
        code_flags = {"-c", "-e", "-E", "-r", "--eval", "-p", "-pe", "-ne", "-lne", "-lpe", "-pi", "-i"}
        if base in SHELLS:
            for k, v in enumerate(vals):
                if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", v) and k + 1 < len(vals):
                    self._scan(vals[k + 1], st.copy(), depth + 1)
                    return
        elif base in ("awk", "gawk", "mawk"):
            if pos:
                self.scan_inline(pos[0].val, st)
                if re.search(r"system\s*\(|\|\s*getline|getline\s*<|\|\s*[\"']", pos[0].val):
                    deny("awk script that runs commands")
            return
        else:
            for k, v in enumerate(vals):
                if v in code_flags and k + 1 < len(vals):
                    self.scan_inline(vals[k + 1], st)
                    return
                if v.startswith("-") and not v.startswith("--") and len(v) > 2 and re.search(r"[ce]", v[1:]) and k + 1 < len(vals) and v[1] in "cepnl":
                    self.scan_inline(vals[k + 1], st)
                    return
            if "-m" in vals:
                return
        if pos and not (pos[0].val == "-"):
            self._script(pos[0], st)
        else:
            st.stdin_code = True   # code arrives on stdin / heredoc: scanned at the end

    def scan_inline(self, code, st):
        if REVERSE_SHELL_RE.search(code):
            deny("reverse-shell / raw network shell pattern in code")
        if SENSITIVE_RE.search(code):
            deny("code that touches credential / private key / browser-profile files")
        if INLINE_ESCALATE_RE.search(code):
            deny("code that launches a privilege-escalation / out-of-sandbox command")
        for m in re.finditer(r"""(['"])([<>+|]*)((?:/|~)[^'"\s\\]*)\1""", code):
            prefix, path = m.group(2), m.group(3)
            if path in OK_DEV_TARGETS:
                continue
            r = self.resolve(path, st.cwd)
            outside = (r is None and self.strict) or (r is not None and not self.inside(r))
            if not outside:
                continue
            window = code[max(0, m.start() - 120):m.end() + 120]
            if ">" in prefix or WRITE_HINT_RE.search(window):
                deny("code that writes to %s, outside the allowed folders" % (r or path))

    # ------------------------------------------------------------------ commands that write
    def _writes(self, base, args, pos, vals, st, depth):
        t = self._target
        if base == "tee":
            for a in pos:
                t(a, st, "tee")
        elif base in ("cp", "install", "rsync", "ln"):
            dest = None
            for k, v in enumerate(vals):
                if v in ("-t", "--target-directory") and k + 1 < len(args):
                    dest = args[k + 1]
                elif v.startswith("--target-directory="):
                    dest = v.split("=", 1)[1]
            if dest is None and pos:
                dest = pos[-1]
            if dest is not None and ":" not in (dest.val if isinstance(dest, Tok) else dest).split("/")[0]:
                t(dest, st, base)
        elif base == "mv":
            for a in pos:
                t(a, st, "mv (moves/removes)")
        elif base in ("touch", "mkdir", "rmdir", "rm", "unlink", "shred", "truncate", "mkfifo", "mknod",
                      "chattr", "setfacl"):
            for a in pos:
                if not re.fullmatch(r"[0-9+\-]+[KMGkmg]?", a.val):
                    t(a, st, base)
        elif base == "dd":
            for v in vals:
                if v.startswith("of="):
                    t(v[3:], st, "dd of=")
        elif base == "sort":
            for k, v in enumerate(vals):
                if v in ("-o", "--output") and k + 1 < len(args):
                    t(args[k + 1], st, "sort -o")
                elif v.startswith("--output="):
                    t(v.split("=", 1)[1], st, "sort -o")
        elif base == "sed":
            inplace = any(v == "--in-place" or v.startswith("--in-place=") or re.fullmatch(r"-[a-zA-Z]*i[^ ]*", v) for v in vals)
            if inplace:
                files = pos if any(v in ("-e", "-f") for v in vals) else pos[1:]
                for a in files:
                    t(a, st, "sed -i")
        elif base == "perl":
            if any(re.fullmatch(r"-[a-zA-Z]*i[^ ]*", v) for v in vals):
                for a in pos[1:]:
                    t(a, st, "perl -i")
        elif base == "curl":
            for k, v in enumerate(vals):
                if v in ("-o", "--output", "--output-dir") and k + 1 < len(args):
                    t(args[k + 1], st, "curl output")
                elif v.startswith("--output="):
                    t(v.split("=", 1)[1], st, "curl output")
                elif v in ("-O", "--remote-name", "-J"):
                    t(st.cwd or ".", st, "curl download")
        elif base == "wget":
            for k, v in enumerate(vals):
                if v in ("-O", "-P", "--output-document", "--directory-prefix") and k + 1 < len(args):
                    t(args[k + 1], st, "wget output")
            if not any(v in ("-O", "--output-document") for v in vals):
                t(st.cwd or ".", st, "wget download")
        elif base == "unzip":
            for k, v in enumerate(vals):
                if v == "-d" and k + 1 < len(args):
                    t(args[k + 1], st, "unzip extract dir")
            if "-d" not in vals:
                t(st.cwd or ".", st, "unzip extract")
        elif base == "tar":
            first = vals[0] if vals else ""
            creating = bool(re.fullmatch(r"-?[A-Za-z]*[cru][A-Za-z]*", first)) or \
                any(v in ("--create", "--append", "--update") for v in vals)
            for k, v in enumerate(vals):
                if v in ("-C", "--directory") and k + 1 < len(args):
                    t(args[k + 1], st, "tar directory")
                elif v.startswith("--directory="):
                    t(v.split("=", 1)[1], st, "tar directory")
                if creating:
                    short_f = ((v.startswith("-") and not v.startswith("--")) or k == 0) and "f" in v and not v.startswith("--")
                    if (short_f or v == "--file") and k + 1 < len(args):
                        t(args[k + 1], st, "tar archive output")
                    elif v.startswith("--file="):
                        t(v.split("=", 1)[1], st, "tar archive output")
            if not creating and not any(v in ("-C", "--directory") or v.startswith("--directory=") for v in vals):
                t(st.cwd or ".", st, "tar extract")
        elif base == "find":
            stop = next((k for k, v in enumerate(vals) if v.startswith("-") or v in ("(", "!")), len(vals))
            roots = args[:stop]
            if any(v in ("-delete",) for v in vals):
                for a in roots:
                    t(a, st, "find -delete")
            for k, v in enumerate(vals):
                if v in ("-exec", "-execdir", "-ok", "-okdir"):
                    sub = []
                    for a in args[k + 1:]:
                        if a.val in (";", "+"):
                            break
                        sub.append(a)
                    if sub:
                        self._words(sub, st.copy(), depth + 1)
        elif base == "git":
            gdir, sub, k = st.cwd, None, 0
            while k < len(vals):
                if vals[k] == "-C" and k + 1 < len(vals):
                    gdir = self.resolve(vals[k + 1], st.cwd)
                    k += 2
                    continue
                if vals[k].startswith("-"):
                    k += 1
                    continue
                sub = vals[k]
                break
            if sub in GIT_WRITE:
                if gdir is None or not self.inside(gdir):
                    deny("git %s outside the allowed folders" % sub)
                rest = [v for v in vals[k + 1:] if not v.startswith("-")]
                if sub == "clone" and len(rest) >= 2:
                    t(rest[-1], st, "git clone destination")
                elif sub == "init" and rest:
                    t(rest[0], st, "git init destination")



# =============================================================================== read-side helpers
def check_read_path(path):
    """Used by the read_file / list_dir tools: never hand credential files to the AI."""
    if SENSITIVE_RE.search(str(path)):
        deny("reading '%s' (credential / private key / browser profile)" % path)


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] == "--exec":
        _launcher_main(sys.argv[2:4])
    else:
        print(json.dumps(kernel_status()))
