# Opt-in Named Face Recognition Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Steps use checkbox syntax.

**Goal:** A volunteer enrolls their own name and photo on a phone; the Jetson publishes names for sufficiently clear matching faces in the rover camera.

**Architecture:** YuNet finds faces and landmarks, SFace embeds aligned faces, and a bounded local gallery matches embeddings conservatively. A separate optional background service publishes original-frame metadata; phone upload routes and a Quest consumer use the existing transport without altering motor or drill scoring behavior.

**Tech Stack:** Existing Python/OpenCV/FastAPI, vanilla mobile HTML, existing Quest TypeScript renderer.

**Spec:** User's request on issue #141; this plan defines its additive interfaces. Shared-file and Quest ownership handoffs are tracked on #24.

## Global Constraints

- Opt-in model configuration; unavailable models produce explicit disabled/error status and leave existing camera/control usable.
- Existing face/drill scoring and motor permissions remain unchanged.
- Names are provided by consenting volunteers; nonmatches and ambiguous matches are Unknown.
- Gallery: at most20 people,5 samples/person,128 finite normalized embedding components; name <=48 characters, no control characters.
- Upload <=4MiB decoded bytes; JPEG/PNG <=4096 per side and12 million pixels. Never persist enrollment photos.
- Live recognition maximum3Hz, latest frame only, single inference worker, no unbounded enrollment/inference queues.
- Result carries original source/frame/receipt, age and gallery generation; stale/source-replaced/deleted-gallery results lose names.
- Display names are cosmetic; no name authorizes motor movement or claims durable spatial identity.

## Review Focus

- Multiple faces, oversized compressed images and malformed bytes must fail enrollment without allocations for huge decoded images.
- A near tie between volunteers must return Unknown rather than choosing a name.
- Deleting a volunteer while an inference is running must prevent publishing that deleted name.
- Slow inference must not stall camera/control or renew the input frame's age.
- Phone Safari over the existing bridge must support camera photo selection on HTTP; real route remains an acceptance check.

### Task1: Engine and local gallery

Files: `jetson/identity_engine.py`, `jetson/tests/test_identity_engine.py`.
Interfaces: `IdentityEngine(detector_path,recognizer_path).infer(bytes)` returns normalized boxes with embeddings; `enroll_embedding(bytes)` requires exactly one raw detected face. `Gallery(path=None).enroll(name,embedding,person_id=None)`, `people()`, `remove(id)`, `match(embedding)` and `generation`.

- [ ] Write failing unittest coverage for bounds, unknown/ambiguous matching, deletion, persistence and image admission.
- [ ] Implement YuNet landmarks/SFace alignment and normalized embeddings with one engine lock.
- [ ] Verify focused unit tests and official model load/blank-image smoke; real accuracy remains unverified.

### Task2: Background service and API

Files: `jetson/identity_service.py`, `jetson/identity_api.py`, `jetson/tests/test_identity_service.py`, narrow owner-approved wiring in `jetson/mapping/server.py`.
Interfaces: `IdentityService(camera,detector_path=None,recognizer_path=None,gallery_path=None,fps=3)`: async `start/close/enroll`, `status/people/snapshot/remove`; `identity_router(service)` returns APIRouter.
Routes: GET `/phone/identity/enroll`; GET/POST `/phone/identity/people`; DELETE `/phone/identity/people/{id}`; GET `/phone/identity/status`; WS `/ws/identity`.
POST JSON `{name,image:<base64>,consent:true,person_id?:id}`. HTTP bodies bounded before JSON parsing. WS packet `{type:"identity",source,frame,received,age_ms,fresh,faces:[{x,y,w,h,conf,name,person_id,similarity}],gallery_generation,infer_ms,error}`; absent/stale results have empty faces. `similarity` is a cosine score, not a probability.

- [ ] Write failing tests for optional startup, stale input, source replacement, deletion during inference, busy enrollment, HTTP admission and routes.
- [ ] Implement latest-frame worker, bounded off-thread inference and original receipt age.
- [ ] Mount before static fallback using env FARSIGHT_IDENTITY_YUNET/SFACE/GALLERY; start/close through existing lifespan after owner handoff.
- [ ] Run service/API and existing camera/face tests; confirm no score/control behavior changed.

### Task3: Phone page and reproducible model setup

Files: `jetson/identity_enroll.html`, `tools/setup_identity_models.py`, `docs/face-identity.md` and focused tests.

- [ ] Build name/consent/camera-photo page with relative routes, safe DOM names, clear failures and removal; resize uploads before encoding.
- [ ] Pin official model weights/size/SHA256 and model licenses in an atomic downloader; ignored models are not committed.
- [ ] Verify downloader failures and mobile-page upload contract; document launch and hardware acceptance checks.

### Task4: Quest consumer and integration

Files: owner handoff determines new module and small renderer hooks; otherwise Firstmate consumes published WS contract.

- [ ] Agree consumer/frame matching and file boundary on #24 before touching shared UI.
- [ ] Test stale/source-switched packets and escaped names; display Unknown for unmatched faces.
- [ ] Run Quest build and simulator judge checks if Quest files change; get independent review.
- [ ] Commit and push issue branch, create PR, merge after review under standing human authorization.
- [ ] Coordinate deployment; keep real phone enrollment, Jetson inference load and Quest recognition open until measured.
