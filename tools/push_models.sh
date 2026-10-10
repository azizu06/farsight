#!/usr/bin/env bash
# Copy models/ (not in git) from a laptop to the Jetson.
# Usage: tools/push_models.sh [user@host]   default: $USER@farsight.local
set -euo pipefail
cd "$(dirname "$0")/.."
TARGET="${1:-$USER@farsight.local}"
rsync -avh --progress models/ "$TARGET:~/farsight/models/"
