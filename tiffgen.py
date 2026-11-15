"""Independent TIFF writer used to produce test imagery for ``tiffwindow``.

This module is deliberately self-contained: it does not import the reader and
builds Classic TIFF files byte by byte, including a from-scratch TIFF LZW
encoder (MSB-first codes, 9/10/11/12-bit early code-width changes, CLEAR and
EOI codes).  The caller supplies the source samples, so the expected pixels
are known without reading any TIFF back.

Only the Python standard library is used.
"""

from __future__ import annotations

import os
import struct

__all__ = ["write_tiff", "lzw_encode", "encode_strip"]

_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 6: 1, 7: 1, 8: 2, 9: 4}


class BitWriter:
    """MSB-first bit packer used by the TIFF LZW encoder."""

    __slots__ = ("out", "acc", "nbits")

    def __init__(self):
        self.out = bytearray()
        self.acc = 0
        self.nbits = 0

    def write(self, code, width):
        self.acc = (self.acc << width) | code
        self.nbits += width
        while self.nbits >= 8:
            self.nbits -= 8
            self.out.append((self.acc >> self.nbits) & 0xFF)
            self.acc &= (1 << self.nbits) - 1 if self.nbits else 0

    def finish(self):
        if self.nbits:
            self.out.append((self.acc << (8 - self.nbits)) & 0xFF)
            self.nbits = 0
        return bytes(self.out)


def lzw_encode(
    data,
    clear_interval=None,
    width_policy="early",
    omit_initial_clear=False,
    omit_eoi=False,
    duplicate_clear=False,
    auto_clear=True,
    statistics=None,
):
    """Compress ``data`` exactly as TIFF LZW (Compression=5) expects.

    ``width_policy`` may be ``"early"`` (the TIFF rule: switch when the next
    free code reaches 511/1023/2047) or ``"late"`` (deliberately wrong, used
    only to prove that the reader really implements the TIFF rule).
    """
    bw = BitWriter()
    stats = statistics if statistics is not None else {}
    stats.update(
        codes=0,
        clears=0,
        transitions=[],
        max_width=9,
        entries=0,
        kwkwk=0,
        width_policy=width_policy,
    )

    def emit(code, width):
        bw.write(code, width)
        stats["codes"] += 1

    table = {}
    next_code = 258
    width = 9
    emitted_since_clear = 0

    def reset():
        nonlocal table, next_code, width, emitted_since_clear
        table = {(value,): value for value in range(256)}
        next_code = 258
        width = 9
        emitted_since_clear = 0

    reset()
    if not omit_initial_clear:
        emit(256, width)
        stats["clears"] += 1
        if duplicate_clear:
            emit(256, width)
            stats["clears"] += 1

    last_added = -1
    if data:
        omega = data[0]
        for byte in data[1:]:
            key = (omega, byte)
            entry = table.get(key)
            if entry is not None:
                omega = entry
                continue
            if omega == last_added:
                # The decoder will read this code as "the entry it is about
                # to create" (KwKwK / dictionary self-reference).
                stats["kwkwk"] += 1
            emit(omega, width)
            stats["entries"] += 1
            if next_code < 4096:
                table[key] = next_code
                last_added = next_code
                next_code += 1
                if width < 12:
                    if width_policy == "early":
                        threshold = (1 << width) - 1
                    elif width_policy == "late":
                        threshold = 1 << width
                    else:
                        raise ValueError("width_policy must be early or late")
                    if next_code > threshold:
                        width += 1
                        stats["transitions"].append(width)
                        if width > stats["max_width"]:
                            stats["max_width"] = width
            elif auto_clear:
                emit(256, width)
                stats["clears"] += 1
                reset()
            omega = byte
            emitted_since_clear += 1
            if clear_interval and emitted_since_clear >= clear_interval:
                emit(256, width)
                stats["clears"] += 1
                reset()
        if omega == last_added:
            stats["kwkwk"] += 1
        emit(omega, width)
    if not omit_eoi:
        # The decoder adds one more string-table entry when it reads that
        # final data code, so the encoder's next free code (and possibly the
        # code width) must advance before EOI is written.
        if next_code < 4096:
            next_code += 1
            if width < 12 and next_code > (1 << width) - 1:
                if width_policy == "early":
                    width += 1
                    stats["transitions"].append(width)
                    if width > stats["max_width"]:
                        stats["max_width"] = width
        emit(257, width)
    return bw.finish()


def encode_strip(
    rows,
    bits,
    byte_order,
    predictor=1,
    clear_interval=None,
    width_policy="early",
    omit_initial_clear=False,
    omit_eoi=False,
    duplicate_clear=False,
    auto_clear=True,
    statistics=None,
):
    """Apply the horizontal predictor and LZW-compress one strip."""
    prefix = "<" if byte_order == "II" else ">"
    mask = (1 << bits) - 1
    raw = bytearray()
    for row in rows:
        values = list(row)
        if predictor == 2:
            for index in range(len(values) - 1, 0, -1):
                values[index] = (values[index] - values[index - 1]) & mask
        if bits == 8:
            raw.extend(values)
        else:
            raw.extend(struct.pack(prefix + str(len(values)) + "H", *values))
    return lzw_encode(
        bytes(raw),
        clear_interval=clear_interval,
        width_policy=width_policy,
        omit_initial_clear=omit_initial_clear,
        omit_eoi=omit_eoi,
        duplicate_clear=duplicate_clear,
        auto_clear=auto_clear,
        statistics=statistics,
    )


def _pack_values(values, typ, byte_order):
    prefix = "<" if byte_order == "II" else ">"
    if typ == 3:
        return struct.pack(prefix + str(len(values)) + "H", *values)
    if typ == 4:
        return struct.pack(prefix + str(len(values)) + "I", *values)
    return bytes(values)


def write_tiff(
    path,
    rows,
    bits=16,
    byte_order="II",
    predictor=1,
    rows_per_strip=None,
    photometric=1,
    clear_interval=None,
    width_policy="early",
    overrides=None,
    omit_tags=(),
    extra_tags=(),
    width_type=4,
    offsets_type=4,
    bytecounts_type=4,
    omit_rows_per_strip=False,
    omit_initial_clear=False,
    omit_eoi=False,
    duplicate_clear=False,
    auto_clear=True,
    multipage=False,
    bigtiff=False,
    strip_statistics=None,
):
    """Write ``rows`` as a single-page Classic TIFF with LZW strips.

    ``rows`` is a list of equal-length integer rows; samples must fit in
    ``bits`` bits.  ``strip_statistics`` may be a list which receives one
    dictionary of encoder statistics per strip.
    """
    if not rows:
        raise ValueError("at least one row is required")
    width = len(rows[0])
    length = len(rows)
    if width <= 0:
        raise ValueError("rows must not be empty")
    if any(len(row) != width for row in rows):
        raise ValueError("all rows must have the same length")
    if bits not in (8, 16):
        raise ValueError("bits must be 8 or 16")
    if byte_order not in ("II", "MM"):
        raise ValueError("byte_order must be II or MM")
    limit = (1 << bits) - 1
    for row in rows:
        for value in row:
            if not isinstance(value, int) or value < 0 or value > limit:
                raise ValueError("sample out of range for %d-bit data" % bits)

    if omit_rows_per_strip:
        rows_per_strip = length
        nstrips = 1
    else:
        if rows_per_strip is None:
            rows_per_strip = length
        if rows_per_strip <= 0:
            raise ValueError("rows_per_strip must be positive")
        nstrips = (length + rows_per_strip - 1) // rows_per_strip

    if path is not None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    payload = bytearray()
    strip_offsets = []
    strip_lengths = []
    stats_list = [] if strip_statistics is None else strip_statistics
    for strip_index in range(nstrips):
        first = strip_index * rows_per_strip
        strip_rows = rows[first:first + rows_per_strip]
        stats = {}
        compressed = encode_strip(
            strip_rows,
            bits,
            byte_order,
            predictor=predictor,
            clear_interval=clear_interval,
            width_policy=width_policy,
            omit_initial_clear=omit_initial_clear,
            omit_eoi=omit_eoi,
            duplicate_clear=duplicate_clear,
            auto_clear=auto_clear,
            statistics=stats,
        )
        stats["rows"] = len(strip_rows)
        stats["raw_bytes"] = len(strip_rows) * width * (bits // 8)
        stats_list.append(stats)
        strip_offsets.append(8 + len(payload))
        strip_lengths.append(len(compressed))
        payload.extend(compressed)

    if bigtiff:
        data = (
            (b"II" if byte_order == "II" else b"MM")
            + struct.pack("<H" if byte_order == "II" else ">H", 43)
            + struct.pack("<H" if byte_order == "II" else ">H", 8)
            + b"\x00\x00"
            + struct.pack("<Q" if byte_order == "II" else ">Q", 16)
            + struct.pack("<Q" if byte_order == "II" else ">Q", 0)
            + b"\x00" * 64
        )
        with open(path, "wb") as stream:
            stream.write(data)
        return {"bigtiff": True, "width": width, "length": length}

    prefix = "<" if byte_order == "II" else ">"
    ifd_offset = 8 + len(payload)
    if ifd_offset % 2:
        payload.append(0)
        ifd_offset += 1

    tags = [
        (256, width_type, [width]),
        (257, 4, [length]),
        (258, 3, [bits]),
        (259, 3, [5]),
        (262, 3, [photometric]),
        (266, 3, [1]),
        (273, offsets_type, strip_offsets),
        (274, 3, [1]),
        (277, 3, [1]),
        (284, 3, [1]),
        (317, 3, [predictor]),
        (339, 3, [1]),
    ]
    if not omit_rows_per_strip:
        tags.append((278, 4, [rows_per_strip]))
    if bytecounts_type == 3:
        tags.append((279, 3, [count & 0xFFFF for count in strip_lengths]))
    else:
        tags.append((279, bytecounts_type, strip_lengths))
    for tag, typ, values in extra_tags:
        tags.append((tag, typ, list(values)))
    if overrides:
        overridden = []
        for tag, typ, values in tags:
            if tag in overrides:
                value = overrides[tag]
                if isinstance(value, (list, tuple)):
                    values = list(value)
                else:
                    values = [value]
            overridden.append((tag, typ, values))
        tags = overridden
    tags = [item for item in tags if item[0] not in set(omit_tags)]
    tags.sort(key=lambda item: item[0])

    entry_count = len(tags)
    arrays_offset = ifd_offset + 2 + 12 * entry_count + 4
    if arrays_offset % 2:
        arrays_offset += 1

    entries = []
    aux = bytearray()
    for tag, typ, values in tags:
        total = _TYPE_SIZE[typ] * len(values)
        raw = _pack_values(values, typ, byte_order)
        if total <= 4:
            value_field = raw.ljust(4, b"\x00")
        else:
            if (arrays_offset + len(aux)) % 2:
                aux.append(0)
            offset = arrays_offset + len(aux)
            value_field = struct.pack(prefix + "I", offset)
            aux.extend(raw)
        entries.append(
            struct.pack(prefix + "HHI", tag, typ, len(values)) + value_field
        )

    if multipage:
        next_ifd = ifd_offset
    else:
        next_ifd = 0

    header = (
        (b"II" if byte_order == "II" else b"MM")
        + struct.pack(prefix + "H", 42)
        + struct.pack(prefix + "I", ifd_offset)
    )
    ifd = (
        struct.pack(prefix + "H", entry_count)
        + b"".join(entries)
        + struct.pack(prefix + "I", next_ifd)
    )
    if arrays_offset > ifd_offset + len(ifd):
        ifd = ifd.ljust(arrays_offset - ifd_offset, b"\x00")
    blob = header + bytes(payload) + ifd + bytes(aux)

    if path is None:
        return {
            "data": blob,
            "width": width,
            "length": length,
            "bits": bits,
            "byte_order": byte_order,
            "rows_per_strip": rows_per_strip,
            "nstrips": nstrips,
            "strip_offsets": strip_offsets,
            "strip_lengths": strip_lengths,
            "ifd_offset": ifd_offset,
            "statistics": stats_list,
        }
    with open(path, "wb") as stream:
        stream.write(blob)
    return {
        "path": path,
        "size": len(blob),
        "width": width,
        "length": length,
        "bits": bits,
        "byte_order": byte_order,
        "rows_per_strip": rows_per_strip,
        "nstrips": nstrips,
        "strip_offsets": strip_offsets,
        "strip_lengths": strip_lengths,
        "ifd_offset": ifd_offset,
        "statistics": stats_list,
    }
