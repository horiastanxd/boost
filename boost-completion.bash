# Bash completion for Boost power utility commands

_boost_completions() {
    local cur prev opts
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    opts="--status --version --force --help -h"

    if [[ ${cur} == -* ]] ; then
        mapfile -t COMPREPLY < <(compgen -W "${opts}" -- "${cur}")
        return 0
    fi
}
complete -F _boost_completions boost powersave silent restore

_summer_completions() {
    local cur prev opts
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    opts="on off status --version --help -h"

    mapfile -t COMPREPLY < <(compgen -W "${opts}" -- "${cur}")
    return 0
}
complete -F _summer_completions summer

_auto_completions() {
    local cur prev opts
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"
    
    # Subcommands
    opts="web open dashboard close-web setup doctor modes on off mode quiet-hours summer-nights snooze today-off resume stats report logs config fans sensors gpu-limit help -h --help"

    # If completed first word
    if [ "$COMP_CWORD" -eq 1 ]; then
        mapfile -t COMPREPLY < <(compgen -W "${opts}" -- "${cur}")
        return 0
    fi

    # Subcommands with values
    case "${prev}" in
        mode)
            local modes="dynamic gaming creator quiet off"
            mapfile -t COMPREPLY < <(compgen -W "${modes}" -- "${cur}")
            return 0
            ;;
        snooze)
            local durations="30m 1h 2h 4h"
            mapfile -t COMPREPLY < <(compgen -W "${durations}" -- "${cur}")
            return 0
            ;;
        summer-nights)
            local actions="on off"
            mapfile -t COMPREPLY < <(compgen -W "${actions}" -- "${cur}")
            return 0
            ;;
        quiet-hours)
            local q_opts="off"
            mapfile -t COMPREPLY < <(compgen -W "${q_opts}" -- "${cur}")
            return 0
            ;;
        fans)
            local fan_opts="status on off init calibrate preset test failsafe"
            mapfile -t COMPREPLY < <(compgen -W "${fan_opts}" -- "${cur}")
            return 0
            ;;
        sensors)
            mapfile -t COMPREPLY < <(compgen -W "list json missing" -- "${cur}")
            return 0
            ;;
        gpu-limit)
            mapfile -t COMPREPLY < <(compgen -W "auto" -- "${cur}")
            return 0
            ;;
        preset)
            mapfile -t COMPREPLY < <(compgen -W "silent balanced aggressive" -- "${cur}")
            return 0
            ;;
        *)
            ;;
    esac
}
complete -F _auto_completions auto
