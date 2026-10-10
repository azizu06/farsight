# Face enrollment and names

Farsight can match visible faces against a small local gallery of people who
enroll themselves. The phone takes an enrollment photo; YuNet finds and aligns
the face, and SFace produces the template used for later comparisons. A face
without a clear match remains **Unknown**. This is separate from the existing
UltraFace detector and rescue-drill scoring.

The live view shows up to two small **LAST SEEN** cards with a name and the
original face thumbnail used for that match. Cards clear after one second,
on a camera-source change, or when the identity connection is lost. They are a
separate overlay that can clear even if the camera image freezes. A name is not
drawn over a face in a newer video frame; the thumbnail makes the observed face
explicit instead of claiming that delayed recognition is a current-frame result.

## Install the models on the Jetson

From the repository root, use the Python environment that runs the rover:

```bash
python tools/setup_identity_models.py
```

The installer downloads about 39 MB into the ignored `models/identity` directory,
checks every file's pinned size and SHA-256, and keeps the model licenses beside
the weights. Re-running it reuses verified files, including when offline. Failed
downloads leave any previous file intact. It does not modify or restart services.
For a different directory, pass `--destination /absolute/path/to/identity`.

Configure the **existing rover process** with these variables, substituting the
actual repository directory:

```bash
export FARSIGHT_IDENTITY_YUNET="$PWD/models/identity/face_detection_yunet_2023mar.onnx"
export FARSIGHT_IDENTITY_SFACE="$PWD/models/identity/face_recognition_sface_2021dec.onnx"
export FARSIGHT_IDENTITY_GALLERY="$PWD/models/identity/gallery.json"
```

Keep the current camera, motor, phone bridge, ports, and navigation launch
configuration. Add these variables to the environment of that same process and
restart it when the rover is parked. Exporting them in an unrelated terminal
does not update an already-running service. With neither model configured,
recognition stays disabled. A missing or incompatible model is reported by the
enrollment page instead of silently enabling recognition.

The implementation uses the project's OpenCV dependency (4.10 or newer) and
starts with a maximum of three inference updates per second, 20 people, and
five photos per person. These are bounds, not measured Jetson throughput.

## Enroll a person

The page is at **`/phone/identity/enroll`** on the existing operator relay.
On Aziz's Mac, open:

```text
http://localhost:8080/phone/identity/enroll
```

The page accepts a photo from the phone's camera or a saved JPEG/PNG. It uses the
native file picker, so HTTP works without a browser camera-permission prompt.
On a Mac, a phone photo copied or AirDropped to the Mac can be chosen from disk.

The native operator app deliberately suspends its bridge when it enters the
background. Opening Safari therefore interrupts its local
`http://127.0.0.1:8081` relay; that is not a reliable enrollment route.

A candidate phone route is Safari on the **operator** iPhone directly to
`http://<ROVER_PHONE_TAILNET_IP>:8081/phone/identity/enroll`, with its Tailscale VPN
active. The rover's capture iPhone must remain in its foreground capture app
and keep forwarding to the Jetson. **This route still needs a test on the actual
phones.** Enroll while parked, then return to the operator app and restore the
production bridge before using the headset. If the direct route fails, use the
Mac page and a copied phone photo. No native in-app enrollment screen is included.

1. Park the rover and open the enrollment page.
2. Wait for **Ready to enroll**. If recognition was just enabled, tap Refresh.
3. Enter your own name and take a well-lit photo with only your face in view.
4. Check the consent box and tap **Enroll my face**.
5. Select your name and add another photo from a slightly different angle.
   Two or three clear photos are a useful starting point.
6. Return to the capture/demo apps, restore the live connection, and test your
   name in the camera view before driving again.

Photos are resized to a maximum side length of 1280 pixels before upload. The
server accepts at most 4 MiB of decoded JPEG/PNG data and requires one usable
face per enrollment photo. Rejected photos do not add a gallery entry. A photo
added to an existing person must also match that person's saved template.

## Remove enrollment data

The server stores names and face templates in the configured gallery file;
enrollment photos are not saved there. Treat the gallery as private demo data
and keep it out of Git. The default installation directory is already ignored.

Tap **Remove** next to a person to delete their templates. Repeat for each person
to empty the gallery. To remove a gallery file manually, stop the rover process
first, remove only the configured gallery file, then restart the same process.
Do not delete a gallery file while the service is writing it.

## API

All routes use the existing relay origin:

| Request | Result |
| --- | --- |
| `GET /phone/identity/enroll` | Mobile enrollment page |
| `GET /phone/identity/status` | Enabled/ready/error state and gallery count |
| `GET /phone/identity/people` | `{people: [{id, name, samples}], enabled, ready, error, ...}` |
| `POST /phone/identity/people` | Add a person or photo; returns `{id, name, samples}` |
| `DELETE /phone/identity/people/{id}` | Delete a person; returns `{removed: true}` |
| `WS /ws/identity` | Freshness-bound identity observations for the live-view cards |

POST JSON contains `name`, `image` (base64 JPEG/PNG **without** a data-URL prefix),
and `consent: true`. Include `person_id` only when adding a photo to an existing
person. The enrollment page handles resizing, explicit consent, and deletion.

Identity messages retain the original `source`, `frame`, `received`,
`gallery_generation`, and `age_ms` alongside `fresh`, `error`, and `faces`.
An optional face `thumbnail` is a base64 JPEG without a data-URL prefix, at most
112 × 112 pixels and 8 KiB. The display accepts only the active camera source,
does not refresh an old observation's age on repeated messages, and cancels a
thumbnail decode when its observation is superseded or removed. Server and
viewer both bound thumbnail work; names have no effect on motor permissions.

## Live demonstration checks

Software tests do not establish recognition accuracy on the moving rover.
Before presenting, check the actual camera at the intended distance and lighting:

- Enrolled teammates get the right name while facing the camera; a person who
  did not enroll stays Unknown.
- Turning away, covering the face, or leaving the frame does not leave an old
  name floating over a different person. Frame/source freshness must be checked
  by the live-view consumer.
- Removing someone makes their next observation Unknown without restarting.
- Phone enrollment does not break the production bridge/capture connection.
- Observe inference time and camera/control performance with recognition
  enabled. Names must never change motor permissions or stopping behavior.

Similarity scores are matching scores, not probabilities. No threshold has been
calibrated for this rig, so a conservative Unknown result is preferable to a
wrong name. Enrollment is not identity verification, authentication, or liveness
detection; a photograph may match an enrolled face.

## Model provenance

Pinned OpenCV Zoo revision:
[`47534e27c9851bb1128ccc0102f1145e27f23f98`](https://github.com/opencv/opencv_zoo/tree/47534e27c9851bb1128ccc0102f1145e27f23f98).

| Model | Bytes | License |
| --- | ---: | --- |
| [YuNet 2023mar](https://github.com/opencv/opencv_zoo/tree/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_detection_yunet) | 232,589 | MIT |
| [SFace 2021dec](https://github.com/opencv/opencv_zoo/tree/47534e27c9851bb1128ccc0102f1145e27f23f98/models/face_recognition_sface) | 38,696,353 | Apache 2.0 |

The exact SHA-256 values are recorded in `tools/setup_identity_models.py`.
Keep `YuNet-LICENSE.txt` and `SFace-LICENSE.txt` with any redistributed weights.
