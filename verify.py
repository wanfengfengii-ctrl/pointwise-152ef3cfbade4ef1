"""Command-line verification and demonstration for :mod:`tiffwindow`.

Usage::

    python3 -I verify.py setup
    python3 -I verify.py verify

``setup`` byte-compiles the modules and imports them in isolated mode.
``verify`` builds every fixture from independent source arrays with
:mod:`tiffgen`, then compares the reader's output pixel for pixel.  Coverage
includes both byte orders, 8- and 16-bit samples, Predictor 1 and 2,
early code-width transitions, CLEAR codes (including consecutive clears),
dictionary self-reference (KwKwK), dictionary-full clears, windows spanning
strips, minimal strips, inline and offset IFD values, the required ``ValueError``
paths, and a measured read-amplification check proving that only strips
intersecting the window are read.

Only the Python standard library is used.  No third-party image library is
required to run the checks.
"""

from __future__ import annotations

import os
import random
import struct
import subprocess
import sys
import tempfile
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import tiffgen  # noqa: E402
import tiffwindow  # noqa: E402

CHECKS = []


def check(item):
    CHECKS.append(item)
    return item


class Failure(AssertionError):
    pass


def require(condition, message):
    if not condition:
        raise Failure(message)


def window(rows, x, y, w, h):
    return [[rows[yy][xx] for xx in range(x, x + w)] for yy in range(y, y + h)]


def source(bits, width, height, seed, low=0, high=None):
    rng = random.Random(seed)
    if high is None:
        high = (1 << bits) - 1
    return [[rng.randrange(low, high + 1) for _ in range(width)] for _ in range(height)]


class Fixture:
    """A generated TIFF plus its source pixels and window expectations."""

    def __init__(self, name, rows, **options):
        self.name = name
        self.rows = rows
        self.options = options
        self.path = os.path.join(FIXTURE_DIR, name)
        self.info = tiffgen.write_tiff(self.path, rows, **options)

    def read(self, x, y, w, h):
        return tiffwindow.read(self.path, x, y, w, h)

    def expect(self, x, y, w, h):
        return window(self.rows, x, y, w, h)


def run_case(name, fixture, boxes):
    for x, y, w, h in boxes:
        got = fixture.read(x, y, w, h)
        want = fixture.expect(x, y, w, h)
        require(got == want, "%s: window %r mismatch" % (name, (x, y, w, h)))


FIXTURE_DIR = None


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------
def cmd_setup():
    import py_compile

    for module in ("tiffwindow.py", "tiffgen.py"):
        py_compile.compile(os.path.join(_HERE, module), doraise=True)
    import importlib

    for module in ("tiffwindow", "tiffgen"):
        importlib.import_module(module)
    print("SETUP_OK")


# --------------------------------------------------------------------------
# business checks
# --------------------------------------------------------------------------
@check
def check_byte_orders():
    data = source(16, 301, 173, 101)
    for order in ("II", "MM"):
        for predictor in (1, 2):
            for rows_per_strip in (1, 7, 50, 173, 5000):
                fixture = Fixture(
                    "order_%s_%d_%d.tif"
                    % (order, predictor, rows_per_strip),
                    data,
                    bits=16,
                    byte_order=order,
                    predictor=predictor,
                    rows_per_strip=rows_per_strip,
                )
                run_case(
                    "order %s pred %d rps %d" % (order, predictor, rows_per_strip),
                    fixture,
                    [
                        (0, 0, 1, 1),
                        (300, 172, 1, 1),
                        (37, 61, 199, 91),
                        (250, 170, 51, 3),
                        (0, 0, 301, 173),
                        (101, 3, 1, 160),
                    ],
                )
                require(fixture.info["nstrips"] == (173 + rows_per_strip - 1) // rows_per_strip,
                        "strip count")


@check
def check_8bit():
    data = source(8, 200, 120, 202)
    for order in ("II", "MM"):
        for predictor in (1, 2):
            fixture = Fixture(
                "bits8_%s_%d.tif" % (order, predictor),
                data,
                bits=8,
                byte_order=order,
                predictor=predictor,
                rows_per_strip=9,
            )
            run_case(
                "8-bit %s pred %d" % (order, predictor),
                fixture,
                [(0, 0, 1, 1), (199, 119, 1, 1), (50, 40, 100, 70), (0, 0, 200, 120)],
            )


@check
def check_width_transitions():
    # Large, incompressible strips drive the code width through 9 -> 12.
    data = source(8, 400, 60, 303)
    fixture = Fixture("width.tif", data, bits=8, rows_per_strip=60, auto_clear=True)
    stats = fixture.info["statistics"]
    widest = max(s["max_width"] for s in stats)
    require(widest == 12, "encoder did not reach 12-bit codes")
    require(any(10 in s["transitions"] for s in stats), "no 9->10 transition")
    require(any(11 in s["transitions"] for s in stats), "no 10->11 transition")
    require(any(s["clears"] >= 2 for s in stats), "dictionary-full CLEAR did not occur")
    run_case("width transitions", fixture, [(0, 0, 400, 60), (1, 0, 398, 60), (0, 0, 1, 1)])

    exact = Fixture("width_exact.tif", data, bits=8, rows_per_strip=60, auto_clear=False)
    for strip in exact.info["statistics"]:
        require(strip["transitions"] == [10, 11, 12], "unexpected width sequence")


@check
def check_bit_width_policies():
    # "late" width changes are not TIFF-conformant: the reader must reject
    # them instead of guessing.  "early" (TIFF's rule) must decode.
    data = source(8, 220, 90, 404)
    early = Fixture("policy_early.tif", data, bits=8, rows_per_strip=90)
    run_case("early policy", early, [(0, 0, 220, 90)])
    late = Fixture("policy_late.tif", data, bits=8, rows_per_strip=90, width_policy="late")
    try:
        late.read(0, 0, 220, 90)
    except ValueError:
        pass
    else:
        raise Failure("late width changes were accepted")


@check
def check_explicit_clear_tables():
    data = source(8, 90, 128, 505)
    # A CLEAR every few codes, plus redundant consecutive CLEARs.
    fixture = Fixture(
        "clear_interval.tif",
        data,
        bits=8,
        rows_per_strip=128,
        clear_interval=11,
        duplicate_clear=True,
    )
    stats = fixture.info["statistics"][0]
    require(stats["clears"] > 5, "fixture does not contain enough CLEAR codes")
    run_case("clear interval", fixture, [(0, 0, 90, 128), (20, 13, 50, 70)])

    # A CLEAR as the very first two codes (consecutive clears at strip start).
    first_double = Fixture(
        "clear_double_start.tif",
        data,
        bits=8,
        rows_per_strip=128,
        duplicate_clear=True,
    )
    run_case("double clear", first_double, [(0, 0, 90, 128)])


@check
def check_dictionary_self_reference():
    # Long runs create KwKwK codes: the decoder reads the code that it is
    # about to add to the table.
    rows = [[7] * 300 for _ in range(80)]
    rows[40] = [123] * 300
    fixture = Fixture("kwkwk.tif", rows, bits=8, rows_per_strip=80)
    require(fixture.info["statistics"][0]["kwkwk"] > 10, "fixture lacks self references")
    run_case("KwKwK", fixture, [(0, 0, 300, 80), (299, 0, 1, 80), (100, 40, 200, 1)])


@check
def check_ifd_value_layout():
    # ImageWidth as SHORT (inline) in one file and LONG (still inline at 4
    # bytes) in another; BitsPerSample offset for 16-bit; strip tables inline
    # for a single-strip file and offset for multi-strip files.
    data = source(16, 64, 40, 606)
    for width_type in (3, 4):
        fixture = Fixture(
            "inline_%d.tif" % width_type,
            data,
            bits=16,
            width_type=width_type,
            rows_per_strip=40,
        )
        run_case("inline width type %d" % width_type, fixture, [(0, 0, 64, 40), (5, 5, 20, 20)])
    # SHORT strip offsets and byte counts (2-byte inline / offset handling).
    fixture = Fixture(
        "short_bytecounts.tif",
        data,
        bits=16,
        rows_per_strip=5,
        bytecounts_type=3,
    )
    for strip_count in fixture.info["strip_lengths"]:
        require(strip_count < 65536, "fixture byte count too large for SHORT")
    run_case("short byte counts", fixture, [(11, 7, 30, 22)])
    short_offsets = Fixture(
        "short_offsets.tif",
        data,
        bits=16,
        rows_per_strip=5,
        offsets_type=3,
    )
    run_case("short strip offsets", short_offsets, [(11, 7, 30, 22), (0, 39, 64, 1)])
    # Missing RowsPerStrip means one strip for the whole image (TIFF default).
    fixture = Fixture(
        "default_rps.tif",
        data,
        bits=16,
        omit_rows_per_strip=True,
    )
    require(fixture.info["nstrips"] == 1, "default RowsPerStrip fixture")
    run_case("default RowsPerStrip", fixture, [(0, 0, 64, 40), (9, 9, 30, 20)])


@check
def check_early_change_at_strip_edges():
    # All-9-bit strip: 300 literal bytes stay below the 511-entry threshold.
    rows = [[(x * 3) & 0xFF for x in range(100)] for _ in range(3)]
    fixture = Fixture("nine_bit.tif", rows, bits=8, rows_per_strip=3)
    require(fixture.info["statistics"][0]["transitions"] == [], "fixture left 9-bit codes")
    run_case("nine-bit strip", fixture, [(0, 0, 100, 3)])

    # Tiny last strip (1 row) after multi-row strips.
    data = source(16, 101, 205, 707)
    fixture = Fixture("last_strip.tif", data, bits=16, rows_per_strip=13)
    require(fixture.info["nstrips"] == 16, "strip count for last-strip fixture")
    run_case("last strip", fixture, [(0, 204, 101, 1), (17, 190, 70, 15)])


@check
def check_wrap_around():
    # Horizontal differencing must wrap mod 2**16 / 2**8.
    rows = [[0, 65535, 1, 65534, 0, 12345, 65535, 300] * 30 for _ in range(12)]
    for order in ("II", "MM"):
        fixture = Fixture(
            "wrap_%s.tif" % order,
            rows,
            bits=16,
            byte_order=order,
            predictor=2,
            rows_per_strip=4,
        )
        run_case("wrap %s" % order, fixture, [(0, 0, 240, 12), (7, 3, 55, 6)])
    rows8 = [[0, 255, 1, 254, 200, 55, 255, 0] * 30 for _ in range(12)]
    for order in ("II", "MM"):
        fixture = Fixture(
            "wrap8_%s.tif" % order,
            rows8,
            bits=8,
            byte_order=order,
            predictor=2,
            rows_per_strip=4,
        )
        run_case("wrap8 %s" % order, fixture, [(0, 0, 240, 12), (7, 3, 55, 6)])


@check
def check_cross_strip_windows():
    data = source(16, 40, 100, 808)
    fixture = Fixture("cross.tif", data, bits=16, rows_per_strip=6, predictor=2)
    run_case(
        "cross strip",
        fixture,
        [
            (0, 2, 40, 10),
            (5, 4, 30, 20),
            (0, 95, 40, 5),
            (0, 0, 40, 100),
            (10, 11, 1, 83),
        ],
    )


@check
def check_minimal_windows():
    data = source(16, 1, 50, 909)  # one column
    fixture = Fixture("one_column.tif", data, bits=16, rows_per_strip=7)
    run_case("one column", fixture, [(0, 0, 1, 50), (0, 3, 1, 7)])
    flat = source(16, 60, 1, 910)  # one row
    fixture = Fixture("one_row.tif", flat, bits=16, rows_per_strip=1)
    run_case("one row", fixture, [(0, 0, 60, 1), (5, 0, 1, 1)])


@check
def check_photometric_black_is_zero():
    # Photometric=0 (WhiteIsZero) changes display, not stored sample values.
    data = source(8, 40, 30, 1001)
    fixture = Fixture("white_is_zero.tif", data, bits=8, rows_per_strip=7, photometric=0)
    run_case("photometric 0", fixture, [(0, 0, 40, 30)])


@check
def check_rejections():
    data = source(16, 32, 20, 1111)
    good_path = os.path.join(FIXTURE_DIR, "reject_good.tif")
    tiffgen.write_tiff(good_path, data, bits=16, rows_per_strip=5)

    bad_options = [
        ("compression", dict(overrides={259: 1})),
        ("tiled", dict(extra_tags=[(322, 4, [16]), (323, 4, [16])])),
        ("orientation", dict(overrides={274: 3})),
        ("samples_per_pixel", dict(overrides={277: 2})),
        ("planar config", dict(overrides={284: 2})),
        ("predictor 3", dict(overrides={317: 3})),
        ("bits 12", dict(bits=16, overrides={258: 12})),
        ("sample format signed", dict(overrides={339: 2})),
        ("fill order 2", dict(overrides={266: 2})),
        ("rgb photometric", dict(overrides={262: 2})),
        ("extra samples", dict(extra_tags=[(338, 3, [0])])),
        ("subifd", dict(extra_tags=[(330, 4, [16])])),
        ("multipage", dict(multipage=True)),
    ]
    for label, options in bad_options:
        path = os.path.join(FIXTURE_DIR, "reject_%s.tif" % label.replace(" ", "_"))
        tiffgen.write_tiff(path, data, **options)
        try:
            tiffwindow.read(path, 0, 0, 4, 4)
        except ValueError:
            pass
        else:
            raise Failure("unsupported %s was accepted" % label)

    # BigTIFF magic.
    path = os.path.join(FIXTURE_DIR, "reject_bigtiff.tif")
    tiffgen.write_tiff(path, data, bigtiff=True)
    try:
        tiffwindow.read(path, 0, 0, 1, 1)
    except ValueError:
        pass
    else:
        raise Failure("BigTIFF was accepted")

    # Not a TIFF at all.
    path = os.path.join(FIXTURE_DIR, "reject_garbage.tif")
    with open(path, "wb") as stream:
        stream.write(b"NOTATIFF" * 3)
    for bad in (path, os.path.join(FIXTURE_DIR, "does_not_exist.tif")):
        try:
            tiffwindow.read(bad, 0, 0, 1, 1)
        except (ValueError, OSError):
            pass
        else:
            raise Failure("non-TIFF input was accepted")

    # Invalid windows on a valid image.
    for x, y, w, h in [
        (-1, 0, 1, 1),
        (0, -1, 1, 1),
        (0, 0, 0, 1),
        (0, 0, 1, 0),
        (0, 0, -1, 1),
        (31, 0, 2, 1),
        (0, 19, 1, 2),
        (32, 0, 1, 1),
        (0, 20, 1, 1),
        (10, 10, 100, 1),
    ]:
        try:
            tiffwindow.read(good_path, x, y, w, h)
        except ValueError:
            pass
        else:
            raise Failure("invalid window %r was accepted" % ((x, y, w, h),))

    # Non-integer coordinates.
    for args in [("0", 0, 1, 1), (0, 0.5, 1, 1), (0, 0, None, 1)]:
        try:
            tiffwindow.read(good_path, *args)
        except (ValueError, TypeError):
            pass
        else:
            raise Failure("non-integer window %r was accepted" % (args,))


def _patch(path, offset, data):
    with open(path, "r+b") as stream:
        stream.seek(offset)
        stream.write(data)


def _ifd_tag_map(path):
    blob = open(path, "rb").read()
    prefix = "<" if blob[:2] == b"II" else ">"
    ifd = struct.unpack(prefix + "I", blob[4:8])[0]
    count = struct.unpack(prefix + "H", blob[ifd:ifd + 2])[0]
    result = {}
    for index in range(count):
        raw = blob[ifd + 2 + 12 * index:ifd + 14 + 12 * index]
        tag, typ, n = struct.unpack(prefix + "HHI", raw[:8])
        result[tag] = (typ, n, raw[8:12])
    return result, prefix


@check
def check_damaged_strips():
    data = source(8, 64, 48, 1212)
    boxes = [(0, 0, 64, 48)]

    path = os.path.join(FIXTURE_DIR, "damage_bad_code.tif")
    tiffgen.write_tiff(path, data, bits=8, rows_per_strip=48)
    info = tiffgen.write_tiff(None, data, bits=8, rows_per_strip=48)
    # Give the decoder a code that is not in the table yet: replace the
    # compressed payload with 0xFF bytes (invalid LZW stream).
    _patch(path, info["strip_offsets"][0], b"\xff" * info["strip_lengths"][0])
    try:
        tiffwindow.read(path, *boxes[0])
    except ValueError:
        pass
    else:
        raise Failure("invalid LZW payload was accepted")

    path = os.path.join(FIXTURE_DIR, "damage_truncated.tif")
    tiffgen.write_tiff(path, data, bits=8, rows_per_strip=48)
    info = tiffgen.write_tiff(None, data, bits=8, rows_per_strip=48)
    # Point the strip beyond the file.
    tags, prefix = _ifd_tag_map(path)
    blob = open(path, "rb").read()
    ifd = struct.unpack(prefix + "I", blob[4:8])[0]
    count = struct.unpack(prefix + "H", blob[ifd:ifd + 2])[0]
    for index in range(count):
        entry = ifd + 2 + 12 * index
        tag = struct.unpack(prefix + "H", blob[entry:entry + 2])[0]
        if tag == 273:
            _patch(path, entry + 8, struct.pack(prefix + "I", len(blob) + 100))
    try:
        tiffwindow.read(path, *boxes[0])
    except ValueError:
        pass
    else:
        raise Failure("strip outside the file was accepted")

    path = os.path.join(FIXTURE_DIR, "damage_short_data.tif")
    tiffgen.write_tiff(path, data, bits=8, rows_per_strip=48)
    info = tiffgen.write_tiff(None, data, bits=8, rows_per_strip=48)
    tags, prefix = _ifd_tag_map(path)
    blob = open(path, "rb").read()
    ifd = struct.unpack(prefix + "I", blob[4:8])[0]
    count = struct.unpack(prefix + "H", blob[ifd:ifd + 2])[0]
    for index in range(count):
        entry = ifd + 2 + 12 * index
        tag = struct.unpack(prefix + "H", blob[entry:entry + 2])[0]
        if tag == 279:
            # Cut the declared payload short so the bit stream ends before
            # the EOI code; the reader must reject the strip.
            _patch(path, entry + 8, struct.pack(prefix + "I", info["strip_lengths"][0] - 16))
    try:
        tiffwindow.read(path, *boxes[0])
    except ValueError:
        pass
    else:
        raise Failure("byte count mismatch was accepted")

    path = os.path.join(FIXTURE_DIR, "damage_no_eoi.tif")
    tiffgen.write_tiff(path, data, bits=8, rows_per_strip=48, omit_eoi=True)
    try:
        tiffwindow.read(path, *boxes[0])
    except ValueError:
        pass
    else:
        raise Failure("strip without EOI was accepted")

    path = os.path.join(FIXTURE_DIR, "damage_no_clear.tif")
    tiffgen.write_tiff(path, data, bits=8, rows_per_strip=48, omit_initial_clear=True)
    try:
        tiffwindow.read(path, *boxes[0])
    except ValueError:
        pass
    else:
        raise Failure("strip without a CLEAR code was accepted")

    path = os.path.join(FIXTURE_DIR, "damage_overflow.tif")
    wide = source(8, 400, 60, 1313)
    tiffgen.write_tiff(path, wide, bits=8, rows_per_strip=60, auto_clear=False)
    try:
        tiffwindow.read(path, 0, 0, 400, 60)
    except ValueError:
        pass
    else:
        raise Failure("dictionary overflow without CLEAR was accepted")

    # A damaged strip outside the requested window is not inspected at all.
    path = os.path.join(FIXTURE_DIR, "damage_elsewhere.tif")
    tiffgen.write_tiff(path, data, bits=8, rows_per_strip=8)
    info = tiffgen.write_tiff(None, data, bits=8, rows_per_strip=8)
    for index in range(info["nstrips"]):
        if index != 2:
            continue
        _patch(path, info["strip_offsets"][index], b"\x00" * info["strip_lengths"][index])
    got = tiffwindow.read(path, 0, 0, 64, 8)
    require(got == window(data, 0, 0, 64, 8), "clean window near a damaged strip")
    try:
        tiffwindow.read(path, 0, 16, 64, 8)
    except ValueError:
        pass
    else:
        raise Failure("damaged strip serving the window was accepted")


@check
def check_partial_window_rejects_bad_strip():
    # A window that touches one clean and one damaged strip must fail as a
    # whole, never returning a half-filled window.
    data = source(8, 30, 24, 1414)
    path = os.path.join(FIXTURE_DIR, "half_damage.tif")
    tiffgen.write_tiff(path, data, bits=8, rows_per_strip=8)
    info = tiffgen.write_tiff(None, data, bits=8, rows_per_strip=8)
    _patch(path, info["strip_offsets"][1], b"\x12" * 4)
    try:
        tiffwindow.read(path, 0, 2, 30, 12)
    except ValueError:
        pass
    else:
        raise Failure("window spanning a damaged strip returned data")


@check
def check_read_amplification():
    """Only strips intersecting the window may be read, and memory stays bounded."""
    width, height, rows_per_strip = 256, 256, 8
    # Highly incompressible payload keeps strip sizes realistic.
    data = source(16, width, height, 1515, 0, 65535)
    path = os.path.join(FIXTURE_DIR, "amplification.tif")
    tiffgen.write_tiff(
        path, data, bits=16, byte_order="MM", predictor=2,
        rows_per_strip=rows_per_strip,
    )
    info = tiffgen.write_tiff(
        None, data, bits=16, byte_order="MM", predictor=2,
        rows_per_strip=rows_per_strip,
    )

    class RecordingFile:
        """File proxy that records which byte ranges were actually read."""

        def __init__(self, handle):
            self.handle = handle
            self.reads = []

        def seek(self, offset, whence=0):
            self.handle.seek(offset, whence)
            return offset

        def read(self, size):
            start = self.handle.tell()
            blob = self.handle.read(size)
            self.reads.append((start, len(blob)))
            return blob

        def fileno(self):
            return self.handle.fileno()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.handle.close()
            return False

    original_open = tiffwindow._open

    def fake_open(path, mode="r", *args, **kwargs):
        handle = open(path, mode, *args, **kwargs)
        return RecordingFile(handle)

    tiffwindow._open = fake_open
    try:
        recorder = {}
        original = tiffwindow._Reader.__init__

        def patched_init(self, fp, file_size):
            recorder["file"] = fp
            original(self, fp, file_size)

        tiffwindow._Reader.__init__ = patched_init
        try:
            got = tiffwindow.read(path, 100, 100, 40, 20)
        finally:
            tiffwindow._Reader.__init__ = original
    finally:
        tiffwindow._open = original_open

    require(got == window(data, 100, 100, 40, 20), "amplification window mismatch")
    covered = set()
    for start, length in recorder["file"].reads:
        if length:
            covered.update(range(start, start + length))
    for index, offset in enumerate(info["strip_offsets"]):
        strip_length = info["strip_lengths"][index]
        touched = any(offset <= pos < offset + strip_length for pos in covered)
        first_row = index * rows_per_strip
        intersects = first_row < 120 and first_row + rows_per_strip > 100
        if intersects:
            require(touched, "strip %d serving the window was not read" % index)
        else:
            require(not touched, "strip %d outside the window was read" % index)

    total_read = sum(length for start, length in recorder["file"].reads)
    header_and_tables = 8 + 2 + 12 * 20 + 4 + 8 * 8 + 8 * 4
    require(
        total_read < header_and_tables + 4 * max(info["strip_lengths"]),
        "reader amplified reads: %d bytes for a 40x20 window" % total_read,
    )

    # Memory: for a small window, peak allocation must stay near one decoded
    # row plus the fixed LZW table, far below one strip (8 rows) and far below
    # the full 256x256 16-bit image.
    import tracemalloc

    tracemalloc.start()
    got = tiffwindow.read(path, 10, 10, 3, 3)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    require(got == window(data, 10, 10, 3, 3), "memory-check window mismatch")
    row_bytes = width * 2
    require(
        peak < 4 * row_bytes + 150000,
        "peak memory %d bytes exceeds the O(width) allowance" % peak,
    )
    require(peak < width * height * 2, "peak memory exceeds the image itself")

    # Same 3x3 window on a much larger image: peak must not grow with image
    # or strip size (one row + the fixed 4096-entry LZW table only).
    big_width, big_height, big_rps = 2000, 1200, 600
    big = [
        [((x * 31 + y * 17) ^ (x + y)) & 0xFFFF for x in range(big_width)]
        for y in range(big_height)
    ]
    big_path = os.path.join(FIXTURE_DIR, "big_window.tif")
    tiffgen.write_tiff(
        big_path, big, bits=16, byte_order="MM", predictor=2,
        rows_per_strip=big_rps,
    )
    tracemalloc.start()
    got_big = tiffwindow.read(big_path, 777, 555, 3, 3)
    _, big_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    require(got_big == window(big, 777, 555, 3, 3), "large-image window mismatch")
    require(big_peak < 4 * big_width * 2 + 150000,
            "peak memory %d bytes on the large image" % big_peak)
    require(
        big_peak < width * height * 2 + 20000,
        "peak grew with image size: %d vs %d" % (big_peak, peak),
    )
    print(
        "MEMORY_OK peak=%d bytes on %dx%d (one row=%d), peak=%d on %dx%d"
        % (peak, width, height, row_bytes, big_peak, big_width, big_height)
    )
    print(
        "READ_AMPLIFICATION_OK bytes_in_intersecting_strips=%d bytes_read=%d peak=%d"
        % (
            sum(
                length
                for index, length in enumerate(info["strip_lengths"])
                if index * rows_per_strip < 120 and index * rows_per_strip + rows_per_strip > 100
            ),
            total_read,
            peak,
        )
    )


@check
def check_libtiff_interop():
    """Cross-check the fixtures themselves with the system libtiff (optional)."""
    if not _have_convert():
        print("LIBTIFF_INTEROP_SKIPPED (no ImageMagick/libTIFF available)")
        return
    data = source(16, 128, 96, 1616)
    for order in ("II", "MM"):
        for predictor in (1, 2):
            path = os.path.join(FIXTURE_DIR, "interop_%s_%d.tif" % (order, predictor))
            tiffgen.write_tiff(path, data, bits=16, byte_order=order, predictor=predictor,
                               rows_per_strip=16)
            raw_path = os.path.join(FIXTURE_DIR, "interop.raw")
            result = subprocess.run(
                ["convert", path, "-depth", "16", "-endian", "LSB", "gray:" + raw_path],
                capture_output=True,
                text=True,
            )
            require(result.returncode == 0, "libtiff failed on %s: %s" % (path, result.stderr))
            got = open(raw_path, "rb").read()
            want = struct.pack(
                "<%dH" % (128 * 96), *[value for row in data for value in row]
            )
            require(got == want, "libtiff decoded %s differently" % path)
    print("LIBTIFF_INTEROP_OK")


def _have_convert():
    import shutil

    return shutil.which("convert") is not None


def cmd_verify():
    global FIXTURE_DIR
    temp_root = tempfile.mkdtemp(prefix="tiffwindow-verify-")
    FIXTURE_DIR = temp_root
    print("fixtures:", temp_root)
    failures = 0
    total = 0
    for item in CHECKS:
        total += 1
        try:
            item()
            print("PASS", item.__name__)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print("FAIL", item.__name__, "-", exc)
            traceback.print_exc()
    print("SUMMARY %d checks, %d failed" % (total, failures))
    if failures:
        raise SystemExit(1)
    print("VERIFY_OK")


def main(argv):
    command = argv[0] if argv else "verify"
    if command == "verify":
        return cmd_verify()
    if command == "setup":
        return cmd_setup()
    raise SystemExit("usage: verify.py [setup|verify]")


if __name__ == "__main__":
    main(sys.argv[1:])
