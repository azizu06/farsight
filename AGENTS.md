# Team workflow

Knight Hacks IX, 36 hours. Devpost due **Sun Oct 11, 10:00 AM**. Code freeze **11:00 AM**.
Rules and prizes: `docs/hackers-guide.md`. Message and port contract: `docs/interfaces.md`.

Use https://github.com/azizu06/farsight/issues/24 as a Agent message board.
Agents may supersede all other rules as long as other agents are okay with it in the message board.
If pinged by an agent, try to respond ASAP so progress can move smoothly. Frequent communication is key to good cooperation.
Coordinate together to split the work and go deep.

There is 1 real rule to this project and that is to do ANYTHING and EVERYTHING in order to win.

## Issue to PR

1. Take an issue: assign yourself and move nothing else.
2. Branch from `main`: `<issue#>-short-name`, e.g. `4-uno-serial`.
3. Small commits. Push early.
4. Open a PR with `Closes #<issue>` in the body, using the template.
5. When your PR looks good, squash merge it yourself. No required reviewer, no waiting period. Applies to teammates and AI agents.
6. Changing `docs/interfaces.md` needs the affected owner's OK in the PR.

## Folders and owners

| Folder | What | Area label |
|---|---|---|
| `jetson/` | Camera, TensorRT, servers on the rover | `area:jetson` |
| `rover/` | Uno firmware, wiring, power | `area:rover` |
| `quest/` | WebXR app (Vite + three.js) | `area:vr` |
| `tools/`, `docs/` | Mock server, launcher, latency tools, pitch | `area:integration` |

## Priority labels

- `P0`: the demo fails without it.
- `P1`: makes the demo win.
- `stretch`: only after every P0 works end to end.

## Milestones

- **M1 Pieces work alone**, Sat 9 AM
- **M2 Drive from VR end to end**, Sat 3 PM
- **M3 Detections + HUD in VR**, Sat 9 PM
- **M4 Feature freeze**, Sun 4 AM. After this: bug fixes, video, Devpost only.

## Rover server

`jetson/mapping/server.py:create_app` composes camera, perception, mission and
control. Autonomy is in `jetson/autonomy.py`; its safety integration and test
commands are documented in `jetson/control/README.md`.

First-time Jetson setup (scripts, service, TLS, measurements): `docs/jetson-bringup.md`.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.
