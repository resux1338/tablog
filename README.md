# tablog

Logs every terminal tab (zsh/bash, tmux-aware) and builds a Markdown report per engagement.

Made for pentests, CTFs and exams, where you need to know afterwards what you ran, when, in
which tab, and what came back. Open a tab and it is logged; nothing to start, nothing to forget.

- every tab is recorded with `script(1)` (or `tmux pipe-pane` inside tmux)
- every command is indexed with start time, duration, exit code, directory and tab name
- `tablog report` turns it into one `report.md`: a timeline over all tabs, then each tab with
  the output of every command
- reverse shells and other interactive tools (nc, penelope, evil-winrm, msfconsole, impacket,
  ssh, ...) are split into one entry per remote command
- `note` puts findings, creds and flags into the timeline while you work

## Requirements

- Linux
- zsh, or bash 5.0+
- `script` from util-linux 2.35+
- Python 3.8+ (only for `tablog report` and `tablog find`)
- optional: tmux, `ip` (for the VPN address shown in the report)

## Install

```sh
git clone https://github.com/resux1338/tablog.git && cd tablog && ./install.sh
```

This copies two files to `~/.local/share/tablog` and adds one line to the end of `~/.zshrc`
and `~/.bashrc`. Open a new tab. To update, pull and run `./install.sh` again.
To remove: `./install.sh --uninstall` (your logs stay).

## Usage

```sh
eng exam               # set engagement, other tabs follow
tabi VPN+UP            # infra tab, title >VPN+UP<
tab 10.10.11.5-web     # name tab after the target
note creds admin:S3cret on :8080   # remark in the report timeline
tablog report          # -> ~/logs/exam/report.md
```

| Command | |
|---|---|
| `eng NAME` | set engagement (no arg: show current) |
| `tab NAME` | name this tab (no arg: show current) |
| `tabi NAME` | infra tab, wrapped as `>NAME<` |
| `note TEXT` | timestamped remark in the report (own Notes section + timeline) |
| `tablog status` | where this tab logs |
| `tablog report [ENG]` | build report.md + .txt transcripts |
| `tablog report --local` | local time instead of UTC |
| `tablog report --max-lines N` | truncate normal command output (default 120, 0 = off) |
| `tablog report --max-lines-remote N` | truncate output inside interactive sessions (default 0 = off) |
| `tablog find REGEX` | search commands and notes (`--all` for every engagement) |
| `tablog help` | cheat sheet |

New tabs/splits that start in `~` open in the directory `eng NAME` was run in. Without one
(or when it was run in `~`) they open in the last directory you were in.

## Inside evil-winrm, reverse shells and other remote sessions

Nothing to set up: tablog records the terminal, so whatever you type and get back inside
evil-winrm, a netcat or penelope reverse shell, ssh, msfconsole, an impacket shell, smbclient
or a database client is logged like everything else.

In the report such a session is not one long blob. tablog recognises the remote prompt
(`*Evil-WinRM* PS C:\Users\x>`, `C:\Windows\system32>`, `www-data@box:/var/www$`,
`meterpreter >`, `SQL>`, a bare `$` or `#`, ...) and gives every remote command its own entry
with its output, plus its own row in the timeline:

```
| 2026-01-01 12:01:30 | 10.10.11.5-web ↳ evil-winrm | | | `*Evil-WinRM* PS C:\Users\svc> whoami /priv` |
```

- Output inside these sessions is never truncated (unless you pass `--max-lines-remote N`).
- Remote commands get their own timestamps in normal tabs; inside tmux they are listed in
  order under the session, without a time of their own.
- If no prompt is recognised, the session is shown in full as one block, nothing is lost.
- `tab`, `note` and `tablog` are commands of your local shell. While you are inside a remote
  shell, write notes from a second tab; they land in the same timeline.
- A tool that is missing from the built-in list can be added with `TABLOG_INTERACTIVE`.

## Output (`~/logs/ENG/`)

```
report.md                    notes + timeline + per-tab sections
commands.jsonl               command index
sessions/<sid>__<tab>.txt    clean transcript
sessions/<sid>__<tab>.log    raw (scriptreplay -T x.timing x.log)
```

Tabs opened before the first `eng NAME` log to `~/logs/general/` and move over once an
engagement is set.

`tablog report` renders tabs in parallel and only rebuilds the `.txt` of tabs whose log changed, so re-running it during an engagement is cheap.

## Settings

Export before the tablog line in your rc:

```sh
export TABLOG_ROOT=~/logs
export TABLOG_QUIET=1        # no banner
export TABLOG_DISABLE=1      # off
export TABLOG_NOLASTDIR=1    # new tabs stay in ~
export TABLOG_INTERACTIVE="tool1 tool2"   # extra shells to split per command
```

## Notes

- Rename tabs with `tab`, not the terminal's rename dialog.
- Use `tabi` or quotes for `>` `<` in names.
- Quote notes that contain `;` `|` `&` `$` `!` or quotes.
- **Everything on screen gets logged, secrets included.** Passwords you type at a visible
  prompt, tokens in command lines and tool output all end up in `~/logs`. Treat that folder
  and the reports like the engagement data they are.
- If logging cannot start (no `script`, log folder not writable), the tab opens normally and
  says so in red.

## Tests

```sh
tests/run.sh
```

Runs the report unit tests and drives real zsh and bash sessions in a throwaway `$HOME`.

## License

MIT, see [LICENSE](LICENSE).
