"""Bounded phone JPEG/ARKit archive for post-run Gaussian reconstruction.

The observer runs after MapState releases its lock. Small metadata and at most
eight validated RGB-D frame references enter the queue. Sampling,
filesystem work and JSON serialization belong to the writer thread. ARKit's
camera-to-world pose already uses OpenGL camera axes.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import threading
import time
import uuid

import numpy as np

from .phone import measured_points

POINT_DTYPE = np.dtype([('xyz', '<f4', (3,)), ('rgb', 'u1', (3,))])
FRAME_POINTS = 12_288
MERGED_POINTS = 500_000


def publish_manifest(run, document):
    temporary = run / 'transforms.json.tmp'
    with temporary.open('w') as output:
        json.dump(document, output)
        output.write('\n')
    temporary.replace(run / 'transforms.json')


def merge_run_points(run, max_points=MERGED_POINTS):
    """Export first real samples per 1cm cell; also recover an interrupted run offline."""
    if not 1 <= max_points <= MERGED_POINTS:
        raise ValueError('PLY point budget must be 1..500000')
    run = Path(run)
    document = json.loads((run / 'transforms.json').read_text())
    cells, truncated = {}, False
    for frame in document['frames']:
        path = (run / frame['points_path']).resolve()
        if not path.is_relative_to(run.resolve()):
            raise ValueError('Point file escapes run directory')
        with path.open('rb') as source:
            raw = source.read(FRAME_POINTS * POINT_DTYPE.itemsize + 1)
        if len(raw) != frame['points_count'] * POINT_DTYPE.itemsize or len(raw) > FRAME_POINTS * POINT_DTYPE.itemsize:
            raise ValueError('Invalid captured point file')
        records = np.frombuffer(raw, dtype=POINT_DTYPE)
        if not np.isfinite(records['xyz']).all():
            raise ValueError('Nonfinite captured point')
        keys = np.ascontiguousarray(np.floor(records['xyz'] * 100).astype('<i4'))
        keys = keys.view('V12').reshape(-1)
        _, indices = np.unique(keys, return_index=True)
        for i in indices:
            key = keys[i].tobytes()
            if key not in cells:
                if len(cells) == max_points:
                    truncated = True
                    continue
                cells[key] = raw[i * POINT_DTYPE.itemsize:(i + 1) * POINT_DTYPE.itemsize]
    header = ('ply\nformat binary_little_endian 1.0\n'
              'comment measured ARKit world metres; 1cm cells; no synthesized points\n'
              f'element vertex {len(cells)}\n'
              'property float x\nproperty float y\nproperty float z\n'
              'property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n')
    temporary = run / 'dense.ply.tmp'
    with temporary.open('wb') as output:
        output.write(header.encode())
        for record in cells.values():
            output.write(record)
    temporary.replace(run / 'dense.ply')
    cloud = dict(file_path='dense.ply', points=len(cells), voxel_m=.01,
                 complete=True, truncated=truncated, max_points=max_points)
    document['point_cloud'] = cloud
    publish_manifest(run, document)
    return cloud


class PhoneRunCapture:
    def __init__(self, root, *, hz=2.5, queue_size=8, max_bytes=2 * 1024**3,
                 max_merge_points=MERGED_POINTS, idle_seconds=5, max_frames=3000):
        if (hz <= 0 or queue_size < 1 or max_bytes < 1 or idle_seconds <= 0
                or not 1 <= max_frames <= 3000 or not 1 <= max_merge_points <= MERGED_POINTS):
            raise ValueError('Capture budgets must be positive and bounded')
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
        self.finalized = False
        self.max_bytes, self.max_merge_points = max_bytes, max_merge_points
        self.disk_bytes = self.manifest_bytes = 0
        self.disk_limited = False
        self.idle_seconds, self.last_written = idle_seconds, 0
        self.max_frames = max_frames

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
        """Queue a bounded frame reference; never sample or write in this observer."""
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
                self.queue.put_nowait((scope, frame, metadata))
            except queue.Full:
                self.dropped += 1

    def status(self):
        """No disk access or writer lock on the health/control path."""
        return dict(enabled=True, hz=1 / self.interval, queued=self.queue.qsize(),
                    written=self.written, dropped=self.dropped, failed=self.failed,
                    disk_bytes=self.disk_bytes, disk_limit_bytes=self.max_bytes,
                    disk_limited=self.disk_limited,
                    run=str(self.run) if self.run else None, error=self.error)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _consume(self):
        try:
            self.disk_bytes = sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file())
        except OSError as exc:
            self.disk_limited, self.error = True, str(exc)
        while not self.stop.is_set() or not self.queue.empty():
            try:
                item = self.queue.get(timeout=.1)
            except queue.Empty:
                if self.run is not None and time.monotonic() - self.last_written >= self.idle_seconds:
                    self._finish()
                continue
            try:
                if self._write(item):
                    self.written += 1
                    self.error = None
                    self.last_written = time.monotonic()
                else:
                    self.dropped += 1
            except (OSError, ValueError) as exc:
                self.failed += 1
                self.error = str(exc)
                self.disk_limited = True  # Preserve earlier pairs; never accumulate failed-write orphans.
            finally:
                self.queue.task_done()
        self._finish()

    def _finish(self):
        try:
            self._finalize()
        except (OSError, ValueError) as exc:
            self.failed += 1
            self.error = str(exc)
            self.finalized, self.disk_limited = True, True

    def _finalize(self):
        if self.run is not None and not self.finalized and self.document['frames']:
            self.document['point_cloud'] = merge_run_points(self.run, self.max_merge_points)
            updated = (self.run / 'transforms.json').stat().st_size
            self.disk_bytes += (self.run / 'dense.ply').stat().st_size + updated - self.manifest_bytes
            self.manifest_bytes = updated
            self.finalized = True

    def _write(self, item):
        if self.disk_limited:
            return False
        scope, frame, metadata = item
        jpeg = frame.jpeg
        if metadata['tracking'] == 'normal':
            xyz, rgb = measured_points(frame, max_points=FRAME_POINTS, phase=metadata['frame_id'])
        else:
            xyz, rgb = np.empty((0, 3), np.float32), np.empty((0, 3), np.uint8)
        records = np.empty(len(xyz), dtype=POINT_DTYPE)
        records['xyz'], records['rgb'] = xyz, rgb
        if scope != self.scope or self.finalized or len(self.document['frames']) >= self.max_frames:
            self._finalize()
            run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ-') + uuid.uuid4().hex[:8]
            run = self.root / run_id
            (run / 'images').mkdir(parents=True)
            self.run, self.scope = run, scope
            self.finalized = False
            self.manifest_bytes = 0
            self.document = dict(camera_model='OPENCV', k1=0, k2=0, p1=0, p2=0,
                                 session_id=scope[0], map_epoch=scope[1], socket_generation=scope[2],
                                 points_format='xyz_f32le_rgb8', points_coordinate_system='arkit_world_y_up_metres',
                                 frames=[])
        metadata['file_path'] = f'images/{len(self.document["frames"]):06d}.jpg'
        metadata['points_path'] = f'images/{len(self.document["frames"]):06d}.points.bin'
        metadata['points_count'] = len(records)
        # Account for the temporary full manifest and reserve the final capped PLY.
        # Existing archived data counts towards the budget and is never evicted.
        manifest_bound = self.manifest_bytes + len(json.dumps(metadata).encode()) + 4096
        reserve = self.max_merge_points * POINT_DTYPE.itemsize + 1024
        if self.disk_bytes + len(jpeg) + records.nbytes + manifest_bound + reserve > self.max_bytes:
            self.disk_limited, self.error = True, 'Capture disk budget reached; earlier files preserved'
            return False
        points_path = self.run / metadata['points_path']
        temporary = points_path.with_suffix('.bin.tmp')
        temporary.write_bytes(records.tobytes())
        temporary.replace(points_path)
        image_path = self.run / metadata['file_path']
        temporary = image_path.with_suffix('.jpg.tmp')
        temporary.write_bytes(jpeg)
        temporary.replace(image_path)
        self.document['frames'].append(metadata)
        publish_manifest(self.run, self.document)
        updated = (self.run / 'transforms.json').stat().st_size
        self.disk_bytes += len(jpeg) + records.nbytes + updated - self.manifest_bytes
        self.manifest_bytes = updated
        return True


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Rebuild a saved run PLY after an interrupted shutdown')
    parser.add_argument('run', type=Path)
    args = parser.parse_args()
    print(json.dumps(merge_run_points(args.run)))
