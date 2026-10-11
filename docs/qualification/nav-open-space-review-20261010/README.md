# Open-space navigation review — 2026-10-10

CPU witnesses for PR #162, pinned to its head `04602b0fa811a1274567a91c6f15d5e29072da35` and baseline `1af477195155f3343d8d9a0d0c878d343bf275dd`. No physical rover or camera was used. Nineteen local-motion tests pass, but the production codec witnesses expose two missing cases:

- A 20 cm upper obstacle with confidence 1 causes both versions to reverse. Confidence 0 causes the production encoder to censor the same measurement to zero; baseline turns in place, while the proposed policy commands forward 0.2 m/s. Zero conflates unknown, censored near measurements and beyond-range returns.
- At an 8 cm camera height with a floor edge 60 cm ahead, the proposed policy commands forward where baseline turns. Its body-height probes all sit level with or above the camera and do not establish floor support. The map timestamp is formally fresh (100 ms), but its previously observed free-floor content conflicts with current depth.

Preserve unknown depth as blocked, or carry a distinct confirmed-clear validity class from the producer. Verify downward support independently of camera mounting height. Scan-direction hysteresis can be reviewed separately. The navigation owner must resolve these cases before deploying this missing-return policy.

`witness.py` executes pinned production `encode_depth`, bridge `decode_depth`, frame transforms and both LocalMotion implementations. It verifies the reviewed candidate blob before execution. `evidence.json` records actual outputs. Run from the repository root:

```sh
git fetch origin pull/162/head
.venv/bin/python docs/qualification/nav-open-space-review-20261010/witness.py --repo . --out /tmp/farsight-nav-review.json
```

If the pinned candidate is absent locally, the script fetches that immutable file from GitHub and checks its blob. These fixtures establish policy regressions under their stated conditions; they do not establish real-world collision rates or qualify an alternative navigation design.
