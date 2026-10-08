#!/usr/bin/env bash
# Install unii-chat-router command symlinks into ~/.local/bin.
set -euo pipefail
cd "$(dirname "$0")"

command -v python3 >/dev/null || { echo "install: python3 is required" >&2; exit 1; }
python3 -c 'import cryptography' 2>/dev/null || {
  echo "install: python3-cryptography is required (pip install cryptography)" >&2
  exit 1
}
command -v unii >/dev/null || {
  echo "install: warning: unii is not in PATH yet (https://uniichat.com/install.sh)" >&2
}

mkdir -p "$HOME/.local/bin"
for name in unii-chat-router unii-chat-router-client unii-deepseek unii-deepseek-client; do
  ln -sfn "$PWD/unii-chat-router" "$HOME/.local/bin/$name"
done
echo "installed: $(ls -d "$HOME"/.local/bin/unii-chat-router*)"
