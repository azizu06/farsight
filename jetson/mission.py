"""Bounded live mission evidence, persisted locally when a directory is set."""

from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time


class Mission:
    def __init__(self, directory=None):
        self.started = time.time()
        self.id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.directory = Path(directory) / self.id if directory else None
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)
        self.events = deque(maxlen=1000)
        self.encounters = {}
        self.lock = threading.RLock()
        self.drill_start = None
        self.drill_duration = 90
        self.drill_seen = set()
        self.saved_snapshots = 0
        self.triage = {}
        # Called as on_person(key, encounter, jpeg, snapshot_url) for each new person track.
        self.on_person = None

    def detect(self, packet, frame):
        with self.lock:
            for box in packet["boxes"]:
                key = f'{frame.source}:{box["id"]}'
                previous = self.encounters.get(key)
                self.encounters[key] = dict(box, source=frame.source, last_seen=time.time(),
                       first_seen=previous["first_seen"] if previous else time.time())
                if self.drill_start and time.monotonic() - self.drill_start <= self.drill_duration and box["cls"] == "person":
                    self.drill_seen.add(key)
                if previous:
                    continue
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
                    self.on_person(key, dict(self.encounters[key]), frame.jpeg, event["snapshot_url"])
            # Cap in-memory tracks; persisted evidence remains in the mission directory.
            while len(self.encounters) > 500:
                self.encounters.pop(next(iter(self.encounters)))

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

    def start_drill(self, duration=90):
        with self.lock:
            self.drill_start = time.monotonic()
            self.drill_duration = duration
            self.drill_seen.clear()
            return self.drill()

    def drill(self):
        remaining = max(0, self.drill_duration - (time.monotonic() - self.drill_start)) if self.drill_start else None
        return dict(active=remaining is not None and remaining > 0, remaining_s=round(remaining, 1) if remaining is not None else None,
                    person_encounters=len(self.drill_seen), duration_s=self.drill_duration,
                    note="Tracks are encounters, not verified distinct people")

    def report(self):
        with self.lock:
            return dict(mission_id=self.id, started_at=self.started, duration_s=round(time.time() - self.started),
                        persisted=bool(self.directory), events=list(self.events),
                        encounters=[dict(entry, key=key, sbar=self.triage.get(key)) for key, entry in self.encounters.items()],
                        triage=dict(self.triage),
                        saved_snapshots=self.saved_snapshots, snapshot_limit=1000, drill=self.drill())
