#!/bin/sh
# Install unii-chat-router.
#   ./install.sh              from a checkout
#   curl -fsSL <raw url>/install.sh | sh    anywhere: clones into
#                                           ~/.local/share/unii-chat-router
set -eu

REPO_URL="https://github.com/hammerhoundai/unii-router.git"
REPO_DIR="${UNII_CHAT_ROUTER_INSTALL_DIR:-$HOME/.local/share/unii-chat-router}"

# Prefer a checkout when the script runs next to the tool.
script_dir=$(cd "$(dirname "$0")" 2>/dev/null && pwd) || script_dir=""
if [ -n "$script_dir" ] && [ -f "$script_dir/unii-router" ] && [ -f "$script_dir/router-proxy.py" ]; then
    REPO_DIR="$script_dir"
elif [ ! -f "$REPO_DIR/unii-router" ]; then
    command -v git >/dev/null || {
        echo "install: git is required for the one-line install" >&2
        echo "        (or clone the repo and run ./install.sh from the checkout)" >&2
        exit 1
    }
    if [ -d "$REPO_DIR/.git" ]; then
        git -C "$REPO_DIR" pull --ff-only -q || {
            echo "install: warning: could not update $REPO_DIR; using existing files" >&2;
        }
    else
        mkdir -p "$(dirname "$REPO_DIR")"
        git clone -q "$REPO_URL" "$REPO_DIR"
    fi
fi
cd "$REPO_DIR"

command -v python3 >/dev/null || { echo "install: python3 is required" >&2; exit 1; }
python3 -c 'import cryptography' 2>/dev/null || {
    echo "install: python3-cryptography is required (pip install cryptography)" >&2
    exit 1
}
command -v unii >/dev/null || {
    echo "install: warning: unii is not in PATH yet (https://uniichat.com/install.sh)" >&2
}

mkdir -p "$HOME/.local/bin"
for name in unii-router unii-router-client; do
    ln -sfn "$REPO_DIR/unii-router" "$HOME/.local/bin/$name"
done
echo "installed into $HOME/.local/bin: unii-router unii-router-client (repo: $REPO_DIR)"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) echo "note: $HOME/.local/bin is not in PATH - add it to your shell config" >&2 ;;
esac
