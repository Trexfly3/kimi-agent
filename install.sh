#!/bin/sh
# Kimi Agent installer for macOS and Linux.
#
#   curl -LsSf https://raw.githubusercontent.com/Trexfly3/kimi-agent/main/install.sh | sh
#
# It installs uv (a small tool that downloads Python and Python apps) if you don't have it, then Kimi Agent with
# its own copy of Python, adds "Kimi Agent" to your applications and opens it. Run it again to update.
#
# Uninstall:
#   curl -LsSf https://raw.githubusercontent.com/Trexfly3/kimi-agent/main/install.sh | sh -s -- --uninstall
#
# Options: --no-launch (don't open the app at the end), --uninstall.
# Environment: KIMI_AGENT_REPO (owner/name), KIMI_AGENT_REF (branch or tag), KIMI_AGENT_SOURCE (a URL to install
# from instead, such as https://... or file:///path/to/kimi-agent.zip), KIMI_AGENT_NO_LAUNCH=1.

set -eu

say() {
    printf '\033[1;38;5;173m==>\033[0m %s\n' "$*"
}

fail() {
    printf '\033[1;31merror:\033[0m %s\n' "$*" >&2
    exit 1
}

download() {
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf "$1"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$1"
    else
        fail "curl or wget is needed to download uv."
    fi
}

install_uv() {
    script=$(mktemp)
    # The official installer, from astral.sh or (where that is blocked) from uv's GitHub releases.
    for url in https://astral.sh/uv/install.sh https://github.com/astral-sh/uv/releases/latest/download/uv-installer.sh; do
        if download "$url" >"$script" 2>/dev/null && [ -s "$script" ]; then
            sh "$script" </dev/null
            rm -f "$script"
            return 0
        fi
    done
    rm -f "$script"
    fail "could not download uv. Check your internet connection and try again."
}

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    for candidate in "${XDG_BIN_HOME:-}/uv" "$HOME/.local/bin/uv" "${CARGO_HOME:-$HOME/.cargo}/bin/uv"; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

usage() {
    cat <<'EOF'
Install or update Kimi Agent:  curl -LsSf https://raw.githubusercontent.com/Trexfly3/kimi-agent/main/install.sh | sh
Options (after "sh -s --"):
  --no-launch   don't open Kimi Agent when the installation is done
  --uninstall   remove Kimi Agent (your settings and conversations are kept)
EOF
}

has_display() {
    [ "$(uname -s)" = Darwin ] || [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]
}

uninstall() {
    uv=$(find_uv) || fail "uv is not installed, so neither is Kimi Agent."
    bin=$("$uv" tool dir --bin)
    if [ -x "$bin/kimi-agent" ]; then
        "$bin/kimi-agent" --quit-gui </dev/null >/dev/null 2>&1 || true
        "$bin/kimi-agent" --remove-shortcuts </dev/null || true
    fi
    "$uv" tool uninstall kimi-agent </dev/null || true
    say "Kimi Agent was removed. Your settings and conversations are still in ${KIMI_AGENT_HOME:-$HOME/.kimi-agent}"
    say "(delete that folder to remove them too)."
}

main() {
    action=install
    no_launch="${KIMI_AGENT_NO_LAUNCH:-0}"
    for arg in "$@"; do
        case "$arg" in
            --uninstall) action=uninstall ;;
            --no-launch) no_launch=1 ;;
            -h | --help)
                usage
                return 0
                ;;
            *) fail "unknown option: $arg" ;;
        esac
    done
    if [ "$action" = uninstall ]; then
        uninstall
        return 0
    fi

    # Trust the same certificates as curl did, so networks with an HTTPS-inspecting proxy work too.
    # (UV_NATIVE_TLS is the older name of UV_SYSTEM_CERTS.)
    UV_SYSTEM_CERTS="${UV_SYSTEM_CERTS:-1}"
    UV_NATIVE_TLS="${UV_NATIVE_TLS:-1}"
    export UV_SYSTEM_CERTS UV_NATIVE_TLS

    repo="${KIMI_AGENT_REPO:-Trexfly3/kimi-agent}"
    ref="${KIMI_AGENT_REF:-main}"
    source="${KIMI_AGENT_SOURCE:-https://github.com/$repo/archive/$ref.zip}"

    if ! uv=$(find_uv); then
        say "Installing uv (it downloads Python and Kimi Agent for you)..."
        install_uv
        uv=$(find_uv) || fail "uv was installed but cannot be found. Open a new terminal and run this again."
    fi
    bin=$("$uv" tool dir --bin)

    if [ -x "$bin/kimi-agent" ]; then  # updating: close the running app first
        "$bin/kimi-agent" --quit-gui </dev/null >/dev/null 2>&1 || true
    fi
    say "Installing Kimi Agent (this can take a minute)..."
    if ! "$uv" tool install --force --python 3.12 --reinstall-package kimi-agent "kimi-agent[computer] @ $source" </dev/null; then
        say "Trying again without the screen-control add-ons..."
        "$uv" tool install --force --python 3.12 --reinstall-package kimi-agent "kimi-agent @ $source" </dev/null ||
            fail "installing Kimi Agent failed (see the messages above)."
    fi
    "$uv" tool update-shell </dev/null >/dev/null 2>&1 || true  # so "kimi-agent" works in new terminals

    if ! "$bin/kimi-agent" --install-shortcuts </dev/null; then
        say "Could not add Kimi Agent to your applications; start it with: $bin/kimi-agent-gui"
    fi

    say "Kimi Agent $("$bin/kimi-agent" --version </dev/null | sed 's/^kimi-agent //') is installed."
    if [ "$(uname -s)" = Darwin ]; then
        say "Open it any time from Launchpad, Spotlight or your Applications folder (\"Kimi Agent\")."
    else
        say "Open it any time from your applications menu (\"Kimi Agent\")."
    fi
    say "To update, run this installer again. In a terminal, use: kimi-agent"
    if [ "$no_launch" != 1 ] && has_display; then
        say "Opening Kimi Agent..."
        nohup "$bin/kimi-agent-gui" </dev/null >/dev/null 2>&1 &
    fi
}

# Everything runs from main, so a partially downloaded script never runs halfway.
main "$@"
