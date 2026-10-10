"""Bounded live mission evidence, persisted locally when a directory is set."""

from collections import deque
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time

from .drill import Drill

RANGE_STOP_CM = 25  # ControlState blocks forward motion below this ultrasonic range


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


class ControlTelemetry:
    """Mission summary built from sampled control status; never changes control.

    Samples arrive at most SAMPLE_HZ, so a state shorter than one period can be
    missed. Every collection is bounded regardless of mission length.
    """
    SAMPLE_HZ = 10
    TIMELINE = 300
    INTERVALS = 100
    REASONS = 64
    ESTOP_DEBOUNCE_S = 0.5

    def __init__(self, on_flag=None):
        self.on_flag = on_flag
        self.samples = 0
        self.last_sample = None
        self.min_range_cm = None
        self.min_range_at = None
        self.range_cm = None
        self.range_at = None
        self.estops = 0
        self.estop_active = False
        self.estop_at = -math.inf
        self.reason = None
        self.reasons = {}
        self.open = {}
        self.lost = {kind: deque(maxlen=self.INTERVALS) for kind in ("rover_link", "controller", "camera")}
        self.lost_counts = dict.fromkeys(self.lost, 0)
        self.seen = dict.fromkeys(self.lost, False)
        self.timeline = deque(maxlen=self.TIMELINE)

    def flag(self, kind, ts, **fields):
        event = dict(kind=kind, ts=ts, **fields)
        self.timeline.append(event)
        if self.on_flag:
            self.on_flag(event)

    def observe(self, status, camera=None, now=None):
        """Fold one control_status packet (plus optional camera freshness) into the summary."""
        if not isinstance(status, dict):
            return
        now = time.time() if now is None else now
        self.samples += 1
        self.last_sample = now
        cm = _number(status.get("range_cm"))
        if cm is not None and cm > 0:
            self.range_cm, self.range_at = cm, now
            if self.min_range_cm is None or cm < self.min_range_cm:
                self.min_range_cm, self.min_range_at = cm, now
        reason = status.get("reason")
        reason = reason[:64] if isinstance(reason, str) else None
        blocked = status.get("estop") is True and (
            reason == "forward_blocked" or (cm is not None and cm < RANGE_STOP_CM))
        if blocked and not self.estop_active and now - self.estop_at > self.ESTOP_DEBOUNCE_S:
            self.estops += 1
            self.flag("estop", now, range_cm=cm, reason=reason)
        if blocked:
            self.estop_at = now
        self.estop_active = blocked
        if reason != self.reason:
            self.reason = reason
            if reason and reason != "active":
                entry = self.reasons.get(reason)
                if entry is None and len(self.reasons) < self.REASONS:
                    entry = self.reasons[reason] = dict(count=0, first_ts=now, last_ts=now)
                if entry is not None:
                    entry["count"] += 1
                    entry["last_ts"] = now
                self.flag("stop", now, reason=reason, range_cm=cm,
                          requires_release=status.get("requires_release") is True)
        up = dict(rover_link=status.get("rover") if isinstance(status.get("rover"), bool) else None,
                  camera=camera if isinstance(camera, bool) else None,
                  controller=bool(status["session_id"]) if "session_id" in status else None)
        for kind, alive in up.items():
            if alive is None:
                continue
            if alive:
                self.seen[kind] = True
                started = self.open.pop(kind, None)
                if started is not None:
                    self.lost[kind].append(dict(start=started, end=now, duration_s=round(now - started, 2)))
                    self.flag(kind + "_restored", now, lost_s=round(now - started, 2))
            elif self.seen[kind] and kind not in self.open:
                # Only a loss after the link was up counts; startup is not a loss.
                self.open[kind] = now
                self.lost_counts[kind] += 1
                self.flag(kind + "_lost", now)

    def range_near(self, now, max_age=1.0):
        return self.range_cm if self.range_at is not None and now - self.range_at <= max_age else None

    def summary(self, now=None):
        now = time.time() if now is None else now
        lost = {}
        for kind, intervals in self.lost.items():
            rows = list(intervals)
            if kind in self.open:
                rows.append(dict(start=self.open[kind], end=None, duration_s=round(now - self.open[kind], 2)))
            lost[kind] = dict(count=self.lost_counts[kind], total_s=round(sum(r["duration_s"] for r in rows), 2),
                              intervals=rows)
        return dict(samples=self.samples, sample_hz=self.SAMPLE_HZ, last_sample_ts=self.last_sample,
                    min_range_cm=self.min_range_cm, min_range_at=self.min_range_at,
                    estop_count=self.estops, stop_reasons={k: dict(v) for k, v in self.reasons.items()},
                    lost=lost)


class Mission:
    PERSON_REFRESH_S = 3.0  # at most one SBAR refresh per person track in this window

    def __init__(self, directory=None, record=False, drill_clock=time.monotonic):
        self.started = time.time()
        self.id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.directory = Path(directory) / self.id if directory else None
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)
        self.recording = bool(record)
        if self.recording and self.directory is None:
            raise ValueError("Recording requires --mission-dir")
        self.record_started = time.monotonic()
        self.record_frames = 0
        if self.recording:
            (self.directory / "frames").mkdir()
            self.finish_recording(elapsed=0)
        self.events = deque(maxlen=1000)
        self.encounters = {}
        self.lock = threading.RLock()
        self.drill_score = Drill(clock=drill_clock)
        self.drill_seen = set()
        self.saved_snapshots = 0
        self.triage = {}
        self.classes = {}
        self.flagged_saved = 0
        self.telemetry = ControlTelemetry(on_flag=self.persist_flag)
        # Called as on_person(key, encounter, jpeg, snapshot_url) for each new person track.
        self.on_person = None
        self.person_notified = {}  # key -> (down flag, time) of the last SBAR request
        # Returns {face_detector, face_error}; the server points it at the perception service.
        self.face_status = dict

    def record(self, kind, value, elapsed=None):
        """Append replay evidence to the same mission directory, without control tokens."""
        if not self.recording:
            return
        with self.lock:
            event = dict(kind=kind, t=time.monotonic() - self.record_started if elapsed is None else elapsed,
                         ts=time.time())
            if kind == "frame":
                name = f"frames/{self.record_frames:08d}.jpg"
                (self.directory / name).write_bytes(value.jpeg)
                self.record_frames += 1
                event.update(file=name, frame=value.frame_id, source=value.source)
            else:
                event["packet"] = {k: v for k, v in value.items() if k not in ("permit", "session_id", "received")}
            with (self.directory / "replay.jsonl").open("a") as log:
                log.write(json.dumps(event) + "\n")

    def finish_recording(self, elapsed=None):
        if self.recording:
            (self.directory / "replay.json").write_text(json.dumps(dict(
                version=1, duration_s=time.monotonic() - self.record_started if elapsed is None else elapsed)))

    def detect(self, packet, frame):
        self.record("detections", packet)
        with self.lock:
            self.drill_score.observe(packet["boxes"])
            drilling = self.drill_score.running()
            for box in packet["boxes"]:
                key = f'{frame.source}:{box["id"]}'
                previous = self.encounters.get(key)
                now = time.time()
                self.encounters[key] = dict(box, source=frame.source, last_seen=now,
                       first_seen=previous["first_seen"] if previous else now,
                       first_range_cm=previous.get("first_range_cm") if previous
                       else self.telemetry.range_near(now))
                if drilling and box["cls"] == "person":
                    self.drill_seen.add(key)
                if previous:
                    self._refresh_person(key, box, now, frame)
                    continue
                cls = str(box.get("cls", "object"))[:32]
                if cls in self.classes or len(self.classes) < 100:
                    self.classes[cls] = self.classes.get(cls, 0) + 1
                self.telemetry.flag("detection", now, cls=cls, key=key, range_cm=self.encounters[key]["first_range_cm"])
                image_name = f'detection-{self.saved_snapshots}-{frame.frame_id}.jpg'
                save = self.directory is not None and self.saved_snapshots < 1000
                event = dict(kind="detection", ts=time.time(), frame=frame.frame_id, source=frame.source,
                             box=box, snapshot=image_name if save else None,
                             snapshot_url=f"/mission/snapshots/{image_name}" if save else None)
                self.events.append(event)
                if save:
                    (self.directory / image_name).write_bytes(frame.jpeg)
                    self.saved_snapshots += 1
                    with (self.directory / "events.jsonl").open("a") as log:
                        log.write(json.dumps(event) + "\n")
                if box["cls"] == "person" and self.on_person:
                    self._notify_person(key, now, frame, event["snapshot_url"])
            # Cap in-memory tracks; persisted evidence remains in the mission directory.
            while len(self.encounters) > 500:
                self.encounters.pop(next(iter(self.encounters)))

    def _notify_person(self, key, now, frame, snapshot_url):
        self.person_notified[key] = (self.encounters[key].get("down"), now)
        while len(self.person_notified) > 500:
            self.person_notified.pop(next(iter(self.person_notified)))
        self.on_person(key, dict(self.encounters[key]), frame.jpeg, snapshot_url)

    def _refresh_person(self, key, box, now, frame):
        """Re-issue on_person when a person's down flag differs from the one their SBAR was written for.

        Comparing with the last notified flag (not the previous frame) keeps a change that lands inside
        the rate-limit window pending until the window passes.
        """
        down = box.get("down")
        if box.get("cls") != "person" or not self.on_person or type(down) is not bool:
            return
        noted, at = self.person_notified.get(key, (None, -self.PERSON_REFRESH_S))
        if down != noted and now - at >= self.PERSON_REFRESH_S:
            self._notify_person(key, now, frame, None)  # None keeps the first snapshot

    def observe_control(self, status, camera=None):
        """Mission sampler hook: read-only control status, at most ControlTelemetry.SAMPLE_HZ."""
        with self.lock:
            self.telemetry.observe(status, camera=camera)

    def persist_flag(self, event):
        # Detection events already persist with their snapshot; flag the rest.
        if self.directory is None or event["kind"] == "detection" or self.flagged_saved >= 10000:
            return
        self.flagged_saved += 1
        with (self.directory / "events.jsonl").open("a") as log:
            log.write(json.dumps(event) + "\n")

    def encounter(self, key):
        with self.lock:
            found = self.encounters.get(key)
            return dict(found) if found else None

    def set_sbar(self, key, sbar):
        """Store a person's SBAR and rewrite the persisted report so it always carries one."""
        with self.lock:
            self.triage[key] = sbar
            while len(self.triage) > 500:
                self.triage.pop(next(iter(self.triage)))
            if self.directory:
                with (self.directory / "triage.jsonl").open("a") as log:
                    log.write(json.dumps(dict(key=key, **sbar)) + "\n")
                temporary = self.directory / "report.json.tmp"
                temporary.write_text(json.dumps(self.report()))
                temporary.replace(self.directory / "report.json")

    def start_drill(self, duration=90, victims=3):
        with self.lock:
            self.drill_seen.clear()
            self.drill_score.start(duration, victims)
            return self.drill()

    def drill_motion(self, v, w, blocked):
        """ControlState.on_output hook: the applied command feeds the route home and e-stop count."""
        self.drill_score.motion(v, w, blocked)

    def drill(self):
        return dict(self.drill_score.status(), person_encounters=len(self.drill_seen), **self.face_status(),
                    note="Victims are faces held in view for 1 s; the route home is dead reckoning and approximate")

    def report(self):
        with self.lock:
            return dict(mission_id=self.id, started_at=self.started, duration_s=round(time.time() - self.started),
                        persisted=bool(self.directory), events=list(self.events),
                        encounters=[dict(entry, key=key, sbar=self.triage.get(key)) for key, entry in self.encounters.items()],
                        triage=dict(self.triage),
                        saved_snapshots=self.saved_snapshots, snapshot_limit=1000, drill=self.drill(),
                        summary=dict(self.telemetry.summary(), detections_by_class=dict(self.classes),
                                     encounters=len(self.encounters)),
                        timeline=list(self.telemetry.timeline))
