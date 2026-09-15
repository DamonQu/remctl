"""Shell completion scripts for the remctl command-line interface."""

BASH_COMPLETION = r"""# bash completion for rctl
_rctl_completion()
{
    local cur command subcommand options
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"

    if (( COMP_CWORD == 1 )); then
        COMPREPLY=( $(compgen -W "--help --version add get list ls reindex ssh exec deploy scp rsync delete remove rm del uninstall completion" -- "$cur") )
        return 0
    fi

    command="${COMP_WORDS[1]}"
    case "$command" in
        add)
            options="-h --help --debug"
            ;;
        get)
            options="-h --help"
            ;;
        list|ls|reindex|uninstall)
            options="-h --help"
            ;;
        ssh)
            options="-h --help -u --user"
            ;;
        exec)
            options="-h --help -u --user"
            ;;
        deploy)
            options="-h --help -u --user --post-workflow"
            ;;
        delete|remove|rm|del)
            options="-h --help --all"
            ;;
        completion)
            if (( COMP_CWORD == 2 )); then
                COMPREPLY=( $(compgen -W "bash" -- "$cur") )
            fi
            return 0
            ;;
        scp)
            if (( COMP_CWORD == 2 )); then
                COMPREPLY=( $(compgen -W "push pull" -- "$cur") )
                return 0
            fi
            subcommand="${COMP_WORDS[2]}"
            case "$subcommand" in
                push) options="-h --help -u --user" ;;
                pull) options="-h --help -u --user -r --recursive" ;;
                *) return 0 ;;
            esac
            ;;
        rsync)
            if (( COMP_CWORD == 2 )); then
                COMPREPLY=( $(compgen -W "push pull" -- "$cur") )
                return 0
            fi
            subcommand="${COMP_WORDS[2]}"
            case "$subcommand" in
                push|pull)
                    options="-h --help -u --user --archive --no-archive -z --compress --delete --exclude --dry-run --checksum --partial --bwlimit"
                    ;;
                *) return 0 ;;
            esac
            ;;
        *)
            return 0
            ;;
    esac

    if [[ "$cur" == -* ]]; then
        COMPREPLY=( $(compgen -W "$options" -- "$cur") )
        return 0
    fi

    case "$command:$subcommand" in
        deploy:|scp:push|rsync:push)
            while IFS= read -r candidate; do
                COMPREPLY+=("$candidate")
            done < <(compgen -f -- "$cur")
            ;;
    esac
}

complete -o filenames -F _rctl_completion rctl
"""


def print_completion(shell: str) -> int:
    """Write the requested shell completion script."""
    if shell != "bash":
        raise ValueError(f"unsupported shell: {shell}")
    print(BASH_COMPLETION, end="")
    return 0
