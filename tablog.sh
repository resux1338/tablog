# tablog: per-tab terminal logging for zsh and bash (tmux-aware)
#
# Install: source this file at the END of ~/.zshrc and ~/.bashrc
#   [ -f ~/.local/share/tablog/tablog.sh ] && . ~/.local/share/tablog/tablog.sh
#
# Commands:
#   tab NAME        name this tab (sets terminal title + log tag, renames log file)
#   tabi NAME       infra tab, wrapped as >NAME<  (tabi VPN+UP  ->  >VPN+UP<)
#   eng NAME        set engagement for this tab, new tabs and still-unassigned tabs;
#                   new tabs open in the directory it was run in
#   note TEXT       timestamped remark in the report timeline (findings, creds, flags)
#   tablog report   build report.md + clean .txt transcripts (interactive shells split per remote command)
#   tablog find X   search commands and notes across the engagement
#   tablog status   show what this tab is logging to
#
# Settings (export before sourcing):
#   TABLOG_ROOT=~/logs   TABLOG_DISABLE=1   TABLOG_QUIET=1   TABLOG_NOLASTDIR=1

[[ $- == *i* ]] || return 0
[[ -n ${TABLOG_DISABLE-} ]] && return 0
# real terminals only: `zsh -ic CMD` / `bash -ic CMD` and piped shells must run untouched
[[ -t 0 && -t 1 && -z ${ZSH_EXECUTION_STRING-}${BASH_EXECUTION_STRING-} ]] || return 0

: "${TABLOG_ROOT:=$HOME/logs}"

if [[ -n ${ZSH_VERSION-} ]]; then
  eval '_tl_dir=${${(%):-%x}:A:h}'
  _tl_sh=zsh
  _tl_tty=$TTY
  zmodload zsh/datetime 2>/dev/null
else
  _tl_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
  _tl_sh=bash
  _tl_tty=$(tty 2>/dev/null)
fi

# ---------- helpers ----------
_tl_cur_eng() {
  local e=
  [[ -r $TABLOG_ROOT/.engagement ]] && IFS= read -r e < "$TABLOG_ROOT/.engagement"
  REPLY=${e:-general}
}
_tl_slug() { REPLY=${1//[^A-Za-z0-9+._-]/_}; }
_tl_now() { REPLY=${EPOCHREALTIME/,/.}; }   # some locales use a decimal comma
_tl_json() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  s=${s//$'\n'/\\n}
  s=${s//$'\t'/\\t}
  s=${s//$'\r'/\\r}
  s=${s//[[:cntrl:]]/}
  REPLY=$s
}
_tl_lhost() {
  local _ a=
  REPLY=
  # no VPN up (the usual case when a tab opens): answer without starting ip(8)
  [[ -d /sys/class/net && ! -e /sys/class/net/tun0 ]] && return 0
  read -r _ _ _ a _ < <(ip -4 -o addr show dev tun0 2>/dev/null)
  REPLY=${a%%/*}
}
# timestamp for the session id, without starting date(1)
_tl_stamp() {
  if [[ $_tl_sh == zsh ]]; then
    REPLY='%D{%Y%m%d-%H%M%S}'; eval 'REPLY=${(%)REPLY}'
  else
    printf -v REPLY '%(%Y%m%d-%H%M%S)T' -1 2>/dev/null
  fi
  [[ $REPLY == [0-9]*-[0-9]* ]] || REPLY=$(date +%Y%m%d-%H%M%S)
}
_tl_warn() { printf '\e[1;31m[tablog] NOT LOGGING this tab: %s\e[0m\n' "$1" >&2; }

# ---------- phase 1: start capturing this tab ----------
# The session arrives through the environment. It is ours only on the terminal of the live
# shell that owns it. Anywhere else (new tmux pane, terminal or IDE started from a logged
# tab, screen window, vim :terminal) this shell is a tab of its own.
if [[ -n ${TABLOG_SID-} && -n ${TABLOG_OWNER-} && $TABLOG_OWNER != "$$" ]] &&
   { [[ ${TABLOG_TTY-} != "$_tl_tty" ]] || ! kill -0 "$TABLOG_OWNER" 2>/dev/null; }; then
  unset TABLOG_SID
fi

if [[ -z ${TABLOG_SID-} ]]; then
  unset TABLOG_OWNER TABLOG_FILE TABLOG_TTY TABLOG_PANE TABLOG_N TABLOG_TAB
  _tl_cur_eng; export TABLOG_ENG=$REPLY
  _tl_stamp; TABLOG_SID="$REPLY-$$"
  TABLOG_FILE="$TABLOG_ROOT/$TABLOG_ENG/sessions/$TABLOG_SID.log"
  _tl_bin=$(command -v $_tl_sh 2>/dev/null)
  _tl_why=
  # Check everything script(1) needs before exec'ing into it: if it cannot start, the tab
  # closes, and so would every tab, console and ssh login after it.
  if ! mkdir -p "${TABLOG_FILE%/*}" 2>/dev/null || ! { true >> "$TABLOG_FILE"; } 2>/dev/null; then
    _tl_why="cannot write to ${TABLOG_FILE%/*}"
  elif [[ -n ${TMUX-} && -n ${TMUX_PANE-} &&
          $(tmux display -p -t "$TMUX_PANE" '#{pane_tty}' 2>/dev/null) == "$_tl_tty" ]]; then
    export TABLOG_SID TABLOG_FILE TABLOG_MODE=tmux TABLOG_PANE=$TMUX_PANE
    tmux pipe-pane -t "$TMUX_PANE" "exec cat >> '$TABLOG_FILE'"
  elif ! command -v script >/dev/null 2>&1; then
    _tl_why="script(1) not found"
  elif _tl_d=$(script --help 2>&1); [[ $_tl_d != *--log-timing* || $_tl_d != *--log-out* ]]; then
    _tl_why="script(1) lacks --log-timing/--log-out (needs util-linux 2.35+)"
  elif [[ ! -x $_tl_bin ]]; then
    _tl_why="cannot find the $_tl_sh binary"
  else
    # The terminal opens the next tab in the directory of script(1), not of the shell inside
    # it. Park script(1) in ~ so every new tab starts in ~ and picks its directory in phase 2
    # (were it left elsewhere, all later tabs would be stuck in that one directory).
    if [[ -z ${TABLOG_NOLASTDIR-} ]]; then
      export TABLOG_START=$PWD
      cd -- "$HOME" 2>/dev/null
    fi
    export TABLOG_SID TABLOG_FILE TABLOG_MODE=script TABLOG_REALSHELL=${SHELL-}
    # script(1) starts "$SHELL -i"; force the shell we are running in
    SHELL=$_tl_bin exec script -q -f \
      --log-timing "${TABLOG_FILE%.log}.timing" -O "$TABLOG_FILE"
    _tl_why="could not exec script(1)"   # only reached when exec itself failed
    [[ -n $TABLOG_REALSHELL ]] && SHELL=$TABLOG_REALSHELL
    [[ -n ${TABLOG_START-} ]] && cd -- "$TABLOG_START" 2>/dev/null
    unset TABLOG_MODE TABLOG_REALSHELL TABLOG_START
  fi
  unset _tl_d _tl_bin
  if [[ -n $_tl_why ]]; then
    _tl_warn "$_tl_why"
    [[ -s $TABLOG_FILE ]] || rm -f -- "$TABLOG_FILE" 2>/dev/null
    unset TABLOG_SID TABLOG_FILE _tl_why
    return 0
  fi
  unset _tl_why
fi

# ---------- phase 2: hooks (only in the shell that owns the session) ----------
if [[ -z ${TABLOG_OWNER-} ]]; then
  export TABLOG_OWNER=$$
  _tl_new=1
elif [[ $TABLOG_OWNER != "$$" ]]; then
  return 0   # nested shell: its activity is already inside the parent command's output
else
  _tl_new=   # rc sourced again, or `exec zsh`: carry on with the same session
fi
[[ -n ${TABLOG_REALSHELL-} ]] && export SHELL=$TABLOG_REALSHELL

# startdir: a new tab that was opened in ~ goes to the directory `eng NAME` was run in, or
# else (not under tmux, which has its own start directory) to the last one any tab was in.
# A tab opened somewhere else on purpose stays there.
if [[ -n $_tl_new && -z ${TABLOG_NOLASTDIR-} ]]; then
  _tl_d=${TABLOG_START:-$PWD}
  if [[ $_tl_d == "$HOME" ]]; then
    _tl_d=
    [[ -r $TABLOG_ROOT/.engdir ]] && IFS= read -r _tl_d < "$TABLOG_ROOT/.engdir"
    if [[ ! -d $_tl_d && $TABLOG_MODE == script && -r $TABLOG_ROOT/.lastdir ]]; then
      IFS= read -r _tl_d < "$TABLOG_ROOT/.lastdir"
    fi
  fi
  [[ -d $_tl_d && $_tl_d != "$PWD" ]] && cd -- "$_tl_d"
  unset _tl_d
fi
unset TABLOG_START

# exported: the tab name and the command counter have to survive `exec zsh`
export TABLOG_TTY=$_tl_tty TABLOG_TAB=${TABLOG_TAB-} TABLOG_N=${TABLOG_N:-${_tl_n:-0}}
_tl_live=${_tl_live-}

_tl_emit() { printf '%s\n' "$1" >> "$TABLOG_ROOT/$TABLOG_ENG/commands.jsonl"; }

_tl_start_event() {
  local lh u h; _tl_lhost; lh=$REPLY; _tl_now
  _tl_emit "{\"ev\":\"start\",\"sid\":\"$TABLOG_SID\",\"t\":$REPLY,\"sh\":\"$_tl_sh\",\"mode\":\"$TABLOG_MODE\",\"user\":\"${USER-}\",\"host\":\"${HOSTNAME:-${HOST:-$(hostname)}}\",\"lhost\":\"$lh\"}"
}

_tl_set_tab() {
  local new=$1 old=$TABLOG_TAB dir base slug lh t o
  [[ $new == "$old" ]] && return 0
  TABLOG_TAB=$new
  _tl_slug "$new"; slug=$REPLY
  dir=${TABLOG_FILE%/*}
  base="$dir/$TABLOG_SID${slug:+__$slug}"
  if [[ $base.log != "$TABLOG_FILE" ]] && mv -f -- "$TABLOG_FILE" "$base.log" 2>/dev/null; then
    [[ -e ${TABLOG_FILE%.log}.timing ]] && mv -f -- "${TABLOG_FILE%.log}.timing" "$base.timing"
    TABLOG_FILE=$base.log
  fi
  _tl_lhost; lh=$REPLY
  _tl_json "$new"; t=$REPLY
  _tl_json "$old"; o=$REPLY
  _tl_now
  _tl_emit "{\"ev\":\"tab\",\"sid\":\"$TABLOG_SID\",\"t\":$REPLY,\"tab\":\"$t\",\"prev\":\"$o\",\"lhost\":\"$lh\"}"
}

_tl_move_eng() {
  local e=$1 old="$TABLOG_ROOT/$TABLOG_ENG" new="$TABLOG_ROOT/$1" f
  [[ $e == "$TABLOG_ENG" ]] && return 0
  mkdir -p "$new/sessions" || return 1
  for f in "$TABLOG_FILE" "${TABLOG_FILE%.log}.timing"; do
    [[ -e $f ]] && mv -f -- "$f" "$new/sessions/"
  done
  TABLOG_FILE="$new/sessions/${TABLOG_FILE##*/}"
  if [[ -f $old/commands.jsonl ]]; then
    grep -F "\"sid\":\"$TABLOG_SID\"" "$old/commands.jsonl" >> "$new/commands.jsonl"
    grep -vF "\"sid\":\"$TABLOG_SID\"" "$old/commands.jsonl" > "$old/.commands.tmp.$$"
    mv -f -- "$old/.commands.tmp.$$" "$old/commands.jsonl"
    # nothing left behind: no empty index or folders for an engagement that was never used
    [[ -s $old/commands.jsonl ]] || rm -f -- "$old/commands.jsonl"
    rmdir -- "$old/sessions" "$old" 2>/dev/null
  fi
  TABLOG_ENG=$e
}

_tl_title() {
  [[ -z $TABLOG_TAB || $TABLOG_MODE == tmux ]] && return 0
  if [[ $_tl_sh == zsh ]]; then
    printf '\e]0;%s\a' "$TABLOG_TAB"
  else
    # bash draws the title from PS1 after PROMPT_COMMAND, so append ours to PS1 (last one wins)
    local sfx='\[\e]0;${TABLOG_TAB}\a\]'
    [[ $PS1 == *"$sfx" ]] || PS1+=$sfx
  fi
}

# tmux: pick up manual window renames (only when automatic-rename is off for the window)
_tl_tmux_sync() {
  [[ $TABLOG_MODE == tmux ]] || return 0
  local w; w=$(tmux display -p -t "$TMUX_PANE" '#{?#{automatic-rename},,#W}' 2>/dev/null)
  [[ -n $w && $w != "$TABLOG_TAB" ]] && _tl_set_tab "$w"
}

_tl_pre() {
  local tb
  _tl_tmux_sync
  TABLOG_N=$((TABLOG_N + 1))
  _tl_cmd=$1; _tl_pwd=$PWD; _tl_live=1
  [[ $PWD == "$HOME" || $PWD == "$HOME"/* ]] && _tl_pwd="~${PWD#"$HOME"}"
  _tl_json "$_tl_cmd"; _tl_jcmd=$REPLY
  _tl_json "$_tl_pwd"; _tl_jpwd=$REPLY
  _tl_json "$TABLOG_TAB"; tb=$REPLY
  _tl_now; _tl_t0=$REPLY
  # on record before it runs: a command that never returns to the prompt (listener still
  # open, tab closed) gets no "cmd" event
  _tl_emit "{\"ev\":\"run\",\"sid\":\"$TABLOG_SID\",\"n\":$TABLOG_N,\"t0\":$_tl_t0,\"tab\":\"$tb\",\"cwd\":\"$_tl_jpwd\",\"cmd\":\"$_tl_jcmd\"}"
  # invisible marker: lets the report cut each command's output out of the raw log exactly
  printf '\e]6973;S;%s;%s\a' "$TABLOG_SID" "$TABLOG_N"
}

_tl_post() {
  local rc=$1 tb e
  if [[ -n $_tl_live ]]; then
    _tl_live=
    printf '\e]6973;E;%s;%s;%s\a' "$TABLOG_SID" "$TABLOG_N" "$rc"
    _tl_json "$TABLOG_TAB"; tb=$REPLY
    _tl_now
    _tl_emit "{\"ev\":\"cmd\",\"sid\":\"$TABLOG_SID\",\"n\":$TABLOG_N,\"t0\":$_tl_t0,\"t1\":$REPLY,\"rc\":$rc,\"tab\":\"$tb\",\"cwd\":\"$_tl_jpwd\",\"cmd\":\"$_tl_jcmd\"}"
  fi
  _tl_tmux_sync
  # lastdir: remember where this tab is (tmux tracks pane dirs itself)
  if [[ -z ${TABLOG_NOLASTDIR-} && $TABLOG_MODE == script && $PWD != "${_tl_lastpwd-}" ]]; then
    _tl_lastpwd=$PWD
    printf '%s\n' "$PWD" > "$TABLOG_ROOT/.lastdir"
  fi
  # unassigned tabs follow the engagement set with `eng` in another tab
  if [[ $TABLOG_ENG == general ]]; then
    _tl_cur_eng; e=$REPLY
    [[ $e != general ]] && _tl_move_eng "$e"
  fi
  _tl_title
}

# ---------- user commands ----------
tab() {
  local new="$*"
  new=${new//[[:cntrl:]]/}
  if [[ -z $new ]]; then
    printf 'tab: %s\n' "${TABLOG_TAB:-<unnamed>}"
    return 0
  fi
  [[ $TABLOG_MODE == tmux ]] && tmux rename-window -t "$TMUX_PANE" -- "$new"
  _tl_set_tab "$new"
  _tl_title
}
tabi() { tab ">$*<"; }

note() {
  local tb tx
  if [[ -z $* ]]; then
    printf 'usage: note TEXT   (quote it if it has ; | & $ or other shell characters)\n' >&2
    return 1
  fi
  _tl_json "$TABLOG_TAB"; tb=$REPLY
  _tl_json "$*"; tx=$REPLY
  _tl_now
  _tl_emit "{\"ev\":\"note\",\"sid\":\"$TABLOG_SID\",\"t\":$REPLY,\"tab\":\"$tb\",\"text\":\"$tx\"}"
  printf '\e[2m[tablog] noted\e[0m\n'
}
[[ $_tl_sh == zsh ]] && alias note='noglob note'   # note is it root? / note 10.10.11.* done

eng() {
  if [[ -z ${1-} ]]; then
    _tl_cur_eng
    printf 'this tab: %s   new tabs: %s\n' "$TABLOG_ENG" "$REPLY"
    return 0
  fi
  _tl_slug "$1"
  mkdir -p "$TABLOG_ROOT/$REPLY/sessions" && printf '%s\n' "$REPLY" > "$TABLOG_ROOT/.engagement"
  # new tabs open here; run from ~ there is no such place, and they follow the last dir again
  if [[ $PWD == "$HOME" ]]; then
    rm -f -- "$TABLOG_ROOT/.engdir"
  else
    printf '%s\n' "$PWD" > "$TABLOG_ROOT/.engdir"
  fi
  _tl_move_eng "$REPLY"
  printf 'engagement: %s\n' "$TABLOG_ENG"
}

tablog() {
  local py="$_tl_dir/tablog-report.py"
  case ${1-status} in
    report) shift; python3 "$py" report --root "$TABLOG_ROOT" "${1:-$TABLOG_ENG}" "${@:2}" ;;
    find)   shift; python3 "$py" find --root "$TABLOG_ROOT" --eng "$TABLOG_ENG" "$@" ;;
    status)
      printf 'engagement: %s\ntab:        %s\nsession:    %s (%s, %s)\nlog:        %s\nindex:      %s\n' \
        "$TABLOG_ENG" "${TABLOG_TAB:-<unnamed>}" "$TABLOG_SID" "$_tl_sh" "$TABLOG_MODE" \
        "$TABLOG_FILE" "$TABLOG_ROOT/$TABLOG_ENG/commands.jsonl" ;;
    *) cat <<'EOH'
tab NAME         name this tab (title + log tag); no NAME shows current
tabi NAME        infra tab, shown as >NAME<
eng NAME         set engagement for this, new and unassigned tabs; no NAME shows current
                 (new tabs open in the directory you run it in)
note TEXT        timestamped remark in the report (findings, creds, flags)
tablog status    where this tab logs
tablog report    build ~/logs/<eng>/report.md  [ENG] [--local] [--max-lines N] [--max-lines-remote N]
tablog find RX   search commands and notes  [--all]
EOH
    ;;
  esac
}

# ---------- wire up the hooks ----------
if [[ $_tl_sh == zsh ]]; then
  autoload -Uz add-zsh-hook
  _tl_zsh_preexec() { _tl_pre "${1:-$3}"; }
  _tl_zsh_precmd()  { _tl_post $?; }
  add-zsh-hook preexec _tl_zsh_preexec
  add-zsh-hook precmd  _tl_zsh_precmd
else
  _tl_armed=
  _tl_histnum() {
    local h; h=$(HISTTIMEFORMAT= builtin history 1)
    if [[ $h =~ ^[[:space:]]*([0-9]+)[*]?[[:space:]]+(.*)$ ]]; then
      _tl_hn=${BASH_REMATCH[1]}; _tl_hc=${BASH_REMATCH[2]}
    else
      _tl_hn=; _tl_hc=
    fi
  }
  _tl_debug() {
    [[ -n $_tl_armed && -z ${COMP_LINE-} ]] || return 0
    _tl_armed=
    local last=$_tl_hn c
    _tl_histnum
    if [[ -n $_tl_hn && $_tl_hn != "$last" ]]; then
      c=$_tl_hc
    elif [[ -n ${_tl_cmd-} && $_tl_cmd == "$BASH_COMMAND"* ]]; then
      c=$_tl_cmd            # ignoredups: same line as before
    else
      c=$BASH_COMMAND       # not in history (ignorespace etc.): first simple command only
    fi
    _tl_pre "$c"
  }
  _tl_bash_precmd() { _tl_post $?; }
  _tl_arm() { _tl_armed=1; }
  _tl_histnum
  if [[ ${PROMPT_COMMAND[*]-} == *_tl_bash_precmd* ]]; then
    :   # rc sourced again: already hooked
  elif [[ $(declare -p PROMPT_COMMAND 2>/dev/null) == "declare -a"* ]]; then
    PROMPT_COMMAND=(_tl_bash_precmd "${PROMPT_COMMAND[@]}" _tl_arm)
  else
    PROMPT_COMMAND="_tl_bash_precmd"$'\n'"${PROMPT_COMMAND-}"$'\n'"_tl_arm"
  fi
  trap '_tl_debug' DEBUG
fi

[[ -n $_tl_new ]] && _tl_start_event
unset _tl_new
if [[ -z ${TABLOG_QUIET-} ]]; then
  # one line: where we log + the commands (bright) as a reminder; full path: tablog status
  printf '\e[2m[tablog] %s · %s ·\e[0m tab\e[2m NAME ·\e[0m tabi\e[2m NAME ·\e[0m eng\e[2m NAME ·\e[0m note\e[2m TEXT ·\e[0m tablog\e[2m status|report|find|help\e[0m\n' \
    "$TABLOG_ENG" "$TABLOG_SID"
fi
