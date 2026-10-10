"""Identity admission, ambiguous matches and bounded image decoding."""
import base64
import json
import math
import struct
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from jetson.identity_engine import Gallery, IdentityEngine, IdentityError


def vector(index=0, cosine=None):
    result = np.zeros(128, np.float32)
    if cosine is None:
        result[index] = 1
    else:
        result[0] = cosine
        result[1] = math.sqrt(1 - cosine ** 2)
    return result


class GalleryTests(unittest.TestCase):
    def test_unknown_and_clear_match_do_not_claim_probability(self):
        gallery = Gallery()
        self.assertIsNone(gallery.match(vector()))
        person = gallery.enroll("  Mehdi  ", vector() * 4)
        self.assertEqual(person["name"], "Mehdi")
        self.assertEqual(person["samples"], 1)
        match = gallery.match(vector())
        self.assertEqual(match["person_id"], person["id"])
        self.assertEqual(match["name"], "Mehdi")
        self.assertAlmostEqual(match["similarity"], 1)
        self.assertIsNone(gallery.match(vector(2)))
        self.assertNotIn("confidence", match)

    def test_two_similar_people_are_unknown_instead_of_wrong_name(self):
        gallery = Gallery()
        gallery.enroll("One", vector())
        gallery.enroll("Two", vector(cosine=.98))
        self.assertIsNone(gallery.match(vector()))

    def test_samples_and_delete_invalidate_generation(self):
        gallery = Gallery(max_samples=2)
        person = gallery.enroll("One", vector())
        self.assertEqual(gallery.generation, 1)
        again = gallery.enroll("One", vector(cosine=.95), person["id"])
        self.assertEqual(again["samples"], 2)
        self.assertEqual(gallery.generation, 2)
        with self.assertRaises(IdentityError):
            gallery.enroll("One", vector(), person["id"])
        self.assertEqual(gallery.generation, 2)
        self.assertTrue(gallery.remove(person["id"]))
        self.assertIsNone(gallery.match(vector()))
        self.assertEqual(gallery.generation, 3)
        self.assertFalse(gallery.remove(person["id"]))
        self.assertEqual(gallery.generation, 3)

    def test_rejects_invalid_name_vectors_and_capacity_without_mutating(self):
        gallery = Gallery(max_people=1)
        for name, embedding in [("", vector()), ("x" * 49, vector()),
                                ("x\nnext", vector()), ("x", np.zeros(128)),
                                ("x", np.ones(127)), ("x", np.full(128, np.nan))]:
            with self.subTest(name=name):
                with self.assertRaises(IdentityError):
                    gallery.enroll(name, embedding)
        self.assertEqual(gallery.people(), [])
        gallery.enroll("One", vector())
        with self.assertRaises(IdentityError):
            gallery.enroll("Two", vector(1))
        self.assertEqual(len(gallery.people()), 1)
        with self.assertRaises(IdentityError):
            gallery.enroll("One", vector(), "unknown-id")

    def test_existing_person_rejects_an_inconsistent_new_face(self):
        gallery = Gallery()
        person = gallery.enroll("One", vector())
        with self.assertRaises(IdentityError):
            gallery.enroll("One", vector(1), person["id"])
        self.assertEqual(gallery.people()[0]["samples"], 1)
        self.assertEqual(gallery.generation, 1)

    def test_public_people_cannot_mutate_gallery(self):
        gallery = Gallery()
        person = gallery.enroll("One", vector())
        people = gallery.people()
        people[0]["name"] = "Changed"
        self.assertEqual(gallery.match(vector())["name"], "One")
        self.assertNotIn("embeddings", person)

    def test_persists_only_embeddings_and_removal_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gallery.json"
            gallery = Gallery(path)
            person = gallery.enroll("One", vector())
            loaded = Gallery(path)
            self.assertEqual(loaded.match(vector())["person_id"], person["id"])
            data = json.loads(path.read_text())
            self.assertNotIn("photo", str(data))
            self.assertEqual(len(data["people"][0]["embeddings"][0]), 128)
            loaded.remove(person["id"])
            self.assertEqual(Gallery(path).people(), [])

    def test_invalid_persistence_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gallery.json"
            for contents in ["not json", '{"version":99,"people":[]}',
                             '{"version":1,"people":[{"id":"one","name":"One","embeddings":[[0]]}]}']:
                path.write_text(contents)
                with self.assertRaises(IdentityError):
                    Gallery(path)

    def test_disk_write_failure_does_not_publish_a_partial_enrollment(self):
        with tempfile.TemporaryDirectory() as directory:
            gallery = Gallery(Path(directory) / "gallery.json")
            with patch("os.replace", side_effect=OSError("disk error")):
                with self.assertRaises(IdentityError):
                    gallery.enroll("One", vector())
            self.assertEqual(gallery.people(), [])
            self.assertEqual(gallery.generation, 0)


    def test_readers_return_committed_state_while_persistence_is_blocked(self):
        for deleting in (False, True):
            with self.subTest(deleting=deleting), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "gallery.json"
                gallery = Gallery(path)
                old_person = gallery.enroll("One", vector()) if deleting else None
                entered, release = threading.Event(), threading.Event()
                persist = gallery._persist

                def blocked(candidate):
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError("persistence test did not release writer")
                    persist(candidate)

                with ThreadPoolExecutor(max_workers=2) as pool, patch.object(gallery, "_persist", blocked):
                    change = pool.submit(gallery.remove, old_person["id"]) if deleting else pool.submit(
                        gallery.enroll, "One", vector())
                    try:
                        self.assertTrue(entered.wait(1))
                        read = pool.submit(lambda: (gallery.people(), gallery.generation, gallery.match(vector())))
                        try:
                            people, generation, match = read.result(timeout=.2)
                        except TimeoutError:
                            self.fail("gallery reads blocked behind pending persistence")
                        self.assertEqual(len(people), 1 if deleting else 0)
                        self.assertEqual(generation, 1 if deleting else 0)
                        self.assertEqual(match["name"] if match else None, "One" if deleting else None)
                    finally:
                        release.set()
                    change.result(timeout=1)
                self.assertEqual(len(gallery.people()), 0 if deleting else 1)
                self.assertEqual(len(Gallery(path).people()), 0 if deleting else 1)

    def test_two_concurrent_enrollments_keep_both_committed_people(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gallery.json"
            gallery = Gallery(path)
            entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
            persist = gallery._persist

            def blocked(candidate):
                if any(person["name"] == "One" for person in candidate.values()) and not entered.is_set():
                    entered.set()
                    if not release.wait(2):
                        raise AssertionError("persistence test did not release writer")
                persist(candidate)

            def second_enrollment():
                second_started.set()
                return gallery.enroll("Two", vector(1))

            with ThreadPoolExecutor(max_workers=2) as pool, patch.object(gallery, "_persist", blocked):
                first = pool.submit(gallery.enroll, "One", vector())
                try:
                    self.assertTrue(entered.wait(1))
                    second = pool.submit(second_enrollment)
                    self.assertTrue(second_started.wait(1))
                finally:
                    release.set()
                first.result(timeout=1)
                second.result(timeout=1)
            self.assertEqual({person["name"] for person in gallery.people()}, {"One", "Two"})
            self.assertEqual(gallery.generation, 2)
            self.assertEqual({person["name"] for person in Gallery(path).people()}, {"One", "Two"})


class FakeDetector:
    def __init__(self, rows):
        self.rows = rows
        self.dimensions = None

    def setInputSize(self, dimensions):
        self.dimensions = dimensions

    def detect(self, image):
        return None, np.array(self.rows, np.float32).reshape(-1, 15)


class FakeRecognizer:
    def __init__(self, embedding=None):
        self.embedding = vector() if embedding is None else embedding

    def alignCrop(self, image, row):
        return image[0:112, 0:112]

    def feature(self, image):
        return self.embedding.reshape(1, -1)


def face(x=30, y=30, width=100, height=100, confidence=.95):
    return [x, y, width, height, x+30, y+35, x+70, y+35,
            x+50, y+55, x+35, y+80, x+65, y+80, confidence]


class EngineTests(unittest.TestCase):
    def engine(self, rows, embedding=None):
        # Model downloads are external; substitute only OpenCV's two DNN factories.
        with patch.object(cv2.FaceDetectorYN, "create", return_value=FakeDetector(rows)), \
             patch.object(cv2.FaceRecognizerSF, "create", return_value=FakeRecognizer(embedding)):
            return IdentityEngine("test-detector.onnx", "test-recognizer.onnx")

    def image(self, width=640, height=480):
        return cv2.imencode(".jpg", np.zeros((height, width, 3), np.uint8))[1].tobytes()

    def test_clear_face_has_normalized_coordinates_and_embedding(self):
        engine = self.engine([face(x=64, y=48, width=128, height=96)])
        result = engine.infer(self.image())
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["x"], .1)
        self.assertAlmostEqual(result[0]["y"], .1)
        self.assertAlmostEqual(result[0]["w"], .2)
        self.assertAlmostEqual(result[0]["h"], .2)
        np.testing.assert_allclose(result[0]["embedding"], vector())

    def test_enrollment_requires_one_raw_face_even_when_second_is_tiny(self):
        image = self.image()
        for rows in [[], [face(), face(x=200, width=10, height=10)]]:
            with self.subTest(count=len(rows)):
                engine = self.engine(rows)
                with self.assertRaises(IdentityError):
                    engine.enroll_embedding(image)
        np.testing.assert_allclose(self.engine([face()]).enroll_embedding(image), vector())

    def test_live_rejects_small_invalid_or_nonfinite_faces(self):
        engine = self.engine([face(width=10), face(x=float("nan")), face(confidence=.2)])
        self.assertEqual(engine.infer(self.image()), [])
        with self.assertRaises(IdentityError):
            self.engine([face()], np.full(128, np.nan)).enroll_embedding(self.image())

    def test_huge_or_nonimage_payload_is_rejected_before_decoder(self):
        fake_png = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", 50000, 50000) + b"\x08\x02\x00\x00\x00" + b"\0"*4
        engine = self.engine([])
        for data in [fake_png, b"not an image", b"\0" * (4*1024*1024 + 1)]:
            with self.subTest(size=len(data)):
                with patch.object(cv2, "imdecode", side_effect=AssertionError("unsafe decode")):
                    with self.assertRaises(IdentityError):
                        engine.infer(data)

    def test_live_faces_have_bounded_recognition_work(self):
        engine = self.engine([face(x=20+i, y=20) for i in range(20)])
        self.assertEqual(len(engine.infer(self.image())), 8)

    def test_live_thumbnail_is_a_small_aligned_jpeg(self):
        result = self.engine([face()]).infer(self.image())[0]
        self.assertIn("thumbnail", result)
        jpeg = base64.b64decode(result["thumbnail"], validate=True)
        self.assertLessEqual(len(jpeg), 8192)
        crop = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(crop.shape, (112, 112, 3))
        np.testing.assert_allclose(result["embedding"], vector())

    def test_only_first_two_live_faces_carry_thumbnails(self):
        result = self.engine([face(x=20+i) for i in range(8)]).infer(self.image())
        self.assertTrue(all("thumbnail" in item for item in result[:2]))
        self.assertTrue(all("thumbnail" not in item for item in result[2:]))

    def test_thumbnail_failure_or_oversize_does_not_break_recognition(self):
        image = self.image()
        engine = self.engine([face()])
        with patch.object(cv2, "imencode", side_effect=cv2.error("encoder unavailable")):
            result = engine.infer(image)
        self.assertEqual(len(result), 1)
        self.assertNotIn("thumbnail", result[0])
        with patch.object(cv2, "imencode", return_value=(True, np.zeros(8193, np.uint8))):
            result = engine.infer(image)
        self.assertEqual(len(result), 1)
        self.assertNotIn("thumbnail", result[0])

    def test_enrollment_candidates_never_include_face_images(self):
        engine = self.engine([face()])
        candidate = engine._faces(self.image(), enrollment=True)[0]
        self.assertNotIn("thumbnail", candidate)
        gallery = Gallery()
        self.assertEqual(gallery.enroll("One", candidate["embedding"])["samples"], 1)

    def test_png_is_supported_and_truncated_jpeg_rejected(self):
        engine = self.engine([face()])
        png = cv2.imencode(".png", np.zeros((480, 640, 3), np.uint8))[1].tobytes()
        self.assertEqual(len(engine.infer(png)), 1)
        with self.assertRaises(IdentityError):
            engine.infer(b"\xff\xd8\xff\xe0\x00\x20short")

    def test_portrait_detector_area_is_bounded_by_its_longest_side(self):
        engine = self.engine([face(x=5, y=5, width=50, height=100)])
        result = engine.infer(self.image(width=480, height=3840))
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["w"], .625)
        self.assertAlmostEqual(result[0]["h"], .15625)

    def test_face_too_small_after_downsampling_is_not_named_or_enrolled(self):
        engine = self.engine([face(width=20, height=60)])
        image = self.image(width=3840, height=2880)
        self.assertEqual(engine.infer(image), [])
        with self.assertRaises(IdentityError):
            engine.enroll_embedding(image)

    def test_resized_live_preserves_original_coordinate_scale(self):
        engine = self.engine([face(x=64, y=48, width=128, height=96)])
        result = engine.infer(self.image(width=1280, height=960))
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0]["x"], .1)
        self.assertAlmostEqual(result[0]["w"], .2)


if __name__ == "__main__":
    unittest.main()
