#!/usr/bin/env python3
"""tablog report/find: turn raw tablog session logs into readable Markdown + clean text.

  tablog-report.py report --root ~/logs ENG [--local] [--max-lines N] [--max-lines-remote N]
  tablog-report.py find   --root ~/logs --eng ENG PATTERN [--all]

Interactive tools (penelope, nc, evil-winrm, impacket shells, msfconsole, ...) are never
truncated, and their output is split into one entry per remote command by spotting the
remote prompt. Add your own tools with TABLOG_INTERACTIVE="tool1 tool2".
"""
import argparse
import bisect
import glob
import json
import os
import re
import shlex
import sys
from collections import Counter
from datetime import datetime, timezone

# ---------- what counts as interactive ----------
# Shells and clients with a prompt: output is split per remote command.
SHELL_TOOLS = {
    # reverse/bind shell handlers
    "penelope", "nc", "ncat", "netcat", "socat", "pwncat", "pwncat-cs",
    # remote access
    "ssh", "telnet", "evil-winrm", "evil-winrm-py", "pwsh",
    # impacket (impacket-psexec, psexec.py, ... all normalise to these)
    "psexec", "wmiexec", "smbexec", "dcomexec", "atexec", "mssqlclient", "smbclient",
    "rpcclient",
    # frameworks / pivoting / c2
    "msfconsole", "ligolo-proxy", "ligolo-ng", "sliver", "sliver-client", "sqlmap",
    # protocol / db clients
    "ftp", "sftp", "lftp", "mysql", "mariadb", "psql", "sqlcmd", "sqsh",
    "redis-cli", "mongo", "mongosh",
}
# Long-running tools without a prompt: shown in full, not split.
LONG_TOOLS = {
    "http.server", "uploadserver", "updog", "smbserver", "responder", "ntlmrelayx",
    "mitm6", "chisel", "tcpdump", "ligolo-agent", "simplehttpserver", "wsgidav",
}
SHELL_TOOLS |= set(os.environ.get("TABLOG_INTERACTIVE", "").split())

WRAPPERS = {"sudo", "doas", "rlwrap", "proxychains", "proxychains4", "env", "time",
            "nohup", "stdbuf", "unbuffer", "exec", "command", "timeout"}
WRAPPER_ARGFLAGS = {"-u", "-g", "-f", "-C", "-p"}
OWN_CMDS = re.compile(r"^\s*(tab|tabi|eng|note|tablog)(\s|$)")  # tablog's own commands stay out
NOTE_CMD = re.compile(r"^\s*note\s")
LEAVING = re.compile(r"^\s*(exit|logout)(\s+-?\d+)?\s*$")   # closing the tab is not unfinished


def tool_names(cmd):
    """Base tool name of every segment of a command line (wrappers stripped)."""
    names = []
    for seg in re.split(r"&&|\|\||;|\|", cmd):
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = seg.split()
        i = 0
        while i < len(toks):
            t = toks[i]
            b = os.path.basename(t).lower()
            if re.match(r"^\w+=", t):              # VAR=value prefix
                i += 1
                continue
            if b in WRAPPERS:
                i += 1
                while i < len(toks) and toks[i].startswith("-"):
                    i += 2 if toks[i] in WRAPPER_ARGFLAGS else 1
                if b == "timeout" and i < len(toks) and re.match(r"^[\d.]+[smhd]?$", toks[i]):
                    i += 1
                continue
            if re.match(r"^python[\d.]*$", b):          # python3 x.py / python3 -m mod
                i += 1
                mod = None
                while i < len(toks) and toks[i].startswith("-"):
                    if toks[i] == "-m" and i + 1 < len(toks):
                        mod = toks[i + 1]
                        break
                    i += 1
                if mod:
                    names.append(mod.lower())
                    break
                continue
            b = re.sub(r"\.py$", "", b)
            b = re.sub(r"^impacket-", "", b)
            names.append(b)
            break
    return names


def classify(cmd):
    names = tool_names(cmd)
    for n in names:
        if n in SHELL_TOOLS:
            return "shell", n
    for n in names:
        if n in LONG_TOOLS:
            return "long", n
    return None, None


# ---------- remote prompt detection ----------
# one alternation, tried in this order
STRONG_PROMPT = re.compile("|".join(f"(?:{p})" for p in (
    r"\*Evil-WinRM\* PS [^>\n]*> ",                       # evil-winrm
    r"PS [A-Za-z]:\\[^>\n]*> ?",                          # PowerShell
    r"[A-Za-z]:\\[^>\n]*>",                               # cmd.exe, impacket shells
    r"(?:\([^)\n]*\)\s*)?[\w.-]+@[\w.-]+:[^\n]*?[#$] ",   # user@host:path$
    r"\[[\w.-]+@[\w.-]+[^\]\n]*\][#$] ",                  # [user@host dir]$
    r"(?:bash|sh|zsh|dash|ash)-[\d.]+[#$] ",              # bash-5.1$
    r"(?:msf\d*|meterpreter)\b[^>\n]*> ",                 # msfconsole / meterpreter
    r"SQL(?: \([^)\n]*\))?> ",                            # impacket mssqlclient
    r"smb: \\[^>\n]*> ",                                  # smbclient
    r"rpcclient \$> ",
    r"(?:ftp|sftp|mysql|MariaDB \[[^\]\n]*\]|os-shell|sql-shell)> ",
    r"lftp [^>\n]*> ",
    r"[\w.-]+:\d+(?:\[\d+\])?> ",                         # redis-cli 127.0.0.1:6379>
    r"[\w-]+=[#>] ",                                      # psql
    r"ligolo-ng » ",
    r"sliver(?: \([^)\n]*\))? > ",
    r"\(Penelope\)[^\n]*?> ",                             # penelope menu
)))
WEAK_PROMPT = re.compile(r"[#$%] ")  # bare prompts; only trusted when they repeat


def split_session(lines):
    """lines: [(text, end_offset)] -> (banner_lines, [sub]) or None if no prompts found."""
    cands = []
    for i, (t, _) in enumerate(lines):
        t += " "                         # lines are right-stripped; prompts end in a space
        m = STRONG_PROMPT.match(t)
        if m:
            cands.append((i, m.group(0), t[m.end():].strip(), True))
        else:
            m = WEAK_PROMPT.match(t)
            if m:
                cands.append((i, m.group(0), t[m.end():].strip(), False))
    weak = Counter(c[1] for c in cands if not c[3])
    prompts = [c for c in cands if c[3] or weak[c[1]] >= 2]
    if not prompts:
        return None
    banner = list(lines[:prompts[0][0]])
    subs = []
    for k, (i, p, cmd, _) in enumerate(prompts):
        end = prompts[k + 1][0] if k + 1 < len(prompts) else len(lines)
        body = list(lines[i + 1:end])
        if not cmd:                      # empty Enter; keep any async output it was followed by
            (subs[-1]["out"] if subs else banner).extend(body)
            continue
        subs.append({"prompt": p.rstrip(), "cmd": cmd, "out": body, "off": lines[i][1]})
    return banner, subs


# ---------- terminal rendering ----------
MARK = re.compile(r"\x1b\]6973;([SE]);([^;\x07]+);(\d+)(?:;(-?\d+))?\x07")
TOKEN = re.compile(
    r"(?P<osc>\x1b\][^\x07\x1b]*(?:\x07|\x1b\\))"
    r"|(?P<csi>\x1b\[(?P<p>[0-?]*)[ -/]*(?P<f>[@-~]))"
    r"|(?P<esc>\x1b[()*+].|\x1b[^\[\]])"
    r"|(?P<ctl>\r*\n|[\r\b])"
    r"|(?P<txt>[^\x1b\r\n\b]+)"
    r"|(?P<lone>\x1b)"
)
CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
SURR = re.compile(r"[\udc80-\udcff]")
TUI_NOTE = "[full-screen program output omitted]"
ALT_MODES = {"?1049", "?1047", "?47"}
ALT_OFF = re.compile(r"\x1b\[\?(?:1049|1047|47)l")


def render(s, base=0):
    """Render terminal output to plain lines: strip colours, apply \\r, \\b, erase-line,
    drop full-screen apps. Returns [(text, end_offset)] with offsets into the full log."""
    out, line, col = [], "", 0           # line: the row being drawn, col: cursor in it
    pos, end = 0, len(s)
    while pos < end:
        resume = end
        for m in TOKEN.finditer(s, pos):
            kind = m.lastgroup
            if kind == "txt":
                t = m.group()
                if not t.isprintable():
                    t = SURR.sub("\ufffd", CTRL.sub("", t))
                if col == len(line):
                    line += t
                else:
                    if col > len(line):
                        line = line.ljust(col)
                    line = line[:col] + t + line[col + len(t):]
                col += len(t)
            elif kind == "ctl":
                c = m.group()
                if c[-1] == "\n":            # "\r\n" comes as one token
                    out.append((line.rstrip(), base + m.end() - 1))
                    line, col = "", 0
                elif c == "\r":
                    col = 0
                elif col:
                    col -= 1
            elif kind == "csi":
                f = m.group("f")
                if f == "m":                 # colours: by far the most common sequence
                    continue
                p = m.group("p")
                if f == "h" and p in ALT_MODES:
                    # full-screen program: skip straight to where it leaves the alt screen
                    if line.strip():
                        out.append((line.rstrip(), base + m.start()))
                    out.append((TUI_NOTE, base + m.start()))
                    line, col = "", 0
                    off = ALT_OFF.search(s, m.end())
                    resume = off.end() if off else end
                    break
                n = int(p) if p.isdigit() else 0
                if f == "K":
                    if n == 0:
                        line = line[:col]
                    elif n == 1:
                        line = " " * min(col, len(line)) + line[col:]
                    else:
                        line = ""
                elif f == "C":
                    col += max(1, n)
                elif f == "D":
                    col = max(0, col - max(1, n))
                elif f == "G":
                    col = max(0, max(1, n) - 1)
        pos = resume
    if line.strip():
        out.append((line.rstrip(), base + end))
    # squeeze blank runs and trim
    res = []
    for item in out:
        if item[0] or (res and res[-1][0]):
            res.append(item)
    if res and not res[-1][0]:
        res.pop()
    return res


def text_of(lines):
    return "\n".join(t for t, _ in lines).strip("\n")


# ---------- timing (script mode): byte offset -> wall clock ----------
class Timing:
    def __init__(self, path, header_len):
        self.bytes, self.secs, self.hdr = [], [], header_len
        cb, ct = 0, 0.0
        with open(path) as fh:
            for raw in fh:
                parts = raw.split()
                try:
                    if len(parts) == 3:             # advanced format: TYPE DELAY BYTES
                        ct += float(parts[1])
                        if parts[0] != "O":
                            continue
                        cb += int(parts[2])
                    else:                           # classic format: DELAY BYTES
                        ct += float(parts[0])
                        cb += int(parts[1])
                except (ValueError, IndexError):
                    continue
                self.bytes.append(cb)
                self.secs.append(ct)

    def elapsed(self, byte_off):
        if not self.bytes:
            return None
        i = bisect.bisect_right(self.bytes, byte_off - self.hdr)
        return self.secs[min(i, len(self.secs) - 1)]


class ByteOffsets:
    """Char offset in the decoded log -> byte offset in the file. Remembers where it got to,
    so walking a log front to back encodes every part of it once."""
    def __init__(self, raw):
        self.raw, self.c, self.b = raw, 0, 0

    def of(self, offsets):
        res = {}
        for c in sorted(set(offsets)):
            if c < self.c:
                self.c = self.b = 0
            self.b += len(self.raw[self.c:c].encode("utf-8", "surrogateescape"))
            self.c = c
            res[c] = self.b
        return res


# ---------- loading ----------
def load_index(path):
    events = []
    if not os.path.exists(path):
        return events
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if raw:
                try:
                    events.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
    return merge_runs(events)


def merge_runs(events):
    """Every command writes a "run" event when it starts and a "cmd" event when it is back at
    the prompt. Drop the runs that finished; turn the others (still running, or the tab was
    closed) into cmd events marked open, without t1 and rc."""
    def key(e):
        return e.get("sid"), e.get("n"), e.get("t0")
    done = {key(e) for e in events if e.get("ev") == "cmd"}
    out = []
    for e in events:
        if e.get("ev") != "run":
            out.append(e)
        elif key(e) not in done and not LEAVING.match(e.get("cmd", "")):
            out.append(dict(e, ev="cmd", open=True))
    return out


def find_log(sessdir, sid):
    """sid.log, or sid__tabname.log once the tab has a name (never another sid with the same prefix)."""
    plain = os.path.join(sessdir, sid + ".log")
    if os.path.exists(plain):
        return plain
    hits = sorted(glob.glob(os.path.join(sessdir, glob.escape(sid) + "__*.log")))
    return hits[0] if hits else None


def write_transcript(log, raw):
    """Clean .txt next to the raw log. Rendering a big log is the slowest part of a report, so
    a transcript newer than its log (and than this script) is kept as it is."""
    txt = log[:-4] + ".txt"
    sid = os.path.basename(log)[:-4].split("__")[0]
    for old in glob.glob(os.path.join(os.path.dirname(log), glob.escape(sid) + "*.txt")):
        name = os.path.basename(old)
        if old != txt and name[len(sid):len(sid) + 2] in ("__", ".t"):   # left by a tab rename
            os.remove(old)
    try:
        if os.stat(txt).st_mtime_ns > max(os.stat(log).st_mtime_ns, os.stat(__file__).st_mtime_ns):
            return txt
    except OSError:
        pass
    with open(txt, "w", encoding="utf-8") as fh:
        fh.write(text_of(render(raw)) + "\n")
    return txt


def marker_spans(raw, sid):
    """Command number -> (output_start, output_end) char offsets in the raw log. A command
    without an end marker (still running, tab closed) runs to the next command or the end."""
    spans, cur = {}, None                # cur: (n, output_start) of the command still open
    for m in MARK.finditer(raw):
        if m.group(2) != sid:
            continue
        n = int(m.group(3))
        if cur and (m.group(1) == "S" or cur[0] == n):
            spans[cur[0]] = (cur[1], m.start())
            cur = None
        if m.group(1) == "S":
            cur = (n, m.end())
    if cur:
        spans[cur[0]] = (cur[1], len(raw))
    return spans


# ---------- formatting ----------
def fmt_time(t, local, full=True):
    d = datetime.fromtimestamp(float(t), None if local else timezone.utc)
    if local:
        d = d.astimezone()
    return d.strftime("%Y-%m-%d %H:%M:%S" if full else "%H:%M:%S")


def fmt_dur(sec):
    sec = max(0.0, float(sec))
    if sec < 10:
        return f"{sec:.1f}s"
    if sec < 3600:
        m, s = divmod(int(round(sec)), 60)
        return f"{m}m{s:02d}s" if m else f"{s}s"
    h, rem = divmod(int(sec), 3600)
    return f"{h}h{rem // 60:02d}m"


def code_span(s):
    s = s.replace("\n", " ⏎ ")
    if "`" in s:
        return f"`` {s} ``"
    return f"`{s}`"


def cell(s):
    return s.replace("|", "\\|")


def fenced(body, lang="console"):
    longest = max((len(m) for m in re.findall(r"`+", body)), default=0)
    f = "`" * max(3, longest + 1)
    return [f + lang, body, f, ""]


def truncate(text, max_lines, txt_name):
    lines = text.split("\n")
    if max_lines <= 0 or len(lines) <= max_lines:
        return text
    head = int(max_lines * 0.75)
    tail = max_lines - head
    cut = len(lines) - head - tail
    return "\n".join(lines[:head] + [f"... [{cut} lines omitted, full output in {txt_name}] ..."]
                     + lines[-tail:])


# ---------- report ----------
def session_report(sid, s, edir, args):
    """One tab: (timeline rows, markdown block, number of remote commands, notes), or None."""
    sessdir = os.path.join(edir, "sessions")
    evs = sorted(s["events"], key=lambda e: e.get("t0", e.get("t", 0)))
    evs = [e for e in evs if not (e.get("ev") == "cmd" and OWN_CMDS.match(e.get("cmd", "")))]
    tabs = [e["tab"] for e in evs if e.get("ev") == "tab" and e.get("tab")]
    if not any(e.get("ev") in ("cmd", "note") for e in evs) and not tabs:
        return None

    log = find_log(sessdir, sid)
    data = b""
    if log:
        with open(log, "rb") as fh:
            data = fh.read()
    raw = data.decode("utf-8", "surrogateescape")
    spans = marker_spans(raw, sid)

    timing = None                    # script mode only; loaded for the first remote shell
    tpath = log[:-4] + ".timing" if log else None
    if not (tpath and os.path.exists(tpath)):
        tpath = None
    hdr = data.find(b"\n") + 1 if data.startswith(b"Script started") else 0
    del data
    offs = ByteOffsets(raw)

    txt_name = os.path.relpath(write_transcript(log, raw), edir) if log else None

    title = tabs[-1] if tabs else "(unnamed tab)"
    earlier = [t for i, t in enumerate(tabs[:-1]) if t not in tabs[i + 1:]]
    st = s["start"] or {}
    meta = [f"session `{sid}`"]
    if st.get("sh"):
        meta.append(st["sh"] + (" in tmux" if st.get("mode") == "tmux" else ""))
    if st.get("t"):
        meta.append("started " + fmt_time(st["t"], args.local))
    lhosts = [e.get("lhost") for e in [st] + evs if e.get("lhost")]
    if lhosts:
        meta.append("LHOST " + lhosts[-1])
    if txt_name:
        meta.append(f"transcript `{txt_name}`")

    rows = []      # timeline: (sort_key, time_or_None, tab, exit, took, command_cell)
    n_remote = 0
    notes = []     # (time, tab, text)
    b = [f"### {title}"]
    if earlier:
        b.append("*previously: " + ", ".join(earlier) + "*")
    b += [" · ".join(meta), ""]

    for e in evs:
        if e.get("ev") == "tab":
            b += [f"> {fmt_time(e['t'], args.local, False)} tab renamed to "
                  f"**{e.get('tab') or '(none)'}**", ""]
            continue
        if e.get("ev") == "note":
            text = " ".join(e.get("text", "").split())
            b += [f"> {fmt_time(e['t'], args.local, False)} **note:** {text}", ""]
            rows.append(((e["t"], 0), e["t"], e.get("tab") or "-", "", "", f"**note:** {text}"))
            notes.append((e["t"], e.get("tab") or "-", text))
            continue
        if e.get("ev") != "cmd":
            continue
        t0, rc, cmd = e["t0"], e.get("rc", 0), e.get("cmd", "")
        tab = e.get("tab") or "-"
        if e.get("open"):                # never came back to the prompt
            rc_cell, took, state = "?", "", "**no exit recorded**"
        else:
            rc_cell, took = str(rc), fmt_dur(e["t1"] - t0)
            state = f"{'ok' if rc == 0 else f'**exit {rc}**'} · {took}"
        rows.append(((t0, 0), t0, tab, rc_cell, took, code_span(cmd)))
        kind, tool = classify(cmd)
        span = spans.get(e.get("n"))
        lines = render(raw[span[0]:span[1]], span[0]) if span else None

        head = f"**{fmt_time(t0, args.local, False)}** · {state} · `{e.get('cwd', '')}`"
        split = split_session(lines) if (lines and kind == "shell") else None

        if lines is None:
            b += [head] + fenced(f"$ {cmd}\n[output not found in session log]")
            continue
        if not kind:
            body = f"$ {cmd}"
            if lines:
                body += "\n" + truncate(text_of(lines), args.max_lines, txt_name or "the log")
            b += [head] + fenced(body)
            continue

        # interactive / long-running: never truncated unless --max-lines-remote
        lim = args.max_lines_remote
        if not split:
            note = f"{tool}, full output" if kind == "long" else f"{tool} session, no prompt detected"
            b += [f"{head} · *{note}*"]
            body = f"$ {cmd}"
            if lines:
                body += "\n" + truncate(text_of(lines), lim, txt_name or "the log")
            b += fenced(body)
            continue

        banner, subs = split
        n_remote += len(subs)
        b += [f"{head} · *{tool} session, {len(subs)} remote commands*"]
        body = f"$ {cmd}"
        if banner:
            body += "\n" + truncate(text_of(banner), lim, txt_name or "the log")
        b += fenced(body)

        # wall-clock time for each remote command (script mode only)
        base = None
        bmap = {}
        if tpath and not timing:
            timing = Timing(tpath, hdr)
        if timing:
            bmap = offs.of([span[0]] + [x["off"] for x in subs])
            e0 = timing.elapsed(bmap[span[0]])
            if e0 is not None:
                base = t0 - e0
        for k, x in enumerate(subs):
            tt = None
            if base is not None:
                el = timing.elapsed(bmap[x["off"]])
                tt = base + el if el is not None else None
            label = fmt_time(tt, args.local, False) if tt else ""
            b.append(f"↳ {label + ' · ' if label else ''}`{x['prompt']}`")
            sub_body = x["cmd"] if not x["out"] else \
                x["cmd"] + "\n" + truncate(text_of(x["out"]), lim, txt_name or "the log")
            b += fenced(f"{x['prompt']} {sub_body}")
            rows.append(((tt or t0, k + 1), tt, f"{tab} ↳ {tool}", "", "",
                         code_span(f"{x['prompt']} {x['cmd']}")))
    return rows, "\n".join(b), n_remote, notes


def _session_job(job):
    return session_report(*job)


PARALLEL_MIN_BYTES = 4 << 20     # below this a process pool costs more than it saves


def run_sessions(jobs, sessdir):
    """Tabs are independent, so render them on all cores when there is enough to render."""
    workers = min(len(jobs), os.cpu_count() or 1)
    if workers > 1 and os.path.isdir(sessdir):
        size = 0
        with os.scandir(sessdir) as it:
            for f in it:
                if f.name.endswith(".log"):
                    size += f.stat().st_size
        if size >= PARALLEL_MIN_BYTES:
            try:
                from concurrent.futures import ProcessPoolExecutor
                with ProcessPoolExecutor(workers) as pool:
                    return list(pool.map(_session_job, jobs))
            except Exception:        # no working pool here: do it in-process instead
                pass
    return [session_report(*job) for job in jobs]


def report(args):
    root = os.path.expanduser(args.root)
    edir = os.path.join(root, args.eng)
    events = load_index(os.path.join(edir, "commands.jsonl"))
    if not events:
        sys.exit(f"tablog: no commands logged for engagement '{args.eng}' ({edir})")

    sessions = {}
    for ev in events:
        sid = ev.get("sid")
        if not sid:
            continue
        s = sessions.setdefault(sid, {"start": None, "events": []})
        if ev.get("ev") == "start":
            s["start"] = s["start"] or ev
        else:
            s["events"].append(ev)

    order = sorted(sessions, key=lambda k: (sessions[k]["start"] or {}).get("t", 0) or 0)
    rows, blocks, n_remote, notes = [], [], 0, []
    for res in run_sessions([(sid, sessions[sid], edir, args) for sid in order],
                            os.path.join(edir, "sessions")):
        if res:
            rows += res[0]
            blocks.append(res[1])
            n_remote += res[2]
            notes += res[3]

    rows.sort(key=lambda r: r[0])
    tzname = "local" if args.local else "UTC"
    local_cmds = [r for r in rows if r[3] != ""]      # notes and remote commands have no exit
    md = [f"# tablog report: {args.eng}", ""]
    if local_cmds:
        first = min(r[1] for r in local_cmds)
        md.append(
            f"Generated {fmt_time(datetime.now().timestamp(), args.local)} {tzname} · "
            f"{len(blocks)} tabs · {len(local_cmds)} commands · {n_remote} remote commands · "
            f"from {fmt_time(first, args.local)} {tzname}"
        )
    if notes:
        md += ["", "## Notes", ""]
        md += [f"- {fmt_time(t, args.local)} · {tab} · {text}" for t, tab, text in sorted(notes)]
    md += ["", "## Timeline", "", f"| Time ({tzname}) | Tab | Exit | Took | Command |",
           "|---|---|---|---|---|"]
    for _, t, tab, rc, took, c in rows:
        md.append(f"| {fmt_time(t, args.local) if t else ''} | {cell(tab)} | {rc} | {took} | {cell(c)} |")
    md += ["", "## Tabs", ""] + [blk + "\n" for blk in blocks]

    out_path = os.path.join(edir, "report.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(md).rstrip() + "\n")
    print(out_path)


# ---------- find ----------
def find(args):
    root = os.path.expanduser(args.root)
    idx = sorted(glob.glob(os.path.join(root, "*", "commands.jsonl"))) if args.all \
        else [os.path.join(root, args.eng, "commands.jsonl")]
    rx = re.compile(args.pattern, re.I)
    hits = []
    for path in idx:
        eng = os.path.basename(os.path.dirname(path))
        for e in load_index(path):
            if e.get("ev") == "cmd" and rx.search(e.get("cmd", "")):
                if not NOTE_CMD.match(e.get("cmd", "")):     # the note itself is listed below
                    hits.append((e["t0"], eng, e))
            elif e.get("ev") == "note" and rx.search(e.get("text", "")):
                hits.append((e["t"], eng, e))
    for t, eng, e in sorted(hits, key=lambda h: h[0]):
        prefix = f"{eng}  " if args.all else ""
        if e.get("ev") == "note":
            print(f"{prefix}{fmt_time(t, args.local)}  [{e.get('tab') or '-'}]  \x1b[33mnote\x1b[0m  {e.get('text', '')}")
            continue
        rc = "?" if e.get("open") else e.get("rc", 0)
        rcs = f"\x1b[31mrc={rc}\x1b[0m" if rc else "rc=0"
        print(f"{prefix}{fmt_time(t, args.local)}  [{e.get('tab') or '-'}]  {rcs}  {e.get('cmd', '')}")
    if not hits:
        print("no matches", file=sys.stderr)
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(prog="tablog")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report")
    r.add_argument("eng")
    r.add_argument("--root", default="~/logs")
    r.add_argument("--local", action="store_true", help="local time instead of UTC")
    r.add_argument("--max-lines", type=int, default=120,
                   help="truncate long output of normal commands (0 = never)")
    r.add_argument("--max-lines-remote", type=int, default=0,
                   help="truncate output inside interactive sessions (default 0 = never)")
    f = sub.add_parser("find")
    f.add_argument("pattern")
    f.add_argument("--root", default="~/logs")
    f.add_argument("--eng", default="general")
    f.add_argument("--all", action="store_true", help="search every engagement")
    f.add_argument("--local", action="store_true")
    a = ap.parse_args()
    sys.exit(report(a) if a.cmd == "report" else find(a))


if __name__ == "__main__":
    main()
