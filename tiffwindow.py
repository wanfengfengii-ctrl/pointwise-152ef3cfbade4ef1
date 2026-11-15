"""Read a rectangular window of raw integer samples from a legacy TIFF.

The only public entry point is :func:`read`::

    from tiffwindow import read
    rows = read("elevation.tif", x, y, w, h)

``rows`` is a list of ``h`` lists of ``w`` integers.  No scaling, colour
mapping or inversion is performed: the stored sample values are returned.

Supported files
---------------
Classic TIFF (magic 42) only, one page, byte order II or MM, strips (not tiles),
one grayscale channel, unsigned 8- or 16-bit samples, Orientation = 1,
Compression = 5 (TIFF LZW) and Predictor 1 or 2.

The reader never materialises a whole uncompressed strip.  It streams the LZW
codes of the strips that intersect the requested window, keeps at most one
decoded row plus the LZW string table (4096 entries), and discards rows above
or below the window.  Strip payloads that do not intersect the window are
neither read nor validated.  Any malformed input raises ``ValueError``.
"""

from __future__ import annotations

import operator
import os
import struct
import sys
from array import array
from builtins import open as _open

__all__ = ["read"]

# TIFF field types and their element sizes (TIFF 6.0 section 2.2).
_TYPE_SIZE = {
    1: 1,   # BYTE
    2: 1,   # ASCII
    3: 2,   # SHORT
    4: 4,   # LONG
    5: 8,   # RATIONAL
    6: 1,   # SBYTE
    7: 1,   # UNDEFINED
    8: 2,   # SSHORT
    9: 4,   # SLONG
    10: 8,  # SRATIONAL
    11: 4,  # FLOAT
    12: 8,  # DOUBLE
}

_CLEAR_CODE = 256
_EOI_CODE = 257
_FIRST_FREE_CODE = 258
_CODE_TABLE_SIZE = 4096
_MAX_CODE_WIDTH = 12
_BITS_MIN = 9
_COMPRESSED_CHUNK = 4096
_NATIVE_LITTLE = sys.byteorder == "little"
_LITERALS = tuple(bytes((value,)) for value in range(256))


class _Tag:
    """One IFD entry that the reader may need."""

    __slots__ = ("tag", "typ", "count", "inline", "offset", "total", "size")

    def __init__(self, tag, typ, count, inline, offset, total, size):
        self.tag = tag
        self.typ = typ
        self.count = count
        self.inline = inline
        self.offset = offset
        self.total = total
        self.size = size


class _Reader:
    def __init__(self, fp, file_size):
        self.fp = fp
        self.file_size = file_size
        self.endian = None
        self.width = None
        self.length = None
        self.bits = None
        self.rows_per_strip = None
        self.nstrips = None
        self.tags = {}
        self._special_tags = set()

    # ------------------------------------------------------------------
    # TIFF structures
    # ------------------------------------------------------------------
    def _read_exact(self, n, what):
        data = self.fp.read(n)
        if len(data) != n:
            raise ValueError("truncated TIFF: %s" % what)
        return data

    def _u16(self, data):
        return struct.unpack(self.endian + "H", data)[0]

    def _u32(self, data):
        return struct.unpack(self.endian + "I", data)[0]

    def _parse_ifd(self):
        fp = self.fp
        fp.seek(0)
        header = self._read_exact(8, "file header")
        if header[:2] == b"II":
            self.endian = "<"
        elif header[:2] == b"MM":
            self.endian = ">"
        else:
            raise ValueError("not a TIFF file: bad byte-order marker")
        magic = self._u16(header[2:4])
        if magic != 42:
            if magic == 43:
                raise ValueError("BigTIFF is not supported")
            raise ValueError("not a classic TIFF file: bad magic number")
        ifd_offset = self._u32(header[4:8])
        if ifd_offset < 8 or ifd_offset + 2 > self.file_size:
            raise ValueError("TIFF IFD offset is outside the file")

        fp.seek(ifd_offset)
        nentries = self._u16(self._read_exact(2, "IFD entry count"))
        if nentries == 0:
            raise ValueError("TIFF has no IFD entries")
        entries_end = ifd_offset + 2 + nentries * 12
        if entries_end + 4 > self.file_size:
            raise ValueError("truncated TIFF IFD")

        # Stream the entries.  Only a handful of tags are retained, so a
        # pathological IFD cannot force a large allocation.
        for index in range(nentries):
            entry = self._read_exact(12, "IFD entry")
            tag, typ, count = struct.unpack(self.endian + "HHI", entry[:8])
            if tag in (322, 323, 330, 338):
                # TileWidth / TileLength / SubIFDs / ExtraSamples.
                self._special_tags.add(tag)
            if tag not in _SCALAR_TAGS and tag not in (273, 279):
                continue
            if tag in self.tags:
                raise ValueError("duplicate IFD tag %d" % tag)
            allowed_types = _SCALAR_TYPES.get(tag, (3, 4))
            if typ not in allowed_types:
                raise ValueError("IFD tag %d has unsupported field type" % tag)
            size = _TYPE_SIZE.get(typ)
            if size is None:
                raise ValueError("IFD tag %d has unknown field type" % tag)
            if count == 0:
                raise ValueError("IFD tag %d has a zero count" % tag)
            total = size * count
            if total <= 4:
                inline = entry[8:8 + total]
                offset = None
            else:
                inline = None
                offset = self._u32(entry[8:12])
                if offset + total > self.file_size:
                    raise ValueError("IFD tag %d points outside the file" % tag)
            self.tags[tag] = _Tag(tag, typ, count, inline, offset, total, size)

        fp.seek(entries_end)
        next_ifd = self._u32(self._read_exact(4, "next IFD offset"))
        if next_ifd != 0:
            raise ValueError("multi-page TIFF is not supported")

        if 322 in self._special_tags or 323 in self._special_tags:
            raise ValueError("tiled TIFF is not supported")
        if 330 in self._special_tags:
            raise ValueError("TIFF with SubIFDs is not supported")
        if 338 in self._special_tags:
            raise ValueError("TIFF with extra samples is not supported")

    def _scalar(self, tag, default=None):
        info = self.tags.get(tag)
        if info is None:
            return default
        if info.count != 1:
            raise ValueError("IFD tag %d must have exactly one value" % tag)
        raw = info.inline
        if raw is None:
            self.fp.seek(info.offset)
            raw = self._read_exact(info.size, "value of IFD tag %d" % tag)
        if info.typ == 3:
            return struct.unpack(self.endian + "H", raw)[0]
        if info.typ == 4:
            return struct.unpack(self.endian + "I", raw)[0]
        raise ValueError("IFD tag %d has an unsupported field type" % tag)

    def _array_element(self, tag, index):
        info = self.tags.get(tag)
        if info is None:
            raise ValueError("missing required IFD tag %d" % tag)
        if index < 0 or index >= info.count:
            raise ValueError("strip index is outside IFD tag %d" % tag)
        pos = index * info.size
        if info.inline is not None:
            raw = info.inline[pos:pos + info.size]
        else:
            self.fp.seek(info.offset + pos)
            raw = self._read_exact(info.size, "strip table entry")
        if info.typ == 3:
            return struct.unpack(self.endian + "H", raw)[0]
        if info.typ == 4:
            return struct.unpack(self.endian + "I", raw)[0]
        raise ValueError("IFD tag %d has an unsupported field type" % tag)

    # ------------------------------------------------------------------
    # Window extraction
    # ------------------------------------------------------------------
    def read_window(self, x, y, w, h):
        self._parse_ifd()

        width = self._scalar(256)
        length = self._scalar(257)
        if width is None or length is None:
            raise ValueError("TIFF is missing ImageWidth or ImageLength")
        if width <= 0 or length <= 0:
            raise ValueError("invalid TIFF image dimensions")
        self.width = width
        self.length = length

        bits = self._scalar(258)
        if bits not in (8, 16):
            raise ValueError("only unsigned 8-bit or 16-bit grayscale is supported")
        self.bits = bits

        compression = self._scalar(259, 1)
        if compression != 5:
            raise ValueError("only TIFF LZW compression (5) is supported")

        photometric = self._scalar(262, 1)
        if photometric not in (0, 1):
            raise ValueError("only single-channel grayscale photometric data is supported")

        orientation = self._scalar(274, 1)
        if orientation != 1:
            raise ValueError("only TIFF Orientation=1 is supported")

        spp = self._scalar(277, 1)
        if spp != 1:
            raise ValueError("only single-channel (SamplesPerPixel=1) images are supported")

        planar = self._scalar(284, 1)
        if planar != 1:
            raise ValueError("only PlanarConfiguration=1 is supported")

        predictor = self._scalar(317, 1)
        if predictor not in (1, 2):
            raise ValueError("only Predictor=1 or 2 is supported")

        fill_order = self._scalar(266, 1)
        if fill_order != 1:
            raise ValueError("only FillOrder=1 is supported")

        sample_format = self._scalar(339, 1)
        if sample_format != 1:
            raise ValueError("only unsigned integer samples are supported")

        if x + w > width or y + h > length:
            raise ValueError("window lies outside the image")

        rows_per_strip = self._scalar(278)
        if rows_per_strip is None:
            # TIFF default is 2**32-1, i.e. one strip for the whole image.
            rows_per_strip = length
        if rows_per_strip <= 0:
            raise ValueError("invalid RowsPerStrip")
        self.rows_per_strip = rows_per_strip
        self.nstrips = (length + rows_per_strip - 1) // rows_per_strip

        for tag, name in ((273, "StripOffsets"), (279, "StripByteCounts")):
            info = self.tags.get(tag)
            if info is None:
                raise ValueError("TIFF is missing %s" % name)
            if info.count != self.nstrips:
                raise ValueError("%s count does not match the number of strips" % name)

        if w <= 0 or h <= 0:
            raise ValueError("window width and height must be positive")

        sample_bytes = bits // 8
        row_bytes = width * sample_bytes
        result = [None] * h

        first_strip = y // rows_per_strip
        last_strip = (y + h - 1) // rows_per_strip

        for strip_index in range(first_strip, last_strip + 1):
            strip_y0 = strip_index * rows_per_strip
            strip_rows = min(rows_per_strip, length - strip_y0)
            offset = self._array_element(273, strip_index)
            byte_count = self._array_element(279, strip_index)
            if offset < 0 or byte_count <= 0:
                raise ValueError("invalid strip offset or byte count")
            if offset + byte_count > self.file_size:
                raise ValueError("strip %d extends past the end of the file" % strip_index)

            need_lo = max(0, y - strip_y0)
            need_hi = min(strip_rows, (y + h) - strip_y0)

            def row_handler(local_row, row_data, _y0=strip_y0):
                global_row = _y0 + local_row
                values = self._row_values(row_data, x, w, predictor)
                result[global_row - y] = values

            sink = _RowSink(row_bytes, strip_rows, need_lo, need_hi, row_handler)
            decoder = _LzwDecoder(
                self.fp,
                offset,
                byte_count,
                row_bytes * strip_rows,
                sink,
            )
            decoder.decode()

        if any(row is None for row in result):
            raise ValueError("internal error: incomplete window")
        return result

    def _row_values(self, row_data, x, w, predictor):
        bits = self.bits
        if bits == 8:
            if predictor == 1:
                return list(row_data[x:x + w])
            values = [0] * w
            prev = 0
            for pos in range(x + w):
                # Horizontal differencing is modulo 2**8.
                prev = (row_data[pos] + prev) & 0xFF
                if pos >= x:
                    values[pos - x] = prev
            return values
        # 16-bit samples: read the bytes as native 16-bit words, then swap
        # when the file byte order differs from the host.
        samples = array("H")
        if predictor == 1:
            samples.frombytes(bytes(row_data[2 * x:2 * (x + w)]))
        else:
            samples.frombytes(bytes(row_data[:2 * (x + w)]))
        if _NATIVE_LITTLE != (self.endian == "<"):
            samples.byteswap()
        if predictor == 1:
            return samples.tolist()
        values = [0] * w
        prev = 0
        for pos in range(x + w):
            # Horizontal differencing is modulo 2**16; the addition wraps.
            prev = (samples[pos] + prev) & 0xFFFF
            if pos >= x:
                values[pos - x] = prev
        return values


# Tags we retain in the IFD.  Type restrictions are per TIFF 6.0.
_SCALAR_TAGS = frozenset(
    (256, 257, 258, 259, 262, 266, 274, 277, 278, 284, 317, 339)
)
_SCALAR_TYPES = {
    256: (3, 4),  # ImageWidth
    257: (3, 4),  # ImageLength
    258: (3, 4),  # BitsPerSample (LONG is tolerated)
    259: (3, 4),  # Compression
    262: (3, 4),  # PhotometricInterpretation
    266: (3, 4),  # FillOrder
    274: (3, 4),  # Orientation
    277: (3, 4),  # SamplesPerPixel
    278: (3, 4),  # RowsPerStrip
    284: (3, 4),  # PlanarConfiguration
    317: (3, 4),  # Predictor
    339: (3, 4),  # SampleFormat
}


class _RowSink:
    """Consume decoded LZW bytes row by row without retaining the strip."""

    __slots__ = ("row_bytes", "rows", "need_lo", "need_hi", "handler",
                 "total", "row", "pos", "current")

    def __init__(self, row_bytes, rows, need_lo, need_hi, handler):
        self.row_bytes = row_bytes
        self.rows = rows
        self.need_lo = need_lo
        self.need_hi = need_hi
        self.handler = handler
        self.total = 0
        self.row = 0
        self.pos = 0
        self.current = None
        self._start_row()

    def _start_row(self):
        if self.need_lo <= self.row < self.need_hi:
            self.current = bytearray(self.row_bytes)
        else:
            self.current = None

    def write(self, data):
        if self.total + len(data) > self.row_bytes * self.rows:
            raise ValueError("corrupt LZW strip: decoded data exceeds the strip size")
        self.total += len(data)
        i = 0
        n = len(data)
        while i < n:
            take = min(self.row_bytes - self.pos, n - i)
            if self.current is not None:
                self.current[self.pos:self.pos + take] = data[i:i + take]
            self.pos += take
            i += take
            if self.pos == self.row_bytes:
                if self.current is not None:
                    self.handler(self.row, self.current)
                self.row += 1
                self.pos = 0
                if self.row < self.rows:
                    self._start_row()


class _BitReader:
    """MSB-first bit reader over a byte range, in 4 KiB input chunks."""

    __slots__ = ("fp", "pos", "end", "buf", "index", "acc", "nbits")

    def __init__(self, fp, offset, count):
        if offset < 0 or count <= 0:
            raise ValueError("invalid compressed strip range")
        self.fp = fp
        self.pos = offset
        self.end = offset + count
        self.buf = b""
        self.index = 0
        self.acc = 0
        self.nbits = 0
        fp.seek(offset)

    def _next_byte(self):
        while self.index >= len(self.buf):
            remaining = self.end - self.pos
            if remaining <= 0:
                return None
            take = min(_COMPRESSED_CHUNK, remaining)
            chunk = self.fp.read(take)
            if len(chunk) != take:
                raise ValueError("corrupt LZW strip: compressed data is truncated")
            self.pos += take
            self.buf = chunk
            self.index = 0
        value = self.buf[self.index]
        self.index += 1
        return value

    def read_code(self, width):
        while self.nbits < width:
            value = self._next_byte()
            if value is None:
                raise ValueError("corrupt LZW strip: stream ends before EOI code")
            self.acc = (self.acc << 8) | value
            self.nbits += 8
        shift = self.nbits - width
        code = (self.acc >> shift) & ((1 << width) - 1)
        self.nbits = shift
        if shift:
            self.acc &= (1 << shift) - 1
        else:
            self.acc = 0
        return code


class _LzwDecoder:
    """TIFF LZW depacker (MSB-first, early code-width change).

    Only the dictionary prefix/suffix arrays are kept, plus a transient buffer
    for the string currently being expanded (at most 4095 bytes).  Decoded
    bytes are streamed to ``sink`` immediately, so a strip is never buffered.
    """

    __slots__ = ("fp", "offset", "count", "expected_bytes", "sink")

    def __init__(self, fp, offset, count, expected_bytes, sink):
        self.fp = fp
        self.offset = offset
        self.count = count
        self.expected_bytes = expected_bytes
        self.sink = sink

    def decode(self):
        br = _BitReader(self.fp, self.offset, self.count)

        # Dictionary: prefix code (-1 for literals) and the last byte of each
        # string.  4096 entries, the only per-strip state kept besides one row.
        prefix = array("h", [-1]) * _CODE_TABLE_SIZE
        suffix = array("B", [0]) * _CODE_TABLE_SIZE
        for value in range(256):
            suffix[value] = value

        literals = _LITERALS
        next_code = _FIRST_FREE_CODE
        width = _BITS_MIN
        previous = -1
        seen_clear = False
        sink = self.sink
        max_width = _MAX_CODE_WIDTH

        while True:
            code = br.read_code(width)
            if code == _CLEAR_CODE:
                next_code = _FIRST_FREE_CODE
                width = _BITS_MIN
                previous = -1
                seen_clear = True
                continue
            if code == _EOI_CODE:
                break
            if not seen_clear:
                raise ValueError("corrupt LZW strip: data before initial CLEAR code")

            if code < 256:
                sink.write(literals[code])
                if previous >= 0:
                    if next_code >= _CODE_TABLE_SIZE:
                        raise ValueError(
                            "corrupt LZW strip: dictionary overflow without CLEAR"
                        )
                    prefix[next_code] = previous
                    suffix[next_code] = code
                    next_code += 1
                    if width < max_width and next_code >= (1 << width) - 1:
                        width += 1
                previous = code
                continue

            if previous < 0:
                raise ValueError(
                    "corrupt LZW strip: dictionary code directly after CLEAR"
                )

            if code < next_code:
                data = bytearray()
                cursor = code
                while cursor >= 0:
                    data.append(suffix[cursor])
                    cursor = prefix[cursor]
                data.reverse()
                first_char = data[0]
            elif code == next_code:
                # KwKwK: the code being defined is used in the same step.
                data = bytearray()
                cursor = previous
                while cursor >= 0:
                    data.append(suffix[cursor])
                    cursor = prefix[cursor]
                data.reverse()
                first_char = data[0]
                data.append(first_char)
            else:
                raise ValueError("corrupt LZW strip: code not in dictionary")

            sink.write(data)

            if next_code >= _CODE_TABLE_SIZE:
                raise ValueError("corrupt LZW strip: dictionary overflow without CLEAR")
            prefix[next_code] = previous
            suffix[next_code] = first_char
            next_code += 1
            # TIFF's "early change": the width grows one code earlier than the
            # encoder's table-size threshold, i.e. as soon as the next free
            # code reaches 511, 1023 or 2047.
            if width < max_width and next_code >= (1 << width) - 1:
                width += 1
            previous = code

        if not seen_clear:
            raise ValueError("corrupt LZW strip: no CLEAR code")
        if sink.total != self.expected_bytes:
            raise ValueError(
                "corrupt LZW strip: decoded %d bytes, expected %d"
                % (sink.total, self.expected_bytes)
            )
        if sink.row != sink.rows:
            raise ValueError("corrupt LZW strip: incomplete rows")


def read(path, x, y, w, h):
    """Return the ``w`` by ``h`` window at ``(x, y)`` as integer rows.

    ``x``/``y`` are measured from the top-left corner under Orientation=1.
    The window must be positive and completely inside the image.  Unsupported
    TIFF variants, invalid windows and damaged strips raise ``ValueError``.
    """
    try:
        x = operator.index(x)
        y = operator.index(y)
        w = operator.index(w)
        h = operator.index(h)
    except TypeError:
        raise ValueError("window coordinates and size must be integers") from None
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise ValueError("window must be positive and inside the image")

    # Unbuffered reads: Python's buffered reader would speculatively fetch
    # neighbouring bytes and could touch strip payloads outside the window.
    try:
        fp = _open(path, "rb", buffering=0)
    except OSError as exc:
        raise ValueError("cannot open TIFF: %s" % exc) from None
    with fp:
        file_size = os.fstat(fp.fileno()).st_size
        return _Reader(fp, file_size).read_window(x, y, w, h)
