"""Privacy metadata stripping for public property photos (no dependencies).

Property photos are public, and phone cameras embed EXIF (GPS coordinates,
device serials, timestamps), XMP and IPTC blocks. This module removes those
blocks from JPEG, PNG and WebP uploads WITHOUT decoding or re-encoding the
image: pixel data is copied byte-for-byte, so quality, format and memory use
are unchanged (one linear pass, output never larger than the input plus a
26-byte orientation block).

Orientation is the one EXIF field that affects how an image is displayed.
When the source carries an EXIF Orientation other than 1 (normal), a minimal
replacement EXIF block containing ONLY that tag is written back, so photos
taken in portrait keep displaying upright. Everything else is dropped.

Fail closed: a structure this module cannot walk safely raises ValueError
(the upload is refused with 422) rather than publishing unstripped bytes.
Input has already passed storage.validate_image_content (magic bytes, size
and header dimensions).
"""
from __future__ import annotations

import struct

_EXIF_HEADER = b"Exif\x00\x00"

# JPEG markers dropped: APP1 (EXIF/XMP), APP2-APP15 except APP14 "Adobe"
# (colour transform, needed to decode some CMYK/YCCK files) and APP2
# ICC_PROFILE (colour accuracy; contains no personal data), plus COM.
_JPEG_STANDALONE = frozenset({0x01, *range(0xD0, 0xD8)})

# PNG ancillary chunks that may carry personal metadata. Critical chunks and
# rendering chunks (gAMA, cHRM, sRGB, iCCP, pHYs, ...) are kept.
_PNG_DROP = frozenset({b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"tIME"})

# WebP RIFF chunks dropped, and the VP8X flag bits that announce them.
_WEBP_DROP = frozenset({b"EXIF", b"XMP "})
_VP8X_EXIF_FLAG = 0x08
_VP8X_XMP_FLAG = 0x04


def _exif_orientation(tiff: bytes) -> int | None:
    """Orientation (1-8) from a TIFF/EXIF block's IFD0, or None.

    Bounds-checked on every read; anything malformed yields None (the tag is
    then simply not carried over).
    """
    if len(tiff) < 8:
        return None
    if tiff[:2] == b"II":
        endian = "<"
    elif tiff[:2] == b"MM":
        endian = ">"
    else:
        return None
    try:
        if struct.unpack(endian + "H", tiff[2:4])[0] != 42:
            return None
        ifd = struct.unpack(endian + "I", tiff[4:8])[0]
        if ifd + 2 > len(tiff):
            return None
        count = struct.unpack(endian + "H", tiff[ifd:ifd + 2])[0]
        for k in range(min(count, 512)):
            entry = ifd + 2 + 12 * k
            if entry + 12 > len(tiff):
                return None
            tag, typ, num = struct.unpack(endian + "HHI", tiff[entry:entry + 8])
            if tag == 0x0112 and typ == 3 and num == 1:
                value = struct.unpack(endian + "H", tiff[entry + 8:entry + 10])[0]
                return value if 1 <= value <= 8 else None
    except struct.error:
        return None
    return None


def _orientation_tiff(orientation: int) -> bytes:
    """Minimal big-endian TIFF block whose IFD0 holds only Orientation."""
    return (
        b"MM" + struct.pack(">HI", 42, 8)          # header, IFD0 at offset 8
        + struct.pack(">H", 1)                      # one entry
        + struct.pack(">HHIHH", 0x0112, 3, 1, orientation, 0)
        + struct.pack(">I", 0)                      # no next IFD
    )


# --------------------------------------------------------------------------
# JPEG
# --------------------------------------------------------------------------

def _keep_jpeg_segment(marker: int, payload: bytes) -> bool:
    if marker == 0xFE:                                   # COM
        return False
    if marker == 0xEE:                                   # APP14 (Adobe)
        return True
    if marker == 0xE2 and payload.startswith(b"ICC_PROFILE\x00"):
        return True
    if marker == 0xE0 and payload.startswith((b"JFIF\x00", b"JFXX\x00")):
        return True
    return not (0xE0 <= marker <= 0xEF)                  # other APPn dropped


def _strip_jpeg(data: bytes) -> bytes:
    if data[:2] != b"\xff\xd8":
        raise ValueError("Image could not be processed")
    out = [b"\xff\xd8"]
    orientation = None
    inserted = False
    i, n = 2, len(data)
    while True:
        if i >= n or data[i] != 0xFF:
            raise ValueError("Image could not be processed")
        while i < n and data[i] == 0xFF:
            i += 1
        if i >= n:
            raise ValueError("Image could not be processed")
        marker = data[i]
        i += 1
        if marker in _JPEG_STANDALONE:
            out.append(bytes((0xFF, marker)))
            continue
        if marker == 0xD9:                                # EOI before SOS
            raise ValueError("Image could not be processed")
        if i + 2 > n:
            raise ValueError("Image could not be processed")
        seg_len = int.from_bytes(data[i:i + 2], "big")
        if seg_len < 2 or i + seg_len > n:
            raise ValueError("Image could not be processed")
        payload = data[i + 2:i + seg_len]
        if marker == 0xE1 and payload.startswith(_EXIF_HEADER) and orientation is None:
            orientation = _exif_orientation(payload[len(_EXIF_HEADER):])
        is_app_or_com = 0xE0 <= marker <= 0xEF or marker == 0xFE
        if not is_app_or_com and not inserted:
            # First non-APP segment: emit the replacement EXIF (if any) here,
            # after JFIF/Adobe APPn, before tables/frame/scan data.
            inserted = True
            if orientation not in (None, 1):
                body = _EXIF_HEADER + _orientation_tiff(orientation)
                out.append(b"\xff\xe1" + (len(body) + 2).to_bytes(2, "big") + body)
        if not is_app_or_com or _keep_jpeg_segment(marker, payload):
            out.append(data[i - 2:i + seg_len])
        i += seg_len
        if marker == 0xDA:
            # SOS: the rest is scan data up to the primary image's EOI. In
            # entropy-coded data 0xFF is always followed by 0x00 (stuffing)
            # or RSTn, so the first FF D9 is the real EOI (also for
            # progressive files, whose later scans/tables precede it).
            # Anything AFTER it — MPF secondary images, depth/gain maps,
            # vendor trailers — may carry its own EXIF/GPS and is dropped.
            eoi = data.find(b"\xff\xd9", i)
            out.append(data[i:] if eoi < 0 else data[i:eoi + 2])
            return b"".join(out)


# --------------------------------------------------------------------------
# PNG
# --------------------------------------------------------------------------

def _strip_png(data: bytes) -> bytes:
    out = [data[:8]]
    i, n = 8, len(data)
    while i < n:
        if i + 12 > n:
            raise ValueError("Image could not be processed")
        length = int.from_bytes(data[i:i + 4], "big")
        ctype = data[i + 4:i + 8]
        end = i + 12 + length
        if end > n:
            raise ValueError("Image could not be processed")
        if ctype not in _PNG_DROP:
            out.append(data[i:end])                       # CRC unchanged
        i = end
        if ctype == b"IEND":
            return b"".join(out)
    raise ValueError("Image could not be processed")


# --------------------------------------------------------------------------
# WebP
# --------------------------------------------------------------------------

def _strip_webp(data: bytes) -> bytes:
    n = len(data)
    if n < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("Image could not be processed")
    riff_end = min(n, 8 + int.from_bytes(data[4:8], "little"))
    chunks: list[tuple[bytes, bytes]] = []
    orientation = None
    i = 12
    while i + 8 <= riff_end:
        ctype = data[i:i + 4]
        size = int.from_bytes(data[i + 4:i + 8], "little")
        end = i + 8 + size
        if end > riff_end:
            raise ValueError("Image could not be processed")
        payload = data[i + 8:end]
        if ctype == b"EXIF" and orientation is None:
            tiff = payload[len(_EXIF_HEADER):] if payload.startswith(_EXIF_HEADER) else payload
            orientation = _exif_orientation(tiff)
        chunks.append((ctype, payload))
        i = end + (size & 1)                               # chunks are padded to even
    if not chunks:
        raise ValueError("Image could not be processed")
    if chunks[0][0] != b"VP8X":
        # Simple-format WebP (VP8 / VP8L alone) cannot carry metadata.
        return data
    kept = [(t, p) for t, p in chunks if t not in _WEBP_DROP]
    vp8x = bytearray(kept[0][1])
    if len(vp8x) < 10:
        raise ValueError("Image could not be processed")
    vp8x[0] &= ~(_VP8X_EXIF_FLAG | _VP8X_XMP_FLAG) & 0xFF
    kept[0] = (b"VP8X", bytes(vp8x))
    if orientation not in (None, 1):
        vp8x[0] |= _VP8X_EXIF_FLAG
        kept[0] = (b"VP8X", bytes(vp8x))
        kept.append((b"EXIF", _orientation_tiff(orientation)))
    body = b"".join(
        t + len(p).to_bytes(4, "little") + p + (b"\x00" if len(p) & 1 else b"")
        for t, p in kept
    )
    return b"RIFF" + (4 + len(body)).to_bytes(4, "little") + b"WEBP" + body


def strip_metadata(data: bytes, mime: str) -> bytes:
    """Return ``data`` without EXIF/XMP/IPTC/text metadata (orientation kept).

    ``mime`` is the already-validated, content-sniffed type. Raises
    ValueError when the structure cannot be walked safely.
    """
    if mime == "image/jpeg":
        return _strip_jpeg(data)
    if mime == "image/png":
        return _strip_png(data)
    if mime == "image/webp":
        return _strip_webp(data)
    raise ValueError("Unsupported image type")
