"""Small dependency-free multi-object tracker that gives detections stable integer ids.

Boxes are normalised dicts with ``cls``, ``x``, ``y``, ``w``, ``h``. Each track
predicts its next box from a smoothed velocity; detections are matched to
predictions best-score-first (IoU, then a centroid-distance fallback for fast
motion), so the outcome does not depend on detection order.
"""
IOU_MATCH = .2
CENTROID_LIMIT = 1.0  # centroid fallback radius, in units of the track's larger side


def iou(a, b):
    x1, y1 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x2 = min(a["x"] + a["w"], b["x"] + b["w"])
    y2 = min(a["y"] + a["h"], b["y"] + b["h"])
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    return intersection / max(a["w"] * a["h"] + b["w"] * b["h"] - intersection, 1e-9)


def _center(box):
    return box["x"] + box["w"] / 2, box["y"] + box["h"] / 2


class _Track:
    def __init__(self, identifier, box, now):
        self.id, self.box, self.seen, self.misses = identifier, box, now, 0
        self.velocity = (0.0, 0.0)  # centre shift per frame

    def predicted(self):
        steps = self.misses + 1
        return dict(self.box, x=self.box["x"] + self.velocity[0] * steps,
                    y=self.box["y"] + self.velocity[1] * steps)

    def update(self, box, now):
        steps = self.misses + 1
        (ox, oy), (nx, ny) = _center(self.box), _center(box)
        measured = ((nx - ox) / steps, (ny - oy) / steps)
        self.velocity = tuple(.5 * old + .5 * new for old, new in zip(self.velocity, measured))
        self.box, self.seen, self.misses = box, now, 0


class Tracker:
    def __init__(self, max_age=5, max_age_s=2.0):
        self.max_age, self.max_age_s = max_age, max_age_s
        self.tracks = []
        self.next_id = 1

    def update(self, boxes, now):
        """Assign ``box["id"]`` in place and return ``boxes``. Call once per detection frame."""
        self.tracks = [t for t in self.tracks if now - t.seen < self.max_age_s]
        pairs = []
        for d, box in enumerate(boxes):
            for t, track in enumerate(self.tracks):
                if track.box["cls"] != box["cls"]:
                    continue
                guess = track.predicted()
                overlap = iou(box, guess)
                if overlap >= IOU_MATCH:
                    pairs.append((1 + overlap, d, t))
                    continue
                (gx, gy), (bx, by) = _center(guess), _center(box)
                limit = CENTROID_LIMIT * max(track.box["w"], track.box["h"])
                distance = ((gx - bx) ** 2 + (gy - by) ** 2) ** .5
                if distance < limit:
                    pairs.append((1 - distance / limit, d, t))
        matched, used = {}, set()
        for _, d, t in sorted(pairs, reverse=True):
            if d not in matched and t not in used:
                matched[d] = t
                used.add(t)
        for t, track in enumerate(self.tracks):
            if t not in used:
                track.misses += 1
        for d, box in enumerate(boxes):
            if d in matched:
                track = self.tracks[matched[d]]
                track.update(box.copy(), now)
            else:
                track = _Track(self.next_id, box.copy(), now)
                self.next_id += 1
                self.tracks.append(track)
            box["id"] = track.id
        self.tracks = [t for t in self.tracks if t.misses <= self.max_age]
        return boxes
