#!/usr/bin/env bash
# Install or update the bug-yourself services (receiver + nightly analyzer) as
# systemd user services. Safe to re-run any time; after pulling new code, run
#
#   server/install.sh --update     (git pull, then reinstall and restart)
#
# Your settings live in ~/.config/bug-yourself/ and are only written the first
# time (or with --reconfigure). Unit files are regenerated on every run.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
CFG=$CONFIG_HOME/bug-yourself
UNIT_DIR=$CONFIG_HOME/systemd/user
RECEIVER_ENV=$CFG/receiver.env
ANALYZER_TOML=$CFG/analyzer.toml
INSTALL_CONF=$CFG/install.conf
UNITS=(bug-receiver.service bug-analyzer.service bug-analyzer.timer)
MARKER=.bug-yourself-archive

say()  { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
Usage: server/install.sh [--update | --reconfigure | --uninstall]

  (no option)    install, or reinstall after you've pulled new code
  --update       git pull, then reinstall and restart the receiver
  --reconfigure  ask the setup questions again (current values are the defaults)
  --uninstall    stop and remove the services; settings and recordings are kept
EOF
}

# --- prompts ------------------------------------------------------------------

ask() {  # ask "Question" "default" -> answer on stdout
    local answer
    if [[ -n ${2-} ]]; then
        read -r -p "$1 [$2]: " answer || true
        printf '%s' "${answer:-$2}"
    else
        while [[ -z ${answer-} ]]; do read -r -p "$1: " answer || die "no answer for: $1"; done
        printf '%s' "$answer"
    fi
}

confirm() {  # confirm "Question" -> 0 for yes
    local answer
    read -r -p "$1 [y/N]: " answer || true
    [[ $answer == [yY]* ]]
}

abspath() {  # expand ~ and make absolute, without requiring the path to exist
    local p=${1/#\~/$HOME}
    [[ $p == /* ]] || p=$PWD/$p
    printf '%s' "$p"
}

quote() {  # value safe inside double quotes, for both systemd's EnvironmentFile and the shell
    local v=$1
    v=${v//\\/\\\\}
    v=${v//\"/\\\"}
    v=${v//\$/\\\$}
    v=${v//\`/\\\`}
    printf '"%s"' "$v"
}

# --- checks -------------------------------------------------------------------

host_has_codec() {
    { command -v oggenc && command -v oggdec; } >/dev/null 2>&1 && return 0
    command -v ffmpeg >/dev/null 2>&1 && ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libvorbis
}

check_python() {
    command -v python3 >/dev/null || die "python3 not found"
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "python 3.11 or newer is needed"
}

# --- configuration ------------------------------------------------------------

load_existing() {
    # shellcheck disable=SC1090
    [[ -f $RECEIVER_ENV ]] && . "$RECEIVER_ENV"
    # shellcheck disable=SC1090
    [[ -f $INSTALL_CONF ]] && . "$INSTALL_CONF"
    if [[ -f $ANALYZER_TOML ]]; then
        HDD_ROOT=$(sed -n 's/^hdd_root *= *"\(.*\)"$/\1/p' "$ANALYZER_TOML")
        SAMPLES_NEW=$(sed -n 's/^samples_new_dir *= *"\(.*\)"$/\1/p' "$ANALYZER_TOML")
        ARCHIVE_MAX=$(sed -n 's/^archive_max *= *"\(.*\)"$/\1/p' "$ANALYZER_TOML")
        SAMPLES_MAX=$(sed -n 's/^samples_max *= *"\(.*\)"$/\1/p' "$ANALYZER_TOML")
    fi
    return 0
}

suggest_hdd() {
    local d
    for d in /run/media/system/* /var/mnt/* /mnt/*; do
        [[ -d $d ]] && say "    $d"
    done
    return 0
}

configure() {
    load_existing
    say ""
    say "== Settings (press Enter to keep the value in brackets)"

    local ts_ip
    ts_ip=$(tailscale ip -4 2>/dev/null | head -n1 || true)
    BUG_BIND=$(ask "Address to listen on (your Tailscale IP)" "${BUG_BIND:-${ts_ip:-0.0.0.0}}")
    BUG_PORT=$(ask "Port" "${BUG_PORT:-8675}")
    BUG_DIR=$(abspath "$(ask "SSD working folder for incoming recordings" "${BUG_DIR:-$HOME/bug-yourself/recordings}")")
    SAMPLES_NEW=$(abspath "$(ask "SSD folder for each night's new clips" "${SAMPLES_NEW:-$HOME/bug-yourself/samples-new}")")

    if [[ -z ${HDD_ROOT-} ]]; then
        say "HDD folder for unanalyzed/, archive/, samples/ and the database. Mounted drives:"
        suggest_hdd
    fi
    HDD_ROOT=$(abspath "$(ask "HDD folder" "${HDD_ROOT:-}")")
    ARCHIVE_MAX=$(ask "Archive size cap" "${ARCHIVE_MAX:-500GB}")
    SAMPLES_MAX=$(ask "Samples library size cap" "${SAMPLES_MAX:-100GB}")

    local token_default=${BUG_TOKEN:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(12))')}
    BUG_TOKEN=$(ask "Upload token (the phone needs the same value)" "$token_default")

    # The receiver needs a Vorbis encoder. If the host has none, run it inside a distrobox.
    if host_has_codec; then
        RECEIVER_PYTHON=$(command -v python3)
    else
        say "No Vorbis encoder (ffmpeg with libvorbis, or oggenc) on the host."
        local box previous=""
        [[ ${RECEIVER_PYTHON-} == *" enter -n "* ]] && previous=${RECEIVER_PYTHON#* enter -n } && previous=${previous%% *}
        read -r -p "Distrobox to run the receiver in (blank: host, fix later)${previous:+ [$previous]}: " box || true
        box=${box:-$previous}
        if [[ -n $box ]]; then
            RECEIVER_PYTHON="$(command -v distrobox || echo /usr/bin/distrobox) enter -n $box -- python3"
        else
            RECEIVER_PYTHON=$(command -v python3)
        fi
    fi
    ANALYZER_PYTHON=${ANALYZER_PYTHON:-$(command -v python3)}

    mkdir -p "$CFG" "$BUG_DIR" "$SAMPLES_NEW"
    umask 077  # the env file holds the token
    cat > "$RECEIVER_ENV" <<EOF
# bug-yourself receiver settings (read by bug-receiver.service). Edit, then run server/install.sh.
BUG_BIND=$(quote "$BUG_BIND")
BUG_PORT=$(quote "$BUG_PORT")
BUG_DIR=$(quote "$BUG_DIR")
BUG_UNANALYZED_DIR=$(quote "$HDD_ROOT/unanalyzed")
BUG_TOKEN=$(quote "$BUG_TOKEN")
BUG_QUALITY=$(quote "${BUG_QUALITY:-10}")
# Extra receiver options, e.g. --max-size 20GB
BUG_EXTRA_ARGS=$(quote "${BUG_EXTRA_ARGS:-}")
EOF
    umask 022
    cat > "$ANALYZER_TOML" <<EOF
# bug-yourself analyzer settings. Edit freely; server/install.sh only rewrites
# this file with --reconfigure.
working_dir = "$BUG_DIR"
hdd_root = "$HDD_ROOT"
samples_new_dir = "$SAMPLES_NEW"
archive_max = "$ARCHIVE_MAX"
samples_max = "$SAMPLES_MAX"
EOF
    cat > "$INSTALL_CONF" <<EOF
# How the services start Python. Written by server/install.sh.
RECEIVER_PYTHON=$(quote "$RECEIVER_PYTHON")
ANALYZER_PYTHON=$(quote "$ANALYZER_PYTHON")
EOF
    prepare_hdd
}

prepare_hdd() {
    if [[ ! -f $HDD_ROOT/$MARKER ]]; then
        local mount
        mount=$(findmnt -n -o TARGET,SOURCE -T "$(existing_parent "$HDD_ROOT")" 2>/dev/null || echo "unknown")
        say ""
        say "$HDD_ROOT has no $MARKER marker yet. It sits on: $mount"
        if confirm "Is that the HDD? Create the folder and marker there"; then
            mkdir -p "$HDD_ROOT"
            touch "$HDD_ROOT/$MARKER"
        else
            warn "skipped; the analyzer won't run until $HDD_ROOT/$MARKER exists"
            return 0
        fi
    fi
    mkdir -p "$HDD_ROOT/unanalyzed"  # the receiver only moves into an existing folder
}

existing_parent() {
    local p=$1
    while [[ ! -e $p && $p != / ]]; do p=$(dirname "$p"); done
    printf '%s' "$p"
}

# --- services -----------------------------------------------------------------

render() {  # render <template> <python command> <dest>
    local esc_repo esc_py
    esc_repo=$(printf '%s' "$REPO" | sed 's/[&|\\]/\\&/g')
    esc_py=$(printf '%s' "$2" | sed 's/[&|\\]/\\&/g')
    sed -e "s|@REPO@|$esc_repo|g" -e "s|@PYTHON@|$esc_py|g" "$1" > "$3.tmp"
    if cmp -s "$3.tmp" "$3"; then
        rm "$3.tmp"
    else
        mv "$3.tmp" "$3"
        say "  updated $(basename "$3")"
    fi
}

install_services() {
    # shellcheck disable=SC1090
    . "$INSTALL_CONF"
    mkdir -p "$UNIT_DIR"
    say ""
    say "== Installing services from $REPO"
    render "$REPO/server/systemd/bug-receiver.service" "$RECEIVER_PYTHON" "$UNIT_DIR/bug-receiver.service"
    render "$REPO/server/systemd/bug-analyzer.service" "$ANALYZER_PYTHON" "$UNIT_DIR/bug-analyzer.service"
    render "$REPO/server/systemd/bug-analyzer.timer" "" "$UNIT_DIR/bug-analyzer.timer"

    systemctl --user daemon-reload
    systemctl --user enable --quiet bug-receiver.service bug-analyzer.timer
    # Restart so a pulled update takes effect now. The phone retries the brief gap.
    systemctl --user restart bug-receiver.service
    systemctl --user start bug-analyzer.timer

    if [[ $(loginctl show-user "$USER" --property=Linger --value 2>/dev/null || true) != yes ]]; then
        loginctl enable-linger "$USER" \
            || warn "couldn't enable lingering; the services will only run while you're logged in"
    fi
}

report() {
    # shellcheck disable=SC1090
    . "$RECEIVER_ENV"
    say ""
    say "== Status"
    sleep 2
    if systemctl --user is-active --quiet bug-receiver.service; then
        say "receiver: running on $BUG_BIND:$BUG_PORT"
    else
        warn "receiver isn't running yet (it retries every 15 s). Logs: journalctl --user -u bug-receiver -n 30"
    fi
    say "analyzer: next run $(systemctl --user show bug-analyzer.timer --property=NextElapseUSecRealtime --value 2>/dev/null || echo 'at midnight')"
    $ANALYZER_PYTHON "$REPO/server/analyzer.py" --config "$ANALYZER_TOML" status 2>&1 | sed 's/^/  /' || true
    say ""
    say "Phone settings:"
    say "  Server URL: http://$(uname -n | cut -d. -f1):$BUG_PORT   (or http://$BUG_BIND:$BUG_PORT)"
    say "  Token:      $BUG_TOKEN"
    say ""
    say "After pulling new code, run: $REPO/server/install.sh --update"
}

uninstall() {
    systemctl --user disable --now bug-receiver.service bug-analyzer.timer 2>/dev/null || true
    for u in "${UNITS[@]}"; do rm -f "$UNIT_DIR/$u"; done
    systemctl --user daemon-reload
    say "Services removed. Settings ($CFG) and recordings were left in place."
}

# --- main ---------------------------------------------------------------------

mode=install
case ${1-} in
    "") ;;
    --update) mode=update ;;
    --reconfigure) mode=reconfigure ;;
    --uninstall) mode=uninstall ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac

command -v systemctl >/dev/null || die "systemctl not found"

if [[ $mode == uninstall ]]; then
    uninstall
    exit 0
fi

if [[ $mode == update ]]; then
    say "== Pulling the latest code"
    git -C "$REPO" pull --ff-only
    # The pull may have changed this very script; continue in the new version.
    exec "$REPO/server/install.sh"
fi

check_python
if [[ $mode == reconfigure || ! -f $RECEIVER_ENV || ! -f $ANALYZER_TOML || ! -f $INSTALL_CONF ]]; then
    configure
else
    load_existing
    prepare_hdd
fi
host_has_codec || [[ ${RECEIVER_PYTHON-} == *distrobox* ]] \
    || warn "no Vorbis encoder on the host; the receiver will exit until ffmpeg/oggenc is available (or rerun with --reconfigure to use a distrobox)"
install_services
report
