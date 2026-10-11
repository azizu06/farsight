#!/usr/bin/env bash
# Run as root by farsight-warmup.timer 90 s after boot. jetson-clocks.service runs at ~15 s, and later in
# boot the CPU minimum dropped back to 729.6 MHz (2026-10-11), so pin the clocks again. Then ask once by
# text and once by voice, replaying the text reply's own speech, so the first operator question skips the
# cold VLM, Piper and whisper loads (text 9.3 s cold versus 2.4 s warm; stt 2148 ms cold versus 593 ms).
set -euo pipefail
/usr/bin/jetson_clocks
scheme=http
[ -z "${FARSIGHT_SSL_CERTFILE:-}" ] || scheme=https
H="$scheme://127.0.0.1:${FARSIGHT_PORT:-8443}"
# The vision model only runs on a camera frame; give the phone up to 10 min to reconnect after boot.
for _ in $(seq 120); do curl -sfk -m 3 "$H/health" | grep -q '"camera": *true' && break; sleep 5; done
ask() { curl -sSfk --retry 30 --retry-delay 5 --retry-all-errors -m 90 "$H/ask" "$@"; }
reply="$(ask -H 'content-type: application/json' -d '{"text":"What do you see in front of you?"}')"
printf 'text ask: %.600s\n' "$reply"
speech="$(mktemp --suffix .wav)"
trap 'rm -f "$speech"' EXIT
curl -sSfk -m 30 -o "$speech" "$H$(printf '%s' "$reply" | python3 -c 'import json,sys; print(json.load(sys.stdin)["audio_url"])')"
printf 'voice ask: %.600s\n' "$(ask -F "audio=@$speech;type=audio/wav")"
