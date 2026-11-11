#!/usr/bin/env python3
"""Unit tests for tablog-report.py (stdlib only): python3 tests/test_report.py"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "..", "tablog-report.py")
spec = importlib.util.spec_from_file_location("tablog_report", SCRIPT)
tr = importlib.util.module_from_spec(spec)
sys.dont_write_bytecode = True
sys.modules["tablog_report"] = tr          # lets worker processes find the module
spec.loader.exec_module(tr)


def text(s):
    return tr.text_of(tr.render(s))


class Render(unittest.TestCase):
    def test_colours_and_titles_are_stripped(self):
        self.assertEqual(text("\x1b[1;31mred\x1b[0m \x1b]0;title\x07plain\r\n"), "red plain")

    def test_carriage_return_overwrites(self):
        self.assertEqual(text("10%\r20%\r100%\r\n"), "100%")
        self.assertEqual(text("abcdef\rXY\r\n"), "XYcdef")

    def test_erase_line(self):
        self.assertEqual(text("progress 1\r\x1b[2Kdone\r\n"), "done")
        self.assertEqual(text("abcdef\x1b[3D\x1b[K\r\n"), "abc")

    def test_backspace_and_cursor_moves(self):
        self.assertEqual(text("abc\b\bX\r\n"), "aXc")
        self.assertEqual(text("a\x1b[3Cb\r\n"), "a   b")
        self.assertEqual(text("abcdef\x1b[2Gx\r\n"), "axcdef")

    def test_full_screen_programs_are_dropped(self):
        out = text("before\r\n\x1b[?1049hlots of \x1b[1;1H screen\r\ndrawing\x1b[?1049lafter\r\n")
        self.assertEqual(out, "before\n" + tr.TUI_NOTE + "\nafter")

    def test_full_screen_program_never_left(self):
        self.assertEqual(text("x\r\n\x1b[?1049hrest"), "x\n" + tr.TUI_NOTE)

    def test_blank_runs_are_squeezed(self):
        self.assertEqual(text("\r\n\r\na\r\n\r\n\r\n\r\nb\r\n\r\n"), "a\n\nb")

    def test_bad_bytes(self):
        raw = b"ok \xff\x00 end\n".decode("utf-8", "surrogateescape")
        self.assertEqual(text(raw), "ok \ufffd end")

    def test_offsets_point_at_line_ends(self):
        self.assertEqual(tr.render("ab\r\ncd\n", 100), [("ab", 103), ("cd", 106)])


class Classify(unittest.TestCase):
    def test_wrappers_and_prefixes(self):
        self.assertEqual(tr.classify("sudo rlwrap nc -lvnp 4444"), ("shell", "nc"))
        self.assertEqual(tr.classify("FOO=1 proxychains -q impacket-psexec a@b"), ("shell", "psexec"))
        self.assertEqual(tr.classify("timeout 30 /usr/bin/ssh host"), ("shell", "ssh"))
        self.assertEqual(tr.classify("python3 psexec.py a@b"), ("shell", "psexec"))

    def test_long_running(self):
        self.assertEqual(tr.classify("python3 -m http.server 80"), ("long", "http.server"))
        self.assertEqual(tr.classify("sudo responder -I tun0"), ("long", "responder"))

    def test_normal_commands(self):
        self.assertEqual(tr.classify("ls -la | grep nc"), (None, None))
        self.assertEqual(tr.classify("echo 'unbalanced"), (None, None))

    def test_any_segment_counts(self):
        self.assertEqual(tr.classify("cd /tmp && nc -lvnp 9001"), ("shell", "nc"))


class SplitSession(unittest.TestCase):
    @staticmethod
    def lines(*texts):
        return [(t, i) for i, t in enumerate(texts)]

    def test_strong_prompt(self):
        banner, subs = tr.split_session(self.lines(
            "listening on [any] 4444 ...",
            "www-data@box:/var/www$ id",
            "uid=33(www-data)",
            "www-data@box:/var/www$ whoami",
            "www-data",
        ))
        self.assertEqual([t for t, _ in banner], ["listening on [any] 4444 ..."])
        self.assertEqual([s["cmd"] for s in subs], ["id", "whoami"])
        self.assertEqual([t for t, _ in subs[0]["out"]], ["uid=33(www-data)"])

    def test_windows_prompts(self):
        _, subs = tr.split_session(self.lines(
            r"*Evil-WinRM* PS C:\Users\a> whoami", r"corp\a", r"C:\Windows\system32>hostname", "dc01"))
        self.assertEqual([s["cmd"] for s in subs], ["whoami", "hostname"])

    def test_weak_prompt_needs_to_repeat(self):
        self.assertIsNone(tr.split_session(self.lines("$ id", "uid=0(root)")))
        _, subs = tr.split_session(self.lines("$ id", "uid=0(root)", "$ pwd", "/"))
        self.assertEqual([s["cmd"] for s in subs], ["id", "pwd"])

    def test_empty_enter_keeps_output(self):
        _, subs = tr.split_session(self.lines("ftp> ls", "a.txt", "ftp>", "late line", "ftp> bye"))
        self.assertEqual([s["cmd"] for s in subs], ["ls", "bye"])
        self.assertEqual([t for t, _ in subs[0]["out"]], ["a.txt", "late line"])

    def test_no_prompt(self):
        self.assertIsNone(tr.split_session(self.lines("just", "output")))


class Pieces(unittest.TestCase):
    def test_marker_spans(self):
        raw = "p$ a\n\x1b]6973;S;sid;1\x07out1\n\x1b]6973;E;sid;1;0\x07p$ b\n\x1b]6973;S;sid;2\x07out2"
        spans = tr.marker_spans(raw, "sid")
        self.assertEqual(raw[slice(*spans[1])], "out1\n")
        self.assertEqual(raw[slice(*spans[2])], "out2")          # no end marker: runs to the end
        self.assertEqual(tr.marker_spans(raw, "other"), {})

    def test_merge_runs(self):
        ev = [
            {"ev": "run", "sid": "s", "n": 1, "t0": 1.0, "cmd": "ls"},
            {"ev": "cmd", "sid": "s", "n": 1, "t0": 1.0, "t1": 2.0, "rc": 0, "cmd": "ls"},
            {"ev": "run", "sid": "s", "n": 2, "t0": 3.0, "cmd": "nc -lvnp 1"},
            {"ev": "run", "sid": "s", "n": 3, "t0": 4.0, "cmd": "exit"},
        ]
        out = tr.merge_runs(ev)
        self.assertEqual([(e["n"], e.get("open", False)) for e in out], [(1, False), (2, True)])

    def test_truncate(self):
        body = "\n".join(str(i) for i in range(100))
        out = tr.truncate(body, 10, "x.txt").split("\n")
        self.assertEqual(len(out), 11)
        self.assertEqual(out[0], "0")
        self.assertEqual(out[-1], "99")
        self.assertIn("90 lines omitted", out[7])
        self.assertEqual(tr.truncate(body, 0, "x.txt"), body)

    def test_fmt_dur(self):
        self.assertEqual([tr.fmt_dur(x) for x in (0.04, 42, 75, 3700)], ["0.0s", "42s", "1m15s", "1h01m"])

    def test_code_span_and_fence(self):
        self.assertEqual(tr.code_span("a `b` c"), "`` a `b` c ``")
        self.assertEqual(tr.fenced("x ``` y")[0], "````console")

    def test_byte_offsets(self):
        raw = "aé\udcffb"                                   # 1 + 2 + 1 + 1 bytes
        offs = tr.ByteOffsets(raw)
        self.assertEqual(offs.of([1, 2]), {1: 1, 2: 3})
        self.assertEqual(offs.of([4, 3]), {3: 4, 4: 5})
        self.assertEqual(offs.of([1]), {1: 1})               # going back restarts


class EndToEnd(unittest.TestCase):
    SID = "20260101-120000-4242"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        sess = os.path.join(self.root, "exam", "sessions")
        os.makedirs(sess)
        sid, t = self.SID, 1767268800.0
        log = "Script started on 2026-01-01 12:00:00+00:00\n"
        chunks = []           # (delay, text) after the header, for the timing file

        def w(s, delay=0.0):
            chunks.append((delay, s))

        w(f"$ ls\r\n\x1b]6973;S;{sid};1\x07\x1b[34mdir\x1b[0m  file.txt\r\n\x1b]6973;E;{sid};1;0\x07")
        w(f"$ seq 1 300\r\n\x1b]6973;S;{sid};2\x07" + "".join(f"{i}\r\n" for i in range(1, 301))
          + f"\x1b]6973;E;{sid};2;0\x07")
        w(f"$ nc -lvnp 4444\r\n\x1b]6973;S;{sid};3\x07listening on [any] 4444 ...\r\n")
        w("www-data@box:/$ id\r\n", 60.0)
        w("uid=33(www-data)\r\n")
        w("www-data@box:/$ cat /etc/passwd | head -1\r\n", 30.0)
        w("root:x:0:0:root:/root:/bin/bash\r\n")
        w(f"\x1b]6973;E;{sid};3;130\x07")
        w(f"$ vim x\r\n\x1b]6973;S;{sid};4\x07\x1b[?1049hscreen\x1b[?1049l\x1b]6973;E;{sid};4;0\x07")
        w(f"$ python3 -m http.server\r\n\x1b]6973;S;{sid};5\x07Serving HTTP on 0.0.0.0\r\n")
        log += "".join(s for _, s in chunks)
        with open(os.path.join(sess, f"{sid}__web.log"), "w") as fh:
            fh.write(log)
        with open(os.path.join(sess, f"{sid}__web.timing"), "w") as fh:
            for delay, s in chunks:
                fh.write(f"O {delay:.6f} {len(s.encode())}\n")
        # same second, pid with the same prefix: must not be mistaken for this session
        with open(os.path.join(sess, f"{sid}1.log"), "w") as fh:
            fh.write("someone else\n")

        def cmd(n, t0, t1, rc, c):
            base = {"sid": sid, "n": n, "t0": t0, "tab": "web", "cwd": "~/work", "cmd": c}
            return [dict(base, ev="run"), dict(base, ev="cmd", t1=t1, rc=rc)]

        events = [{"ev": "start", "sid": sid, "t": t, "sh": "zsh", "mode": "script",
                   "user": "u", "host": "h", "lhost": "10.10.14.7"},
                  {"ev": "tab", "sid": sid, "t": t + 1, "tab": "web", "prev": "", "lhost": "10.10.14.7"}]
        events += cmd(1, t + 10, t + 10.5, 0, "ls")
        events += cmd(2, t + 20, t + 21, 0, "seq 1 300")
        events += [{"ev": "note", "sid": sid, "t": t + 25, "tab": "web", "text": "shell as www-data | via upload"}]
        events += cmd(3, t + 30, t + 200, 130, "nc -lvnp 4444")
        events += cmd(4, t + 210, t + 215, 0, "vim x")
        events += cmd(6, t + 216, t + 217, 0, "note shell as www-data")
        events += [{"ev": "run", "sid": sid, "n": 5, "t0": t + 220, "tab": "web", "cwd": "~/work",
                    "cmd": "python3 -m http.server"}]
        with open(os.path.join(self.root, "exam", "commands.jsonl"), "w") as fh:
            for e in events:
                fh.write(json.dumps(e) + "\n")
            fh.write("{broken line\n")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out = io.StringIO()
        old = sys.argv
        sys.argv = ["tablog"] + list(argv)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                try:
                    tr.main()
                except SystemExit as e:
                    code = e.code
        finally:
            sys.argv = old
        return code, out.getvalue()

    def report(self, *extra):
        code, out = self.run_cli("report", "--root", self.root, "exam", *extra)
        self.assertFalse(code)
        with open(out.strip(), encoding="utf-8") as fh:
            return fh.read()

    def test_report(self):
        md = self.report()
        self.assertIn("1 tabs · 5 commands · 2 remote commands", md)
        self.assertIn("### web", md)
        self.assertIn("LHOST 10.10.14.7", md)
        self.assertIn("$ ls\ndir  file.txt\n", md)                       # colours gone
        self.assertNotIn("\x1b", md)
        self.assertIn("lines omitted", md)                               # seq truncated
        self.assertIn("*nc session, 2 remote commands*", md)
        self.assertIn("www-data@box:/$ id\nuid=33(www-data)", md)
        self.assertIn("| web ↳ nc |  |  | `www-data@box:/$ cat /etc/passwd \\| head -1` |", md)
        self.assertIn("↳ 12:01:30 · `www-data@box:/$`", md)              # timed from the timing file
        self.assertIn("↳ 12:02:00 · `www-data@box:/$`", md)
        self.assertIn("**exit 130**", md)
        self.assertIn(tr.TUI_NOTE, md)
        self.assertIn("**no exit recorded**", md)                        # http.server still running
        self.assertIn("*http.server, full output*", md)
        self.assertIn("## Notes\n\n- 2026-01-01 12:00:25 · web · shell as www-data | via upload", md)
        self.assertIn("| web |  |  | **note:** shell as www-data \\| via upload |", md)
        self.assertNotIn("$ note shell", md)                             # the command itself stays out
        self.assertNotIn("someone else", md)

    def test_transcript_is_reused_until_the_log_changes(self):
        self.report()
        txt = os.path.join(self.root, "exam", "sessions", f"{self.SID}__web.txt")
        with open(txt, encoding="utf-8") as fh:
            self.assertIn("uid=33(www-data)", fh.read())
        with open(txt, "w") as fh:
            fh.write("marker\n")
        future = os.stat(txt).st_mtime + 3600
        os.utime(txt, (future, future))
        self.report()
        with open(txt) as fh:
            self.assertEqual(fh.read(), "marker\n")                      # newer than the log: kept
        os.utime(txt, (1, 1))
        self.report()
        with open(txt, encoding="utf-8") as fh:
            self.assertIn("uid=33(www-data)", fh.read())                 # older: rebuilt

    def test_max_lines_off(self):
        self.assertNotIn("lines omitted", self.report("--max-lines", "0"))

    def test_parallel_matches_serial(self):
        serial = self.report()
        old = tr.PARALLEL_MIN_BYTES
        tr.PARALLEL_MIN_BYTES = 0
        try:
            # a second tab, so there is something to spread over processes
            with open(os.path.join(self.root, "exam", "commands.jsonl"), "a") as fh:
                fh.write(json.dumps({"ev": "tab", "sid": "20260101-130000-1", "t": 1767272400.0,
                                     "tab": "other", "prev": ""}) + "\n")
            parallel = self.report()
        finally:
            tr.PARALLEL_MIN_BYTES = old
        self.assertIn("2 tabs", parallel)
        self.assertIn("### other", parallel)
        self.assertIn(serial[serial.index("### web"):], parallel)        # same tab, same block

    def test_find(self):
        code, out = self.run_cli("find", "--root", self.root, "--eng", "exam", "nc|www-data")
        self.assertEqual(code, 0)
        lines = out.strip().split("\n")
        self.assertEqual(len(lines), 2)
        self.assertIn("note", lines[0])
        self.assertIn("nc -lvnp 4444", lines[1])
        code, _ = self.run_cli("find", "--root", self.root, "--eng", "exam", "nothing-like-this")
        self.assertEqual(code, 1)

    def test_unknown_engagement(self):
        code, _ = self.run_cli("report", "--root", self.root, "nope")
        self.assertTrue(code)


if __name__ == "__main__":
    unittest.main()
