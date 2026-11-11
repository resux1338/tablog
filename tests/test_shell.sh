#!/usr/bin/env bash
# Drives real interactive zsh and bash sessions with tablog loaded, in a throwaway $HOME,
# and checks what ends up in the index, the log folder and the report.
set -u
src=$(cd "$(dirname "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fails=0

ok()   { printf '  ok    %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; fails=$((fails + 1)); }
has()  { if grep -qF -- "$2" "$1" 2>/dev/null; then ok "$3"; else fail "$3"; fi; }
hasnt() { if grep -qF -- "$2" "$1" 2>/dev/null; then fail "$3"; else ok "$3"; fi; }

# session SHELL HOME: types the lines on stdin into an interactive shell on a pty
session() {
  local sh=$1 h=$2 line
  mkdir -p "$h/work"
  echo ". $src/tablog.sh" > "$h/.zshrc"
  echo ". $src/tablog.sh" > "$h/.bashrc"
  { sleep 1.5; while IFS= read -r line; do printf '%s\n' "$line"; sleep 0.4; done; } |
    env -i HOME="$h" PATH="$PATH" TERM=xterm USER=tester LANG=C.UTF-8 SHELL="$(command -v "$sh")" \
      script -q -c "cd; $sh -i" /dev/null > "$h/screen.out" 2>&1
}

if ! command -v script >/dev/null 2>&1; then
  echo "shell tests skipped: script(1) not installed"
  exit 0
fi

for sh in zsh bash; do
  if ! command -v "$sh" >/dev/null 2>&1; then
    echo "$sh: not installed, skipped"
    continue
  fi
  echo "$sh:"
  h=$tmp/$sh
  session "$sh" "$h" <<'EOF'
echo early-in-general
eng exam
tab 10.10.11.5-web
echo hello-from-tab
cd work
false
printf '\e[31mred-text\e[0m\n'; seq 1 200
note found creds admin:S3cret
note "quoted; note | with pipe"
tabi VPN+UP
echo "quo\"te"
tablog status
exit
EOF
  idx=$h/logs/exam/commands.jsonl
  rep=$h/logs/exam/report.md

  if [[ -s $idx ]]; then ok "index written"; else fail "index written"; fi
  if python3 -c 'import json,sys; [json.loads(l) for l in open(sys.argv[1])]' "$idx" 2>/dev/null; then
    ok "index is valid JSON lines"
  else
    fail "index is valid JSON lines"
  fi
  has "$idx" '"ev":"start"' "start event"
  has "$idx" '"cmd":"echo early-in-general"' "commands from before eng moved to the engagement"
  if [[ ! -e $h/logs/general ]]; then ok "empty general folder removed"; else fail "empty general folder removed"; fi
  has "$idx" '"rc":1' "exit code recorded"
  has "$idx" '"cwd":"~/work"' "directory recorded"
  has "$idx" '"ev":"tab"' "tab event"
  has "$idx" '"tab":">VPN+UP<"' "tabi wraps the name"
  has "$idx" '"text":"found creds admin:S3cret"' "note event"
  has "$idx" '"text":"quoted; note | with pipe"' "quoted note"
  has "$idx" 'echo \"quo\\\"te\"' "quotes escaped in the index"
  if ls "$h"/logs/exam/sessions/*___VPN+UP_.log >/dev/null 2>&1; then ok "log renamed after the tab"; else fail "log renamed after the tab"; fi
  if ls "$h"/logs/exam/sessions/*.timing >/dev/null 2>&1; then ok "timing file"; else fail "timing file"; fi
  has "$h/screen.out" "session:" "tablog status prints"

  if python3 "$src/tablog-report.py" report --root "$h/logs" exam >/dev/null; then ok "report builds"; else fail "report builds"; fi
  has "$rep" '## Notes' "report: notes section"
  has "$rep" '**note:** found creds admin:S3cret' "report: note in timeline"
  has "$rep" 'hello-from-tab' "report: command output"
  has "$rep" 'red-text' "report: colour codes stripped"
  hasnt "$rep" $'\e' "report: no escape sequences"
  has "$rep" 'lines omitted' "report: long output truncated"
  has "$rep" '**exit 1**' "report: failed command marked"
  has "$rep" '*previously: 10.10.11.5-web*' "report: earlier tab name"
  hasnt "$rep" '$ tab 10.10.11.5-web' "report: tablog's own commands left out"
  if ls "$h"/logs/exam/sessions/*.txt >/dev/null 2>&1; then ok "transcript written"; else fail "transcript written"; fi
  if python3 "$src/tablog-report.py" find --root "$h/logs" --eng exam 'creds' | grep -q 'note'; then
    ok "find: matches notes"
  else
    fail "find: matches notes"
  fi

  # a shell that cannot log must still open
  h2=$tmp/$sh-ro
  mkdir -p "$h2/logs" && chmod 555 "$h2/logs"
  session "$sh" "$h2" <<'EOF'
echo still-alive
exit
EOF
  chmod 755 "$h2/logs"
  if [[ $(id -u) != 0 ]]; then
    has "$h2/screen.out" "NOT LOGGING" "unwritable log folder: warns"
    has "$h2/screen.out" "still-alive" "unwritable log folder: shell still works"
  fi
done

if (( fails )); then
  echo "shell tests: $fails failed"
  exit 1
fi
echo "shell tests: all passed"
