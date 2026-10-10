"""Split ffmpeg fragmented MP4 the same way jetson/video.py does."""

import struct


def codec_from_init(data):
    offset = data.find(b"avcC")
    if offset < 0 or len(data) < offset + 8:
        raise ValueError("Encoder initialization is missing H.264 codec metadata")
    return "avc1." + data[offset + 5:offset + 8].hex().upper()


def fragment_timing(data, fps):
    """Single video track, forced one-million-tick timescale (jetson/video.py).

    tfdt and trun live inside moof, which precedes mdat, so only the head of
    the fragment is scanned — mdat payloads can be megabytes.
    """
    offset = data.find(b"tfdt", 0, 65536)
    samples = data.find(b"trun", 0, 65536)
    if offset < 0 or samples < 0:
        raise ValueError("Fragment missing sample timing")
    version = data[offset + 4]
    start = struct.unpack_from(">Q" if version == 1 else ">I", data, offset + 8)[0] / 1_000_000
    count = struct.unpack_from(">I", data, samples + 8)[0]
    first = round(start * fps)
    return first, count, start, (first + count) / fps


def is_keyframe(data):
    """A fragment whose mdat carries an IDR picture, so a viewer can start decoding on it.

    ffmpeg writes MP4 samples as AVCC: 4-byte big-endian NAL lengths, not Annex B
    start codes, and default-base-is-moof says nothing about sync samples.
    (Ported from quest-hud's _is_keyframe on aziz/mac-dense-render.)
    """
    at = data.find(b"mdat")
    if at < 4:
        return False
    size = struct.unpack_from(">I", data, at - 4)[0]
    if size == 1:  # 64-bit largesize between kind and payload
        if at + 12 > len(data):
            return False
        size = struct.unpack_from(">Q", data, at + 4)[0]
        end = min(len(data), at - 4 + size)
        pos = at + 12
    else:
        end = min(len(data), at - 4 + size)
        pos = at + 4
    while pos + 4 <= end:
        length = struct.unpack_from(">I", data, pos)[0]
        pos += 4
        if length == 0 or pos + length > end:
            return False
        if data[pos] & 0x1F == 5:
            return True
        pos += length
    return False


class Mp4Reader:
    """Collects ftyp+moov as init, then each moof..mdat as one fragment."""

    def __init__(self):
        self.init = None
        self.mime = None
        self._init = bytearray()
        self._fragment = bytearray()
        self._pending = bytearray()

    def feed(self, chunk):
        """Yield ('init', bytes) once, then ('fragment', bytes) per mdat."""
        self._pending += chunk
        out = []
        pending = self._pending
        offset = 0
        while len(pending) - offset >= 8:
            size, kind = struct.unpack_from(">I4s", pending, offset)
            if size == 1:
                if len(pending) - offset < 16:
                    break
                size = struct.unpack_from(">Q", pending, offset + 8)[0]
            if not 8 <= size <= 4 * 1024 * 1024:
                raise ValueError("Invalid or excessive MP4 box from encoder")
            if len(pending) - offset < size:
                break
            view = memoryview(pending)[offset:offset + size]
            offset += size
            if self.init is None:
                self._init += view
                if kind == b"moov":
                    self.init = bytes(self._init)
                    self.mime = f'video/mp4; codecs="{codec_from_init(self.init)}"'
                    out.append(("init", self.init))
            else:
                self._fragment += view
                if kind == b"mdat":
                    out.append(("fragment", bytes(self._fragment)))
                    self._fragment.clear()
            view.release()
        if offset:
            del pending[:offset]
        return out
