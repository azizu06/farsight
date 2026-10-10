#!/usr/bin/env bash
# Laptop side, once the Jetson has booted Ubuntu and accepts your SSH key (ssh-copy-id user@192.168.55.1).
# Gets the repo and models onto the Jetson, then opens jetson/setup.sh in tmux so YOU type the sudo password.
# Safe to re-run: it updates the checkout, rsyncs only changed models, and re-attaches the running tmux session.
#
# Usage: tools/bootstrap_jetson.sh [--dry-run] [--no-models] [--no-attach] [--branch NAME] [--setup-args "..."] user@host
#   host: 192.168.55.1 over the USB-C cable, or farsight.local once the hostname is set.
#   --branch      branch to check out on the Jetson (default main; it must be pushed)
#   --setup-args  extra arguments for jetson/setup.sh, e.g. "--with-vlm" or "--skip-builds"
#   --no-attach   start setup in a detached tmux session instead of attaching (a sudo prompt then waits there:
#                 attach later with: ssh -t user@host tmux attach -t setup)
set -euo pipefail
cd "$(dirname "$0")/.."

DRY=0 MODELS=1 ATTACH=1 BRANCH=main SETUP_ARGS="" TARGET=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --no-models) MODELS=0 ;;
    --no-attach) ATTACH=0 ;;
    --branch) BRANCH="${2:?--branch needs a name}"; shift ;;
    --setup-args) SETUP_ARGS="${2?--setup-args needs a value}"; shift ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    -*) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
    *) TARGET="$1" ;;
  esac
  shift
done
[ -n "$TARGET" ] || { echo "usage: tools/bootstrap_jetson.sh [options] user@host   (see --help)" >&2; exit 2; }
REPO_URL="${FARSIGHT_REPO_URL:-https://github.com/azizu06/farsight}"
REMOTE_DIR="${FARSIGHT_REMOTE_DIR:-farsight}"
SSH_OPTS=(-o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new)

step() { printf '\n== %s\n' "$*"; }
# Run a command locally, or print it under --dry-run.
run() { if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }
# Run a shell snippet on the Jetson over stdin; values arrive as positional args, never spliced into the snippet.
remote() {
  local script="$1"; shift
  if [ "$DRY" = 1 ]; then printf '  [dry-run] ssh %s bash -s -- %s <<script\n%s\n  script\n' "$TARGET" "$*" "$script"
  else ssh "${SSH_OPTS[@]}" "$TARGET" bash -s -- "$@" <<<"$script"; fi
}

step "SSH key login to $TARGET"
if [ "$DRY" = 1 ]; then
  echo "  [dry-run] ssh -o BatchMode=yes $TARGET true"
else
  ssh "${SSH_OPTS[@]}" -o BatchMode=yes "$TARGET" true \
    || { echo "SSH key login to $TARGET failed. Once: ssh-copy-id $TARGET   (the Jetson password is typed by you)"; exit 1; }
fi

step "Branch $BRANCH is on GitHub"
if [ "$DRY" = 0 ] && [ -z "$(git ls-remote --heads origin "$BRANCH" 2>/dev/null)" ]; then
  echo "origin has no branch '$BRANCH': push it first (the Jetson clones from $REPO_URL)"; exit 1
fi

step "git, tmux, rsync on the Jetson (sudo asks for the password here, typed by you)"
run ssh -t "${SSH_OPTS[@]}" "$TARGET" 'for c in git tmux rsync; do command -v $c >/dev/null || missing="$missing $c"; done; [ -z "$missing" ] || { sudo apt-get update && sudo apt-get install -y $missing; }'

step "Repo on the Jetson (~/$REMOTE_DIR)"
# shellcheck disable=SC2016  # the snippet is expanded on the Jetson, not here
remote '
  set -e
  dir=$HOME/$1; url=$2; branch=$3
  if [ ! -d "$dir/.git" ]; then git clone -q "$url" "$dir"; fi
  cd "$dir"
  if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "Uncommitted edits in $dir: stopping bootstrap (commit or stash them on the Jetson)"; exit 1
  fi
  git fetch -q origin "$branch"
  git checkout -q "$branch" 2>/dev/null || git checkout -q -b "$branch" FETCH_HEAD
  git merge -q --ff-only FETCH_HEAD
  git log --oneline -1
' "$REMOTE_DIR" "$REPO_URL" "$BRANCH"

if [ "$MODELS" = 1 ]; then
  step "Models"
  push_flags=(); [ "$DRY" = 0 ] || push_flags=(--dry-run)
  FARSIGHT_REMOTE_DIR="$REMOTE_DIR" bash tools/push_models.sh ${push_flags[@]+"${push_flags[@]}"} "$TARGET"
fi

step "Setup in tmux session 'setup' (log: ~/setup.log on the Jetson)"
setup_cmd="bash ~/$REMOTE_DIR/jetson/setup.sh $SETUP_ARGS 2>&1 | tee -a ~/setup.log; echo; echo 'setup finished: press Enter to close'; read -r _"
if [ "$ATTACH" = 1 ]; then
  echo "Attaching: type the sudo password when asked. Detach with Ctrl-b d; re-run this script to re-attach."
  run ssh -t "${SSH_OPTS[@]}" "$TARGET" tmux new-session -A -s setup "$(printf '%q' "$setup_cmd")"
else
  run ssh "${SSH_OPTS[@]}" "$TARGET" "tmux has-session -t setup 2>/dev/null || tmux new-session -d -s setup $(printf '%q' "$setup_cmd")"
  echo "Started detached. Attach to answer the sudo prompt: ssh -t $TARGET tmux attach -t setup"
fi
