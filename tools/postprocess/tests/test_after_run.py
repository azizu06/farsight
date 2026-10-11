"""Behavior tests for the post-run transfer boundary and once-only publication."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.postprocess import after_run as pipeline


class AfterRunTests(unittest.TestCase):
    def test_capture_change_defers_even_an_older_finished_run(self):
        old = [10, 100, 50, 1000]
        live = [1, 200, 0, 100]  # no seed yet, but still an active drive
        seen, ready = pipeline.finished({}, {'old': old, 'live': live}, 0)
        self.assertEqual(ready, [])
        seen, ready = pipeline.finished(seen, {'old': old, 'live': [2, 201, 0, 101]}, 60)
        self.assertEqual(ready, [])
        _, ready = pipeline.finished(seen, {'old': old, 'live': [2, 201, 0, 101]}, 120)
        self.assertEqual(ready, ['old'])

    def test_pull_waits_full_60_seconds_and_is_bandwidth_limited(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(host='jetson', run_id='saved', local_root=Path(tmp),
                                      work_dir=Path(tmp), bwlimit=1024, poll_seconds=30)
            calls = []
            def observe(*_):
                calls.append('observe')
                return {'saved': [10, 100, 50, 1000]}
            def run(argv, **kwargs):
                calls.append('pull')
                self.assertEqual(argv[0], 'rsync')
                self.assertIn('--bwlimit=1024', argv)
                self.assertIn('--include=/images/*.jpg', argv)
                self.assertIn('--include=/dense.ply', argv)
                return subprocess.CompletedProcess(argv, 0)
            with patch.object(pipeline, 'inventory', observe), \
                 patch.object(pipeline.time, 'monotonic', side_effect=[0, 0, 30, 60, 60, 61]), \
                 patch.object(pipeline.time, 'sleep'), patch.object(pipeline, 'command', run):
                name, _, transport, pull_s, wait_s = pipeline.wait_for_run(args)
            self.assertEqual(calls, ['observe', 'observe', 'observe', 'pull'])
            self.assertEqual((name, transport, pull_s, wait_s), ('saved', 'rsync', 1, 60))

    def test_offline_uses_local_without_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = root / 'run-saved'
            (run / 'photos').mkdir(parents=True)
            (run / 'photos/transforms.json').write_text('{}')
            (run / 'dense.ply').write_text('native measured seed')
            args = argparse.Namespace(host='offline', run_id=None, local_root=root, work_dir=root / 'work')
            with patch.object(pipeline, 'inventory', side_effect=subprocess.CalledProcessError(255, 'ssh')), \
                 patch.object(pipeline, 'pull') as pull:
                name, source, transport, _, _ = pipeline.wait_for_run(args)
            self.assertEqual((name, source, transport), ('run-saved', run, 'offline-local'))
            pull.assert_not_called()

    def test_watcher_skips_completed_runs_and_processes_only_one_per_tick(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'new.done.json').write_text('{}')
            args = argparse.Namespace(host='jetson', work_dir=root, poll_seconds=30, since=None)
            signatures = {'old': [1, 2, 3, 4], 'new': [1, 2, 3, 4]}
            with patch.object(pipeline, 'inventory', return_value=signatures), \
                 patch.object(pipeline, 'finished', return_value=({}, ['old', 'new'])), \
                 patch.object(pipeline, 'pull', return_value=root / 'old') as pull, \
                 patch.object(pipeline, 'process') as process, \
                 patch.object(pipeline.time, 'sleep', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    pipeline.watch(args)
            pull.assert_called_once_with(args, 'old')
            self.assertEqual(process.call_count, 1)

    def test_prepare_drops_truncated_jpeg_and_preserves_seed_reference(self):
        import numpy as np
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'run'
            (source / 'images').mkdir(parents=True)
            frames = []
            rgb = np.random.default_rng(7).integers(0, 256, (256, 256, 3), dtype=np.uint8)
            for i in range(6):
                name = f'images/{i:06d}.jpg'
                Image.fromarray(rgb).save(source / name, quality=95)
                pose = np.eye(4)
                pose[0, 3] = i
                frames.append(dict(file_path=name, transform_matrix=pose.tolist(), t_capture=i))
            broken = source / 'images/000002.jpg'
            broken.write_bytes(broken.read_bytes()[:-500])
            (source / 'transforms.json').write_text(json.dumps(dict(frames=frames)))
            (source / 'dense.ply').write_text('ply\nelement vertex 1\nend_header\n')
            dataset, receipt = pipeline.prepare(source, root / 'prepared')
            self.assertEqual(receipt['rejected'][0]['file'], 'images/000002.jpg')
            selected = json.loads((dataset / 'transforms.json').read_text())
            self.assertEqual(selected['ply_file_path'], 'init.ply')
            self.assertNotIn('images/000002.jpg', [f['file_path'] for f in selected['frames']])
            self.assertEqual((dataset / 'init.ply').read_bytes(), (source / 'dense.ply').read_bytes())

    def test_short_valid_jpegs_survive_as_padded_png_and_long_pauses_stay_one_run(self):
        import numpy as np
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'run'
            (source / 'images').mkdir(parents=True)
            frames = []
            for i, t in enumerate([0, 1, 2, 40, 41, 42]):
                name = f'images/{i:06d}.jpg'
                Image.new('RGB', (64, 64), (100, 120, 140)).save(source / name)
                pose = np.eye(4)
                pose[0, 3] = i
                frames.append(dict(file_path=name, transform_matrix=pose.tolist(), t_capture=t))
            (source / 'transforms.json').write_text(json.dumps(dict(frames=frames)))
            (source / 'init.ply').write_text('ply\nelement vertex 1\nend_header\n')
            dataset, whole = pipeline.prepare(source, root / 'whole', 60)
            _, cut = pipeline.prepare(source, root / 'cut', 12)
            self.assertEqual(whole['selection'], 'whole-run')
            self.assertEqual(whole['drive_frames'], 6)
            self.assertLess(cut['drive_frames'], 6)
            self.assertEqual(len(whole['normalized_short_jpegs']), 6)
            self.assertEqual(whole['rejected'], [])
            selected = json.loads((dataset / 'transforms.json').read_text())
            for frame in selected['frames']:
                image = dataset / frame['file_path']
                self.assertEqual(image.suffix, '.png')
                self.assertGreaterEqual(image.stat().st_size, 20000)
                with Image.open(image) as decoded:
                    decoded.load()
                    with Image.open(source / image.with_suffix('.jpg').relative_to(dataset)) as original:
                        self.assertEqual(decoded.getpixel((0, 0)), original.getpixel((0, 0)))
                self.assertTrue((root / 'whole/decoded/images' / image.with_suffix('.jpg').name).exists())

    def test_xr_start_pose_transforms_camera_with_same_flip_as_scene(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp)
            matrix = [[-1, 0, 0, 1], [0, -1, 0, 2], [0, 0, 1, 3], [0, 0, 0, 1]]
            (dataset / 'transforms.json').write_text(json.dumps(dict(frames=[
                dict(file_path='images/000017.jpg', frame_id=17, transform_matrix=matrix)])))
            pose = pipeline.start_pose(dataset)
            self.assertEqual(pose['frame_id'], 17)
            self.assertEqual(pose['camera_position'], [1, -2, -3])
            self.assertEqual(pose['floor_position'], [1, -3.6, -3])
            self.assertAlmostEqual(abs(pose['yaw_degrees']), 180)
            self.assertEqual(pose['flip'], 1)
            self.assertTrue(pose['desktop_uses_orbit'])

    def test_armed_watcher_ignores_archive_backlog_before_since_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = argparse.Namespace(host='jetson', work_dir=root, poll_seconds=30,
                                      since='20261011T122834')
            old, new = 'run-20261011T094259', '20261011T130000.000Z-test'
            signatures = {old: [1, 2, 3, 4], new: [1, 2, 3, 4]}
            with patch.object(pipeline, 'inventory', return_value=signatures), \
                 patch.object(pipeline, 'finished', return_value=({}, [old, new])), \
                 patch.object(pipeline, 'pull', return_value=root / new) as pull, \
                 patch.object(pipeline, 'process'), \
                 patch.object(pipeline.time, 'sleep', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    pipeline.watch(args)
            pull.assert_called_once_with(args, new)

    def test_atomic_publication_and_single_process_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.ply'
            source.write_bytes(b'new complete ply')
            target = root / 'dist/scenes/run.ply'
            target.parent.mkdir(parents=True)
            target.write_bytes(b'old complete ply')
            replace = pipeline.os.replace
            def swap(temporary, final):
                self.assertEqual(final.read_bytes(), b'old complete ply')
                self.assertEqual(temporary.read_bytes(), b'new complete ply')
                replace(temporary, final)
            with patch.object(pipeline.os, 'replace', side_effect=swap):
                pipeline.publish(source, root / 'dist', 'run')
            self.assertEqual(target.read_bytes(), b'new complete ply')
            self.assertFalse(target.with_suffix('.ply.tmp').exists())
            with pipeline.exclusive(root / 'work'):
                with self.assertRaisesRegex(RuntimeError, 'Another after_run'):
                    with pipeline.exclusive(root / 'work'):
                        self.fail('overlapping pipeline acquired the lock')


if __name__ == '__main__':
    unittest.main()
