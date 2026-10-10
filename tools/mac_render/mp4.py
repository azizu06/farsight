"""Split ffmpeg fragmented MP4 the same way jetson/video.py does."""

import struct


def codec_from_init(data):
    offset = data.find(b"avcC")
    if offset < 0 or len(data) < offset + 8:
        raise ValueError("Encoder initialization is missing H.264 codec metadata")
    return "avc1." + data[offset + 5:offset + 8].hex().upper()


def fragment_timing(data, fps):
    """Single video track, forced one-million-tick timescale (jetson/video.py)."""
    offset = data.find(b"tfdt")
    samples = data.find(b"trun")
    if offset < 0 or samples < 0:
        raise ValueError("Fragment missing sample timing")
    version = data[offset + 4]
    start = struct.unpack_from(">Q" if version == 1 else ">I", data, offset + 8)[0] / 1_000_000
    count = struct.unpack_from(">I", data, samples + 8)[0]
    first = round(start * fps)
    return first, count, start, (first + count) / fps


class Mp4Reader:
    """Collects ftyp+moov as init, then each moof..mdat as one fragment."""

    def __init__(self):
        self.init = None
        self.mime = None
        self._init = bytearray()
        self._fragment = bytearray()
        self._pending = b""

    def feed(self, chunk):
        """Yield ('init', bytes) once, then ('fragment', bytes) per mdat."""
        self._pending += chunk
        out = []
        while len(self._pending) >= 8:
            size, kind = struct.unpack(">I4s", self._pending[:8])
            if size == 1:
                if len(self._pending) < 16:
                    break
                size = struct.unpack(">Q", self._pending[8:16])[0]
            if not 8 <= size <= 4 * 1024 * 1024 or size < 8:
                raise ValueError("Invalid or excessive MP4 box from encoder")
            if len(self._pending) < size:
                break
            box, self._pending = self._pending[:size], self._pending[size:]
            if self.init is None:
                self._init.extend(box)
                if kind == b"moov":
                    self.init = bytes(self._init)
                    self.mime = f'video/mp4; codecs="{codec_from_init(self.init)}"'
                    out.append(("init", self.init))
            else:
                self._fragment.extend(box)
                if kind == b"mdat":
                    out.append(("fragment", bytes(self._fragment)))
                    self._fragment.clear()
        return out
