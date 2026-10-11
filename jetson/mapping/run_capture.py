"""Bounded phone JPEG/ARKit archive for post-run Gaussian reconstruction.

The observer runs after MapState releases its lock. Only JPEG bytes and small
metadata enter the queue; filesystem work and JSON serialization belong to the
writer thread. ARKit's camera-to-world pose already uses OpenGL camera axes.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import threading
import time
import uuid


class PhoneRunCapture:
    def __init__(self, root, *, hz=2.5, queue_size=8):
        self.root = Path(root).expanduser()
        self.interval = 1 / hz
        self.queue = queue.Queue(maxsize=queue_size)
        self.stop = threading.Event()
        self.thread = None
        self.offer_lock = threading.Lock()
        self.sample_scope = None
        self.next_sample = 0
        self.written = self.dropped = self.failed = 0
        self.error = None
        self.run = None
        self.scope = None
        self.document = None

    @classmethod
    def from_environment(cls, *, demo_mode=False, synthetic=False):
        enabled = os.environ.get('FARSIGHT_RUN_CAPTURE', '').strip()
        if synthetic or not (enabled == '1' or (not enabled and demo_mode)):
            return None
        return cls(os.environ.get('FARSIGHT_RUN_CAPTURE_DIR') or '~/captures')

    def start(self):
        self.thread = threading.Thread(target=self._consume, name='phone-run-capture', daemon=True)
        self.thread.start()

    def offer_frame(self, frame, *, now=None):
        """Offer an already validated RGB-D frame without retaining depth/RGB arrays."""
        header = frame.header
        scope = (header['session_id'], header['map_epoch'], header.get('_mapping_socket_generation', 0))
        now = time.monotonic() if now is None else now
        with self.offer_lock:
            if self.stop.is_set():
                return
            if scope == self.sample_scope and now < self.next_sample:
                return
            self.sample_scope, self.next_sample = scope, now + self.interval
            image = header['image']
            k = image['intrinsics']  # Native JPEG K, column-major; not depth calibration.
            metadata = dict(file_path='', transform_matrix=frame.transform.tolist(),
                            fl_x=k[0], fl_y=k[4], cx=k[6], cy=k[7],
                            w=image['width'], h=image['height'],
                            frame_id=header['frame_id'], t_capture=header['t_capture'],
                            tracking=header['tracking'])
            try:
                self.queue.put_nowait((scope, frame.jpeg, metadata))
            except queue.Full:
                self.dropped += 1

    def status(self):
        """No disk access or writer lock on the health/control path."""
        return dict(enabled=True, hz=1 / self.interval, queued=self.queue.qsize(),
                    written=self.written, dropped=self.dropped, failed=self.failed,
                    run=str(self.run) if self.run else None, error=self.error)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _consume(self):
        while not self.stop.is_set() or not self.queue.empty():
            try:
                item = self.queue.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                self._write(item)
                self.written += 1
                self.error = None
            except OSError as exc:
                self.failed += 1
                self.error = str(exc)
            finally:
                self.queue.task_done()

    def _write(self, item):
        scope, jpeg, metadata = item
        if scope != self.scope:
            run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ-') + uuid.uuid4().hex[:8]
            run = self.root / run_id
            (run / 'images').mkdir(parents=True)
            self.run, self.scope = run, scope
            self.document = dict(camera_model='OPENCV', k1=0, k2=0, p1=0, p2=0,
                                 session_id=scope[0], map_epoch=scope[1], socket_generation=scope[2],
                                 frames=[])
        metadata['file_path'] = f'images/{len(self.document["frames"]):06d}.jpg'
        image_path = self.run / metadata['file_path']
        temporary = image_path.with_suffix('.jpg.tmp')
        temporary.write_bytes(jpeg)
        temporary.replace(image_path)
        self.document['frames'].append(metadata)
        temporary = self.run / 'transforms.json.tmp'
        with temporary.open('w') as output:
            # dump streams Python-sized pieces instead of one large GIL-held dumps.
            json.dump(self.document, output)
            output.write('\n')
        temporary.replace(self.run / 'transforms.json')
