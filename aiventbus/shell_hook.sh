# AI Event Bus — shell preexec hook
# Sends every command to the event bus as a terminal.command event.
#
# Install:  eval "$(aibus shell-hook)"
# Or add to ~/.bashrc / ~/.zshrc:
#   eval "$(aibus shell-hook)"

__aibus_url="${AIVENTBUS_URL:-http://localhost:8420}"

if [ -n "$ZSH_VERSION" ]; then
    # ── ZSH ──────────────────────────────────────────────────────────
    __aibus_preexec() {
        local cmd="$1"
        # Skip our own curl calls
        [[ "$cmd" == *"__aibus"* ]] && return
        [[ "$cmd" == *"/api/v1/events"* ]] && return
        # Escape for JSON: backslash, double-quote, newlines, tabs
        local escaped
        escaped=$(printf '%s' "$cmd" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g' | tr '\n' ' ')
        # Fire and forget — backgrounded, no output
        curl -s -X POST "${__aibus_url}/api/v1/events" \
            -H 'Content-Type: application/json' \
            -d "{\"topic\":\"terminal.command\",\"payload\":{\"command\":\"${escaped}\",\"shell\":\"${SHELL}\",\"cwd\":\"${PWD}\"},\"priority\":\"low\",\"source\":\"hook:preexec\"}" \
            >/dev/null 2>&1 &
        disown 2>/dev/null
    }
    autoload -Uz add-zsh-hook 2>/dev/null
    add-zsh-hook preexec __aibus_preexec

elif [ -n "$BASH_VERSION" ]; then
    # ── BASH ─────────────────────────────────────────────────────────
    # Bash doesn't have native preexec, use DEBUG trap + PROMPT_COMMAND guard
    __aibus_last_cmd=""
    __aibus_debug_trap() {
        # Only fire once per prompt cycle
        [ "$BASH_COMMAND" = "$PROMPT_COMMAND" ] && return
        local cmd
        cmd=$(HISTTIMEFORMAT='' history 1 | sed 's/^ *[0-9]* *//')
        # Dedupe — don't re-send the same command
        [ "$cmd" = "$__aibus_last_cmd" ] && return
        __aibus_last_cmd="$cmd"
        # Skip our own calls
        [[ "$cmd" == *"__aibus"* ]] && return
        [[ "$cmd" == *"/api/v1/events"* ]] && return
        local escaped
        escaped=$(printf '%s' "$cmd" | sed 's/\\/\\\\/g; s/"/\\"/g; s/\t/\\t/g' | tr '\n' ' ')
        curl -s -X POST "${__aibus_url}/api/v1/events" \
            -H 'Content-Type: application/json' \
            -d "{\"topic\":\"terminal.command\",\"payload\":{\"command\":\"${escaped}\",\"shell\":\"${SHELL}\",\"cwd\":\"${PWD}\"},\"priority\":\"low\",\"source\":\"hook:preexec\"}" \
            >/dev/null 2>&1 &
        disown 2>/dev/null
    }
    trap '__aibus_debug_trap' DEBUG
fi
