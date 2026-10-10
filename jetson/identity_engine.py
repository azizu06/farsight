"""Opt-in, local face identity: aligned embeddings and a bounded enrollment gallery.

A similarity is a cosine score, not a probability. Unknown and ambiguous faces
are deliberately left unnamed. This module never authorizes rover movement.
"""
from __future__ import annotations

import json
import os
import struct
import tempfile
import threading
import unicodedata
import uuid
from pathlib import Path

import cv2
import numpy as np


class IdentityError(ValueError):
    """An image, embedding, model or gallery cannot be safely used."""


_MAX_IMAGE_BYTES = 4 * 1024 * 1024
_MAX_IMAGE_SIDE = 4096
_MAX_IMAGE_PIXELS = 12_000_000


def _embedding(value) -> np.ndarray:
    try:
        result = np.asarray(value, dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise IdentityError("Invalid face embedding") from exc
    if result.size != 128 or result.ndim not in (1, 2):
        raise IdentityError("Face embedding must contain 128 values")
    result = result.reshape(128)
    norm = float(np.linalg.norm(result))
    if not np.isfinite(result).all() or not np.isfinite(norm) or norm < 1e-8:
        raise IdentityError("Face embedding must be finite and nonzero")
    return result / norm


def _name(value) -> str:
    if not isinstance(value, str):
        raise IdentityError("Enter a name")
    result = value.strip()
    if not result or len(result) > 48 or any(unicodedata.category(c).startswith("C") for c in result):
        raise IdentityError("Name must contain 1–48 characters without control characters")
    return result


def _image_dimensions(data: bytes) -> tuple[int, int]:
    """Check encoded dimensions before a potentially large decoder allocation."""
    if not isinstance(data, bytes) or not data or len(data) > _MAX_IMAGE_BYTES:
        raise IdentityError("Image must be a JPEG or PNG of at most 4 MiB")
    width = height = 0
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 33 or data[8:16] != b"\0\0\0\rIHDR":
            raise IdentityError("Invalid PNG header")
        width, height = struct.unpack(">II", data[16:24])
    elif data.startswith(b"\xff\xd8"):
        cursor = 2
        while cursor < len(data):
            if data[cursor] != 0xff:
                raise IdentityError("Invalid JPEG header")
            while cursor < len(data) and data[cursor] == 0xff:
                cursor += 1
            if cursor >= len(data):
                break
            marker = data[cursor]
            cursor += 1
            if marker in (0xd9, 0xda):
                break
            if marker in (0x00, 0xd8) or 0xd0 <= marker <= 0xd7:
                raise IdentityError("Invalid JPEG marker")
            if cursor + 2 > len(data):
                raise IdentityError("Truncated JPEG header")
            length = int.from_bytes(data[cursor:cursor+2], "big")
            if length < 2 or cursor + length > len(data):
                raise IdentityError("Truncated JPEG header")
            if marker in (0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf):
                if length < 8 or width or height:
                    raise IdentityError("Invalid JPEG dimensions")
                height, width = struct.unpack(">HH", data[cursor+3:cursor+7])
            cursor += length
    else:
        raise IdentityError("Image must be JPEG or PNG")
    if (width < 64 or height < 64 or width > _MAX_IMAGE_SIDE or height > _MAX_IMAGE_SIDE
            or width * height > _MAX_IMAGE_PIXELS):
        raise IdentityError("Image dimensions are outside supported limits")
    return width, height


class IdentityEngine:
    """CPU YuNet detection + SFace alignment/recognition, serialized per engine."""

    def __init__(self, detector_path, recognizer_path):
        self._lock = threading.Lock()
        try:
            self.detector = cv2.FaceDetectorYN.create(
                str(detector_path), "", (640, 480), .9, .3, 5000,
                cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU)
            self.recognizer = cv2.FaceRecognizerSF.create(
                str(recognizer_path), "", cv2.dnn.DNN_BACKEND_OPENCV,
                cv2.dnn.DNN_TARGET_CPU)
        except (cv2.error, AttributeError) as exc:
            raise IdentityError("Cannot load YuNet/SFace models with this OpenCV build") from exc

    def _faces(self, data: bytes, *, enrollment: bool) -> list[dict]:
        dimensions = _image_dimensions(data)
        try:
            image = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        except cv2.error as exc:
            raise IdentityError("Cannot decode face image") from exc
        if image is None or (image.shape[1], image.shape[0]) != dimensions:
            raise IdentityError("Cannot decode face image or dimensions differ from its header")
        original_width, original_height = dimensions
        maximum_side = 1024 if enrollment else 640
        scale = min(1., maximum_side / max(original_width, original_height))
        if scale < 1:
            image = cv2.resize(image, (round(original_width * scale), round(original_height * scale)),
                               interpolation=cv2.INTER_AREA)
        height, width = image.shape[:2]
        # OpenCV's detector input size and recognizer are mutable; enrollment and
        # live inference must use the same lock, not independent model calls.
        with self._lock:
            try:
                self.detector.setInputSize((width, height))
                _, raw = self.detector.detect(image)
                rows = [] if raw is None else np.asarray(raw)
                if enrollment and len(rows) != 1:
                    raise IdentityError("Enrollment requires exactly one visible face")
                result = []
                recognition_attempts = 0
                for row in rows:
                    if np.asarray(row).shape != (15,) or not np.isfinite(row).all():
                        continue
                    x, y, box_width, box_height = (float(v) for v in row[:4])
                    score = float(row[14])
                    if (score < .9 or score > 1 or x < 0 or y < 0
                            or box_width < 36 or box_height < 36
                            or x + box_width > width or y + box_height > height
                            or box_width / width * original_width < 60
                            or box_height / height * original_height < 60):
                        continue
                    landmarks = row[4:14].reshape(5, 2)
                    if ((landmarks[:, 0] < 0).any() or (landmarks[:, 0] >= width).any()
                            or (landmarks[:, 1] < 0).any() or (landmarks[:, 1] >= height).any()):
                        continue
                    if recognition_attempts >= 8:
                        break
                    recognition_attempts += 1
                    aligned = self.recognizer.alignCrop(image, row)
                    try:
                        embedding = _embedding(self.recognizer.feature(aligned))
                    except IdentityError:
                        continue
                    result.append(dict(x=x/width, y=y/height, w=box_width/width,
                                       h=box_height/height, conf=score, embedding=embedding))
            except cv2.error as exc:
                raise IdentityError("Face model inference failed") from exc
        if enrollment and len(result) != 1:
            raise IdentityError("Use one clear, front-facing face of at least 60 pixels")
        return result

    def infer(self, jpeg: bytes) -> list[dict]:
        return self._faces(jpeg, enrollment=False)

    def enroll_embedding(self, jpeg: bytes) -> np.ndarray:
        return self._faces(jpeg, enrollment=True)[0]["embedding"]


class Gallery:
    """Small, optional persistent collection of self-enrolled face embeddings."""

    def __init__(self, path=None, max_people=20, max_samples=5, threshold=.5, margin=.08):
        if (not isinstance(max_people, int) or not 1 <= max_people <= 100
                or not isinstance(max_samples, int) or not 1 <= max_samples <= 20
                or not np.isfinite(threshold) or not 0 <= threshold <= 1
                or not np.isfinite(margin) or not 0 <= margin <= 1):
            raise IdentityError("Invalid gallery limits")
        self.path = None if path is None else Path(path)
        self.max_people, self.max_samples = max_people, max_samples
        self.threshold, self.margin = float(threshold), float(margin)
        self._lock = threading.RLock()
        self._people = {}
        self._generation = 0
        if self.path is not None and self.path.exists():
            self._load()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @staticmethod
    def _public(person) -> dict:
        return dict(id=person["id"], name=person["name"], samples=len(person["embeddings"]))

    def people(self) -> list[dict]:
        with self._lock:
            return [self._public(person) for person in self._people.values()]

    def enroll(self, name, embedding, person_id=None) -> dict:
        name, embedding = _name(name), _embedding(embedding)
        with self._lock:
            updated = dict(self._people)
            if person_id is None:
                if len(updated) >= self.max_people:
                    raise IdentityError("Enrollment gallery is full")
                person_id = str(uuid.uuid4())
                person = dict(id=person_id, name=name, embeddings=[])
            else:
                if person_id not in updated:
                    raise IdentityError("Person does not exist")
                person = dict(updated[person_id])
                person["embeddings"] = list(person["embeddings"])
                if max(float(np.dot(embedding, sample)) for sample in person["embeddings"]) < self.threshold:
                    raise IdentityError("New sample does not match this person's enrolled face")
                person["name"] = name
            if len(person["embeddings"]) >= self.max_samples:
                raise IdentityError("This person already has the maximum number of samples")
            person["embeddings"].append(embedding.copy())
            updated[person_id] = person
            self._persist(updated)
            self._people = updated
            self._generation += 1
            return self._public(person)

    def remove(self, person_id) -> bool:
        with self._lock:
            if person_id not in self._people:
                return False
            updated = dict(self._people)
            del updated[person_id]
            self._persist(updated)
            self._people = updated
            self._generation += 1
            return True

    def match(self, embedding) -> dict | None:
        query = _embedding(embedding)
        with self._lock:
            ranked = sorted(((max(float(np.dot(query, sample)) for sample in person["embeddings"]), person)
                             for person in self._people.values()), key=lambda value: value[0], reverse=True)
            if not ranked or ranked[0][0] < self.threshold:
                return None
            score, person = ranked[0]
            if len(ranked) > 1 and score - ranked[1][0] < self.margin:
                return None
            return dict(person_id=person["id"], name=person["name"],
                        similarity=min(1., max(-1., score)))

    def _load(self):
        try:
            with self.path.open("rb") as source:
                raw = source.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise IdentityError("Enrollment gallery is too large")
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("people"), list):
                raise IdentityError("Invalid enrollment gallery format")
            people = data["people"]
            if len(people) > self.max_people:
                raise IdentityError("Enrollment gallery exceeds person limit")
            result = {}
            for item in people:
                person_id = item["id"]
                if not isinstance(person_id, str) or str(uuid.UUID(person_id)) != person_id or person_id in result:
                    raise IdentityError("Invalid enrollment person ID")
                samples = item["embeddings"]
                if not isinstance(samples, list) or not 1 <= len(samples) <= self.max_samples:
                    raise IdentityError("Invalid enrollment sample count")
                result[person_id] = dict(id=person_id, name=_name(item["name"]),
                                         embeddings=[_embedding(sample) for sample in samples])
            self._people = result
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise IdentityError("Cannot load enrollment gallery; recognition remains disabled") from exc

    def _persist(self, people):
        if self.path is None:
            return
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "people": [dict(id=p["id"], name=p["name"],
                       embeddings=[sample.tolist() for sample in p["embeddings"]]) for p in people.values()]}
            descriptor, temporary = tempfile.mkstemp(prefix=".identity-", suffix=".json", dir=self.path.parent)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except (OSError, TypeError, ValueError) as exc:
            raise IdentityError("Cannot save enrollment gallery") from exc
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
