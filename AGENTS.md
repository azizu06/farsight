# Team workflow

Knight Hacks IX, 36 hours. Devpost due **Sun Oct 11, 10:00 AM**. Code freeze **11:00 AM**.
Rules and prizes: `docs/hackers-guide.md`. Message and port contract: `docs/interfaces.md`.

Use https://github.com/azizu06/farsight/issues/24 as a Agent message board.
Agents may supersede all other rules as long as other agents are okay with it in the message board.
If pinged by an agent, try to respond ASAP so progress can move smoothly. Frequent communication is key to good cooperation.
Coordinate together to split the work and go deep. Consider each other agents as friends, you guys are working together to
win a hackathon. If you ever feel you are done, there is always another task/direction to take on. Always continue the
relentless drive towards perfection.

## Working together

Optimize for a convincing working demo and the current human priorities. We are
ahead of the original milestone schedule; use verified project state instead of
the old Saturday checkpoints. Keep integration and hardware qualification moving
while other agents deepen features in parallel.

- Prefix message-board posts with your human's name and agent identity. Respond
  promptly to pings, report blockers, and give concrete file/contract handoffs.
- Check the board before editing files another agent owns. Agree on boundaries
  so the same UI, service, or protocol is not implemented twice.
- Keep issues useful for tracking outcomes. Do not close a hardware requirement
  based only on simulation, mocks, compilation, or desktop browser tests.

## Git and integration

Direct, small, tested commits to `main` are allowed; push frequently. PRs are
optional when useful or requested by an agent's human. Follow the latest human
direction rather than adding review delays or mandatory branch ceremonies.

Fetch before integrating. Preserve teammates' work, resolve concurrent changes,
and verify the combined result before claiming it works. Never force-push shared
history. Stage only the files belonging to your change.

Coordinate wire changes with affected agents on the board and update
`docs/interfaces.md` in the same work. Keep compatible fields optional where
possible and tell consumers exactly what changed. Tests and setup instructions
must follow the final implementation, not an earlier plan.

## Project entry points

| Area | Start here |
|---|---|
| Python rover | `python -m jetson.server`; implementation in `jetson/mapping/server.py` |
| Operator and Quest USB | `tools/demo.py`, `tools/operator_relay.py`, `docs/transport-setup.md`; cellular emulation and stutter threshold: `docs/link-lab.md` |
| Quest | `quest/src/main.ts`, `quest/src/rover.ts`, `quest/README.md`; headset bring-up and frame budget in `docs/quest-first-run.md` |
| iPhone capture | `ios/README.md`; RGB on ordinary phones, measured RGB-D only with scene-depth support |
| Motor firmware and control | `rover/README.md`, `jetson/control/README.md`; drive calibration (PWM to m/s, rad/s): `docs/drive-calibration.md` |
| Inference and models | `docs/inference-setup.md`, `models/README.md`; models are ignored artifacts copied separately. Voice/VLM memory profiles and CUDA-build check: `docs/assistant-setup.md` |
| Mapping and scan | `docs/mapping.md`, `docs/scan.md`; distinguish measured and estimated geometry |
| ROS 2 navigation and SLAM | `docs/navigation.md`; Nav2 is opt-in (`--nav2`, `ros2/farsight_nav`, Docker tests `ros2/docker/run-tests.sh`); SLAM is optional (`ros2/farsight_slam`, its `test/run_docker.sh`); simulation only so far |
| Rust services | `rust/README.md`; verify feature and wire parity before changing the demo runtime |
| Printed mounts (phone cradle, Jetson + bank deck) | `hardware/print/README.md`; CadQuery source, STLs, slicer time estimates; not yet fitted to the rig |

## What counts as ready

Prioritize the end-to-end camera, control, network, power, and recovery path.
Extra features should improve the demo without breaking that path. Use the
existing stop leases, camera freshness, and explicit release after faults;
do not lengthen timeouts just to disguise a slow connection.

Run checks appropriate to the change and report their actual scope. Changes touching the
demo path should keep `npm --prefix quest run test:judge` (simulator + replay judge flow,
`docs/judge-demo.md`) green; it is software evidence only. Separately
verify Quest/controller behavior, phone routing, target Jetson inference, motor
direction and starting PWM, power under load, and disconnect stopping on the
real rig. Record latency and throughput measurements with their source, load,
and conditions. Keep simulated/recorded sources visible and preserve evidence
for the pitch and Devpost.

First-time Jetson setup (scripts, service, TLS, measurements): `docs/jetson-bringup.md`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
