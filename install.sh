#!/usr/bin/env bash
# Installs tablog into ~/.local/share/tablog and hooks it into ~/.zshrc and ~/.bashrc.
#   ./install.sh              install or update
#   ./install.sh --uninstall  remove the files and the rc hooks (logs are kept)
set -euo pipefail
src=$(cd "$(dirname "$0")" && pwd)
dst="$HOME/.local/share/tablog"
line='[ -f ~/.local/share/tablog/tablog.sh ] && . ~/.local/share/tablog/tablog.sh  # tablog: keep last'

if [ "${1-}" = "--uninstall" ]; then
  for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
    [ -f "$rc" ] || continue
    if grep -q 'share/tablog/tablog.sh' "$rc"; then
      cp -p -- "$rc" "$rc.tablog-bak"
      grep -v 'share/tablog/tablog.sh' "$rc.tablog-bak" > "$rc" || true
      echo "unhooked: $rc (backup: $rc.tablog-bak)"
    fi
  done
  rm -f -- "$dst/tablog.sh" "$dst/tablog-report.py"
  rmdir -- "$dst" 2>/dev/null || true
  echo "done. Your logs were not touched. Tabs that are already open keep logging until closed."
  exit 0
elif [ -n "${1-}" ]; then
  echo "usage: $0 [--uninstall]" >&2
  exit 2
fi

mkdir -p "$dst"
install -m 644 "$src/tablog.sh" "$dst/tablog.sh"
install -m 755 "$src/tablog-report.py" "$dst/tablog-report.py"

for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
  [ -f "$rc" ] || continue
  if grep -q 'share/tablog/tablog.sh' "$rc"; then
    echo "already hooked: $rc"
  else
    printf '\n%s\n' "$line" >> "$rc"
    echo "hooked: $rc"
  fi
done
echo "done. Open a new terminal tab to start logging."
