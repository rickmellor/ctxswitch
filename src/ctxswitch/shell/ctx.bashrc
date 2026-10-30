# ctx shell: the rcfile for `ctx shell` — records every command bash runs so the board can show this pane as
# harness "shell" and the hub can recap it. Read by bash after its own init, and it reads the user's ~/.bashrc
# first, so the operator's own setup (aliases, PS1, PROMPT_COMMAND) still applies.
#
# Records (one JSON object per line in $CTX_SHELL_LOG; strings that can hold arbitrary text are base64, so no
# quote, backslash or newline in a command can break a line):
#   {"t":"open","v":1,"ts":..,"cwd":"<b64>","shell":"bash"}  once
#   {"t":"start","n":..,"ts":..,"cwd":"<b64>","cmd":"<b64>","out":<bytes>}   before a command
#   {"t":"end","n":..,"ts":..,"rc":..,"out":<bytes>}                         after it (same n)
#   {"t":"close","ts":..}                                                    on exit
# The command's output is $CTX_SHELL_OUT[start.out:end.out] — tmux pipe-pane appends the pane's bytes there, so
# nothing has to be teed through the shell. Liveness goes to $CTX_SHELL_REG_DIR/shell-<pid>.json, idle/busy.
#
# Needs bash 4.4+ (PS0, EPOCHREALTIME).

# without ctx shell's env there is nothing to record into: stay out of the way
[ -n "${CTX_SHELL_LOG:-}" ] || return 0 2>/dev/null

[ -f ~/.bashrc ] && . ~/.bashrc

# a command that starts with a space stays out of history — and out of the transcript ("secret" lines).
# keep ignoresups when the user had it; HISTTIMEFORMAT must stay empty: history 1 must print "  N  cmd".
HISTCONTROL=ignorespace
case :${HISTCONTROL:-}: in *ignoresups*) HISTCONTROL=$HISTCONTROL:ignoresups ;; esac
shopt -s histappend

# __ctx_b64 <text> -> base64 of its UTF-8 bytes, no wrapping. A failed encode yields "" rather than a line the
# collector can't parse: a missing command is recoverable from the transcript, an unparseable line is not.
__ctx_b64() { local b; b=$(printf '%s' "$1" | base64 -w0 2>/dev/null); printf '%s\n' "${b:-}"; }

# __ctx_outsize -> bytes of the pane capture so far (0 before pipe-pane writes anything, or without one)
__ctx_outsize() { local s; s=$(stat -c %s "$CTX_SHELL_OUT" 2>/dev/null); printf '%s\n' "${s:-0}"; }

# __ctx_now -> epoch seconds with a fraction; __ctx_iso -> the same, as the registry's ISO 8601 UTC
__ctx_now() { local t=$EPOCHREALTIME; printf '%s\n' "${t//,/\\.}"; }
__ctx_iso() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# __ctx_jesc <text> -> "..." safe to put in a JSON string: a path, so only backslash and quote can break it
__ctx_jesc() { local s=${1//\\/\\\\}; printf '"%s"' "${s//\"/\\\"}"; }

# one record per printf, so lines can never interleave
__ctx_rec() { printf '%s\n' "$1" >> "$CTX_SHELL_LOG"; }

# __ctx_reg <busy|idle>: rewrite the liveness entry atomically (the collector drops a pid whose /proc start time
# no longer matches procStart, so a recycled pid can't inherit this shell's entry)
__ctx_reg() {
    local st=$1 ts ps
    ts=$(__ctx_iso)
    # field 22 of /proc/$$/stat: the 20th after the last ") " (comm may hold spaces and parens) — liveness.proc_start
    ps=$(awk '{ s=$0; sub(/^.*\) /, "", s); split(s, f, " "); print f[20] }' "/proc/$$/stat" 2>/dev/null)
    # the umask keeps the tmp file 0600, so the entry is never briefly readable by anyone else
    ( umask 077
      printf '{"sessionId":"%s","pid":%s,"procStart":"%s","status":"%s","statusSince":"%s","cwd":%s}\n' \
          "$CTX_SHELL_ID" "$$" "${ps:-}" "$st" "$ts" "$(__ctx_jesc "$PWD")" > "$CTX_SHELL_REG_DIR/shell-$$.json.tmp" \
          && mv -f "$CTX_SHELL_REG_DIR/shell-$$.json.tmp" "$CTX_SHELL_REG_DIR/shell-$$.json" ) 2>/dev/null
    return 0
}

# The history number of the newest history entry ("  N  cmd" -> N). Empty when history holds nothing.
__ctx_num() { HISTTIMEFORMAT= history 1 2>/dev/null | sed -n 's/^[^0-9]*\([0-9][0-9]*\).*/\1/p'; }

# The newest history number this shell has seen, in a file: PS0 runs in a subshell, so it can write files but
# cannot hand a variable back to PROMPT_COMMAND.
__ctx_nfile() { printf '%s\n' "$(__ctx_num)" > "$CTX_SHELL_LOG.n" 2>/dev/null; }

# before a command. PS0 is expanded after the line is read and before it runs, in a subshell — expansion only,
# so it takes the work through a command substitution, which prints nothing into the pane.
# A line only reaches history when the prompt saw it: the newest history number grew since the last prompt.
# That is what keeps a leading-space command (ignorespace) and an empty Enter out of the transcript.
__ctx_cmd() { HISTTIMEFORMAT= history 1 2>/dev/null | sed 's/^[^0-9]*[0-9][0-9]*[[:space:]]*//'; }

__ctx_ps0() {
    local c n cmd
    c=$(__ctx_num)
    n=$(cat "$CTX_SHELL_LOG.n" 2>/dev/null)
    if [ -n "$c" ] && [ "${n:-0}" -lt "$c" ] 2>/dev/null; then
        cmd=$(__ctx_cmd)
        __ctx_rec "{\"t\":\"start\",\"n\":$c,\"ts\":$(__ctx_now),\"cwd\":\"$(__ctx_b64 "$PWD")\",\"cmd\":\"$(__ctx_b64 "$cmd")\",\"out\":$(__ctx_outsize)}"
        printf '%s\n' "$c" > "$CTX_SHELL_LOG.started" 2>/dev/null
        __ctx_reg busy
    fi
    :                                                   # PS0's expansion prints: nothing here may print
}

# after a command: close the start we wrote (if any) and leave $? as the user's prompt expects to find it.
__ctx_end() {
    local rc=$? n out
    if [ -f "$CTX_SHELL_LOG.started" ]; then
        n=$(<"$CTX_SHELL_LOG.started")
        out=$(__ctx_outsize)
        __ctx_rec "{\"t\":\"end\",\"n\":${n:-0},\"ts\":$(__ctx_now),\"rc\":$rc,\"out\":$out}"
        rm -f "$CTX_SHELL_LOG.started"
        __ctx_reg idle
        [ "$out" -gt "${CTX_SHELL_MAX_OUT:-16777216}" ] && : > "$CTX_SHELL_OUT"    # keep the capture small; offsets restart at 0
    fi
    __ctx_nfile
    return $rc
}

# PS0's contents are expanded and *printed*, not executed, so the hook goes in as a command substitution: it
# runs in a subshell before the command, and its own output is captured instead of echoed into the pane.
PS0='$(__ctx_ps0)'
case ${PROMPT_COMMAND-} in
    "") PROMPT_COMMAND=__ctx_end ;;
    *)  PROMPT_COMMAND=__ctx_end$'\n'"$PROMPT_COMMAND" ;;
esac
__ctx_nfile                                              # where history stands now: don't replay what's already in it
__ctx_rec "{\"t\":\"open\",\"v\":1,\"ts\":$(__ctx_now),\"cwd\":\"$(__ctx_b64 "$PWD")\",\"shell\":\"bash\"}"
__ctx_reg idle
trap '__ctx_rec "{\"t\":\"close\",\"ts\":$(__ctx_now)}"; rm -f "$CTX_SHELL_REG_DIR/shell-$$.json" "$CTX_SHELL_LOG.n" "$CTX_SHELL_LOG.started"' EXIT
