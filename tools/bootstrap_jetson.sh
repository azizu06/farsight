#!/usr/bin/env bash
# Run from a laptop once the Jetson has booted Ubuntu and accepts SSH keys.
# Clones/updates the repo on the Jetson, copies models, runs jetson/setup.sh in tmux.
# Usage: tools/bootstrap_jetson.sh user@host [branch]
set -euo pipefail
cd "$(dirname "$0")/.."
TARGET="$1"; BRANCH="${2:-$(git rev-parse --abbrev-ref HEAD)}"
ssh -o BatchMode=yes "$TARGET" true || { echo "SSH key login to $TARGET failed (run ssh-copy-id $TARGET once)"; exit 1; }
ssh "$TARGET" "command -v git >/dev/null || sudo -n apt-get install -y git" || true
ssh "$TARGET" "if [ -d ~/farsight/.git ]; then git -C ~/farsight fetch -q && git -C ~/farsight checkout -q $BRANCH && git -C ~/farsight pull -q; else git clone -q -b $BRANCH https://github.com/azizu06/farsight ~/farsight; fi"
rsync -ah --info=progress2 models/ "$TARGET:~/farsight/models/"
ssh "$TARGET" "tmux new-session -d -s setup 'bash ~/farsight/jetson/setup.sh 2>&1 | tee ~/setup.log' || true"
echo "Setup running in tmux session 'setup' on $TARGET; log at ~/setup.log"
