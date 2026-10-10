#!/usr/bin/env bash
# nvblox go/no-go on the Jetson: launch nvblox on synthetic, recorded or live depth, sample for a few minutes, write a
# report with a PASS/FAIL verdict. Budget about an hour including the install (jetson/nvblox-install.sh); the run is
# --duration seconds (default 600). Exit 0 = PASS, 1 = FAIL, 2 = could not run. Docs: docs/nvblox.md.
# Usage: bash jetson/nvblox-smoke.sh [--source synthetic|bag|live] [--bag DIR] [--duration SEC] [--warmup SEC]
#          [--mesh-hz HZ] [--nav2] [--report FILE] [--check] [--dry-run] [-- threshold flags for nvblox_smoke report]
#   synthetic  built-in room with a box (farsight_nvblox synthetic_depth); needs no phone. Do not run it next to the
#              real bridge: both would publish the same TF.
#   bag        ros2 bag play DIR --loop --clock; the bag needs depth/image, depth/camera_info, /tf and /tf_static.
#   live       attach to an nvblox_node already running (core launch with costmap_source:=nvblox) and the real phone.
#   --nav2     also gate the Nav2 local costmap update rate (Nav2 must be running with the nvblox layer)
#   --check    preflight only (packages, tegrastats, ROS), nothing is launched
# Thresholds default to the go/no-go gates in docs/nvblox.md; override with e.g. -- --min-available-mb 800.
# Measures only: it never changes power mode, services or packages. Run it with YOLO and the server up to see real load.
set -euo pipefail
export PYTHONUNBUFFERED=1

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
ROS2="${NVBLOX_ROS2:-ros2}"
TEGRASTATS="${NVBLOX_TEGRASTATS:-tegrastats}"
ROS_SETUP="${NVBLOX_ROS_SETUP:-/opt/ros/jazzy/setup.bash}"
WS_SETUP="${NVBLOX_WS_SETUP:-$REPO/ros2/install/setup.bash}"
MEMINFO="${NVBLOX_MEMINFO:-/proc/meminfo}"
SLICE_TOPIC="${NVBLOX_SLICE_TOPIC:-/nvblox_node/static_map_slice}"
MESH_TOPIC="${NVBLOX_MESH_TOPIC:-/nvblox_node/mesh}"
COSTMAP_TOPIC="${NVBLOX_COSTMAP_TOPIC:-/local_costmap/costmap}"
PARAMS="$REPO/ros2/farsight_nvblox/config/nvblox.yaml"
SOURCE=synthetic BAG="" DURATION=600 WARMUP=30 MESH_HZ=0 NAV2=0 CHECK=0 DRY=0 REPORT=""
THRESHOLDS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --source) SOURCE="${2:?--source needs synthetic, bag or live}"; shift ;;
    --bag) BAG="${2:?--bag needs a directory}"; shift ;;
    --duration) DURATION="${2:?--duration needs seconds}"; shift ;;
    --warmup) WARMUP="${2:?--warmup needs seconds}"; shift ;;
    --mesh-hz) MESH_HZ="${2:?--mesh-hz needs a rate}"; shift ;;
    --nav2) NAV2=1 ;;
    --report) REPORT="${2:?--report needs a file}"; shift ;;
    --check) CHECK=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    --) shift; THRESHOLDS=("$@"); break ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done
case "$SOURCE" in synthetic|live) ;; bag) [ -n "$BAG" ] || { echo "--source bag needs --bag DIR" >&2; exit 2; } ;;
  *) echo "--source must be synthetic, bag or live" >&2; exit 2 ;; esac
for number in "$DURATION" "$WARMUP"; do
  case "$number" in ''|*[!0-9]*) echo "--duration and --warmup are whole seconds" >&2; exit 2 ;; esac
done
[ "$DURATION" -ge 5 ] || { echo "--duration must be at least 5 s" >&2; exit 2; }
REPORT="${REPORT:-$HOME/nvblox-smoke-$(date +%Y%m%d-%H%M%S).md}"

step() { printf '\n== %s\n' "$*"; }
die() { echo "nvblox-smoke: $*" >&2; exit 2; }

step "Plan"
LAUNCH_ARGS=(farsight_nvblox nvblox.launch.py "mesh_rate_hz:=$MESH_HZ")
case "$SOURCE" in
  synthetic) LAUNCH_ARGS+=("synthetic:=true") ;;
  bag) LAUNCH_ARGS+=("use_sim_time:=true") ;;
esac
echo "source=$SOURCE duration=${DURATION}s warmup=${WARMUP}s mesh_hz=$MESH_HZ nav2=$NAV2"
echo "report=$REPORT"
if [ "$SOURCE" = live ]; then echo "launch: none (attach to the running /nvblox_node)"; else echo "launch: $ROS2 launch ${LAUNCH_ARGS[*]}"; fi
if [ "$DRY" = 1 ]; then
  echo "[dry-run] would sample $SLICE_TOPIC (hz, delay), $MESH_TOPIC if mesh_hz > 0, tegrastats, MemAvailable and nvblox RSS every 1-2 s, then run: $PYTHON -m jetson.nvblox_smoke report"
  exit 0
fi

step "Preflight"
set +u
# shellcheck disable=SC1090
[ -f "$ROS_SETUP" ] && . "$ROS_SETUP"
# shellcheck disable=SC1090
[ -f "$WS_SETUP" ] && . "$WS_SETUP"
set -u
command -v "$ROS2" >/dev/null || die "ros2 not found; source ROS 2 Jazzy (jetson/ros2-install.sh)"
command -v "$TEGRASTATS" >/dev/null || die "tegrastats not found; run this on the Jetson"
for package in nvblox_ros nvblox_nav2 nvblox_msgs farsight_nvblox; do
  "$ROS2" pkg prefix "$package" >/dev/null 2>&1 || die "ROS package $package missing (bash jetson/nvblox-install.sh, then colcon build ros2/farsight_nvblox)"
done
echo "ROS packages present: nvblox_ros nvblox_nav2 nvblox_msgs farsight_nvblox"
[ "$CHECK" = 1 ] && { echo "preflight OK"; exit 0; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/nvblox-smoke.XXXXXX")"
PIDS=() LAUNCH_PID="" BAG_PID=""
GROUP=""  # "-" when the launch runs in its own process group (setsid), so the stop signal reaches its children
stop_pid() {  # TERM (ros2 launch shuts its children down; background jobs ignore SIGINT), then KILL after 10 s
  local pid="$1" i
  kill -0 "$pid" 2>/dev/null || return 0
  kill -TERM -- "$GROUP$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  for i in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$pid" 2>/dev/null || return 0; sleep 1; done
  kill -KILL -- "$GROUP$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
}
cleanup() {
  local pid
  for pid in ${PIDS[@]+"${PIDS[@]}"} "$BAG_PID"; do [ -n "$pid" ] && stop_pid "$pid"; done
  [ -n "$LAUNCH_PID" ] && stop_pid "$LAUNCH_PID"
  return 0
}
trap cleanup EXIT

run_for() {  # run_for SECONDS OUTFILE command...: capture output, stop the command after SECONDS
  local secs="$1" out="$2" pid killer
  shift 2
  "$@" >"$out" 2>&1 &
  pid=$!
  ( sleep "$secs"; kill -TERM "$pid" 2>/dev/null ) &
  killer=$!
  wait "$pid" 2>/dev/null || true
  kill "$killer" 2>/dev/null || true
}

meta() { printf '%s=%s\n' "$1" "$2" >>"$WORK/meta.env"; }
meta date "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
meta host "$(hostname)"
meta tegra_release "$(head -1 /etc/nv_tegra_release 2>/dev/null | tr -d '\n' || true)"
meta cuda "$( (nvcc --version 2>/dev/null || /usr/local/cuda/bin/nvcc --version 2>/dev/null) | sed -n 's/.*release \([0-9.]*\).*/\1/p' | head -1)"
meta power_mode "$( (nvpmodel -q 2>/dev/null | head -1) || echo unknown)"
meta source "$SOURCE"
meta duration_s "$DURATION"
meta mesh_hz "$MESH_HZ"
meta nav2 "$NAV2"
meta voxel_size "$(awk '$1 == "voxel_size:" {print $2; exit}' "$PARAMS")"
meta slice_heights "$(awk '/esdf_slice_(min|max)_height:/ {sub(":", "", $1); printf "%s=%s ", $1, $2}' "$PARAMS")"
meta nvblox_packages "$(dpkg-query -W -f='${Package}=${Version} ' ros-jazzy-nvblox-ros ros-jazzy-nvblox-nav2 ros-jazzy-nvblox-msgs 2>/dev/null || echo unknown)"
meta server_running "$(pgrep -f 'jetson.server' >/dev/null 2>&1 && echo yes || echo no)"
meta load_notes "$(free -m 2>/dev/null | awk '/Mem:/ {print "idle-before: " $7 " MB available of " $2 " MB"}')"

step "Launch ($SOURCE)"
if [ "$SOURCE" = live ]; then
  echo "attaching to the running nvblox_node"
else
  setsid_cmd=(); command -v setsid >/dev/null && setsid_cmd=(setsid)
  ${setsid_cmd[@]+"${setsid_cmd[@]}"} "$ROS2" launch "${LAUNCH_ARGS[@]}" >"$WORK/launch.log" 2>&1 &
  LAUNCH_PID=$!
  [ ${#setsid_cmd[@]} -gt 0 ] && GROUP="-"
  if [ "$SOURCE" = bag ]; then
    "$ROS2" bag play "$BAG" --loop --clock >"$WORK/bag.log" 2>&1 &
    BAG_PID=$!
  fi
fi

step "Wait for /nvblox_node"
started=$SECONDS
up=0
while [ $((SECONDS - started)) -lt 90 ]; do
  if "$ROS2" node list 2>/dev/null | grep -qx '/nvblox_node'; then up=1; break; fi
  [ -n "$LAUNCH_PID" ] && ! kill -0 "$LAUNCH_PID" 2>/dev/null && break
  sleep 1
done
if [ "$up" = 1 ]; then meta startup_s "$((SECONDS - started))"; else meta startup_s timeout; echo "nvblox_node never appeared"; fi

if [ "$up" = 1 ]; then
  step "Parameters declared by the node"
  "$PYTHON" -m jetson.nvblox_smoke param-keys "$PARAMS" >"$WORK/params_expected.txt"
  "$ROS2" param list /nvblox_node 2>/dev/null | sed 's/^ *//' >"$WORK/params_declared.txt" || true
  grep -vxFf "$WORK/params_declared.txt" "$WORK/params_expected.txt" >"$WORK/params_missing.txt" || true
  meta params_checked "$(wc -l <"$WORK/params_expected.txt" | tr -d ' ')"
  echo "missing: $(wc -l <"$WORK/params_missing.txt" | tr -d ' ')"

  step "Warm up ${WARMUP}s, then sample ${DURATION}s"
  sleep "$WARMUP"
  run_for "$DURATION" "$WORK/tegrastats.txt" "$TEGRASTATS" --interval 1000 & PIDS+=($!)
  run_for "$DURATION" "$WORK/slice_hz.txt" "$ROS2" topic hz --window 20 "$SLICE_TOPIC" & PIDS+=($!)
  run_for "$DURATION" "$WORK/slice_delay.txt" "$ROS2" topic delay --window 20 "$SLICE_TOPIC" & PIDS+=($!)
  case "$MESH_HZ" in 0|0.0) ;; *) run_for "$DURATION" "$WORK/mesh_hz.txt" "$ROS2" topic hz --window 5 "$MESH_TOPIC" & PIDS+=($!) ;; esac
  [ "$NAV2" = 1 ] && { run_for "$DURATION" "$WORK/costmap_hz.txt" "$ROS2" topic hz --window 10 "$COSTMAP_TOPIC" & PIDS+=($!); }
  sample_end=$((SECONDS + DURATION))
  while [ "$SECONDS" -lt "$sample_end" ]; do
    now="$(date +%s)"
    awk -v t="$now" '/^MemAvailable:/ {print t "," $2}' "$MEMINFO" >>"$WORK/meminfo.csv" || true
    pids="$(pgrep -f 'nvblox_container|component_container.*nvblox' 2>/dev/null | tr '\n' ',' | sed 's/,$//' || true)"
    if [ -n "$pids" ]; then
      rss="$(ps -o rss= -p "$pids" 2>/dev/null | awk '{s += $1} END {print s + 0}')"
      echo "$now,$rss" >>"$WORK/rss.csv"
    fi
    sleep 2
  done
  wait "${PIDS[@]}" 2>/dev/null || true
  PIDS=()
fi

step "Report"
cleanup
LAUNCH_PID="" BAG_PID=""
status=0
(cd "$REPO" && "$PYTHON" -m jetson.nvblox_smoke report --samples "$WORK" --out "$REPORT" ${THRESHOLDS[@]+"${THRESHOLDS[@]}"}) || status=$?
echo "samples kept in $WORK; report at $REPORT"
exit "$status"
