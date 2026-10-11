#!/usr/bin/env bash
# Fetch ONNX models for farsight-compiler's learned stage into models/compiler/.
# Depth Anything V2 Small (Apache-2.0) via onnx-community on Hugging Face.
# Usage: tools/compiler/fetch_models.sh
#   then: export FARSIGHT_DEPTH_MODEL=models/compiler/depth_anything_v2_small.onnx
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="$ROOT/models/compiler"
mkdir -p "$OUT_DIR"

DAV2_URL="https://huggingface.co/onnx-community/depth-anything-v2-small/resolve/main/onnx/model.onnx"
DAV2_DST="$OUT_DIR/depth_anything_v2_small.onnx"
DAV2_SHA256="afb6a5c28f3b6bf1618c6e43f02073ef9dfdc70e937502d51603e57b0a1df10c"

sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}';
    else shasum -a 256 "$1" | awk '{print $1}'; fi
}

fetch() { # url dst sha
    local url="$1" dst="$2" want="$3"
    if [ -f "$dst" ] && [ "$(sha256 "$dst")" = "$want" ]; then
        echo "[models] up to date: $dst"
    else
        echo "[models] downloading $url -> $dst"
        curl -fL --retry 3 -o "$dst.tmp" "$url"
        mv "$dst.tmp" "$dst"
    fi
    local got
    got="$(sha256 "$dst")"
    echo "  sha256: $got"
    if [ "$got" != "$want" ]; then
        echo "  ERROR: sha256 mismatch (expected $want)" >&2
        exit 1
    fi
}

fetch "$DAV2_URL" "$DAV2_DST" "$DAV2_SHA256"

echo "ready. export FARSIGHT_DEPTH_MODEL=\"$DAV2_DST\""
