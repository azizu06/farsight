# Team workflow

Knight Hacks IX, 36 hours. Devpost due **Sun Oct 11, 10:00 AM**. Code freeze **11:00 AM**.
Rules and prizes: `docs/hackers-guide.md`. Message and port contract: `docs/interfaces.md`.

All code in this repo is written during Knight Hacks IX. Do not paste code from earlier
projects (the guide requires original work).

## Issue to PR

1. Take an issue: assign yourself and move nothing else.
2. Branch from `main`: `<issue#>-short-name`, e.g. `4-uno-serial`.
3. Small commits. Push early.
4. Open a PR with `Closes #<issue>` in the body, using the template.
5. One teammate glances at it (5 minutes max), then squash merge. If nobody is free
   within 15 minutes and the change only touches your own folder, merge it yourself.
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
