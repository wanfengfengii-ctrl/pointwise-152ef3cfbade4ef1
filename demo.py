"""Real extraction demonstration for :mod:`tiffwindow`.

The demo builds terrain-style elevation TIFFs on disk with the independent
writer in :mod:`tiffgen` (so the expected pixels are known without reading
anything back), extracts windows with :func:`tiffwindow.read`, prints the raw
integer matrices, and compares every extracted pixel with the source array.

It explicitly exercises the required dimensions:

* little-endian (II) and big-endian (MM) files;
* code-width growth across 9 -> 10 -> 11 -> 12 bits and dictionary CLEARs
  (a large, noisy raster plus a purpose-built CLEAR-heavy raster);
* horizontal differencing (Predictor 2) with wrap-around, side by side with
  Predictor 1;
* windows that span several strips, including a full-image window.

If the system has ImageMagick/libtiff, the same raster is also encoded by libtiff
and read back through the same reader as an independent cross-check.

Usage::

    python3 -I demo.py                      # built-in terrain demo
    python3 -I demo.py image.tif 120 80 20 10
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import tempfile
import tracemalloc

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import tiffgen  # noqa: E402
import tiffwindow  # noqa: E402

WIDTH, HEIGHT, ROWS_PER_STRIP = 480, 360, 23


def terrain(width, height, seed=20261115):
    """A deterministic synthetic elevation raster (metres)."""
    rng = random.Random(seed)
    rows = []
    for y in range(height):
        row = []
        for x in range(width):
            base = 850 + (x * 190) // width + (y * 260) // height
            hill = 180 if ((x - width // 2) ** 2 + (y - height // 2) ** 2) < (height // 3) ** 2 else 0
            ridge = ((x * 7 + y * 13) ^ (x * y)) & 0x7F
            row.append(base + hill + ridge + rng.randrange(-9, 10))
        rows.append(row)
    return rows


def noise(width, height, seed):
    rng = random.Random(seed)
    return [[rng.randrange(65536) for _ in range(width)] for _ in range(height)]


def show(rows, columns=21):
    """Print the extracted matrix, five columns per number field."""
    for row in rows:
        print("    " + " ".join("%5d" % value for value in row[:columns]))
        if len(row) > columns:
            print("    ... (%d columns in total)" % len(row))


def extract(path, x, y, w, h):
    return tiffwindow.read(path, x, y, w, h)


def expected(rows, x, y, w, h):
    return [[rows[yy][xx] for xx in range(x, x + w)] for yy in range(y, y + h)]


def report(label, path, rows, boxes):
    print("\n[%s] %s" % (label, path))
    ok = True
    for x, y, w, h in boxes:
        got = extract(path, x, y, w, h)
        want = expected(rows, x, y, w, h)
        match = got == want
        ok = ok and match
        print("    window x=%-3d y=%-3d w=%-3d h=%-3d  pixels match: %s"
              % (x, y, w, h, match))
        if not match:
            for j, row in enumerate(got):
                if row != want[j]:
                    print("      first differing row %d: %r != %r" % (j, row[:8], want[j][:8]))
                    break
    return ok


def encode_with_libtiff(rows, work_dir, endian, predictor, bits):
    """Encode the same pixels with libtiff (through ImageMagick), if present."""
    if not shutil.which("convert"):
        return None
    raw_path = os.path.join(work_dir, "source_%s.raw" % endian)
    with open(raw_path, "wb") as stream:
        for row in rows:
            if bits == 8:
                stream.write(bytes(row))
            else:
                stream.write(b"".join(int(value).to_bytes(2, "little") for value in row))
    twin_path = os.path.join(work_dir, "terrain_%s_p%d_libtiff.tif" % (endian, predictor))
    command = [
        "convert",
        "-size", "%dx%d" % (len(rows[0]), len(rows)),
        "-depth", str(bits),
        "gray:" + raw_path,
        "-define", "tiff:endian=msb" if endian == "MM" else "tiff:endian=lsb",
        "-compress", "lzw",
        "-define", "tiff:predictor=%d" % predictor,
        "-define", "tiff:rows-per-strip=%d" % ROWS_PER_STRIP,
        twin_path,
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        print("    libtiff twin could not be built:", result.stderr.strip())
        return None
    marker = open(twin_path, "rb").read(2)
    if marker != (b"MM" if endian == "MM" else b"II"):
        print("    libtiff ignored the endian define; skipping", endian)
        return None
    return twin_path


def external_demo(path, x, y, w, h):
    print("external demo: %s window x=%d y=%d w=%d h=%d" % (path, x, y, w, h))
    try:
        rows = tiffwindow.read(path, x, y, w, h)
    except ValueError as exc:
        print("rejected:", exc)
        return 1
    show(rows)
    print("DEMO_EXTERNAL_OK rows=%d cols=%d" % (len(rows), len(rows[0])))
    return 0


def main(argv):
    if argv:
        path = os.path.abspath(argv[0])
        if len(argv) >= 5:
            x, y, w, h = (int(argv[index]) for index in range(1, 5))
        else:
            x, y, w, h = 0, 0, 20, 10
        return external_demo(path, x, y, w, h)

    work_dir = tempfile.mkdtemp(prefix="tiffwindow-demo-")
    source_rows = terrain(WIDTH, HEIGHT)

    print("1) big-endian terrain, Predictor 2, windows crossing strips")
    mm_path = os.path.join(work_dir, "terrain_MM_p2.tif")
    mm_info = tiffgen.write_tiff(
        mm_path, source_rows, bits=16, byte_order="MM", predictor=2,
        rows_per_strip=ROWS_PER_STRIP,
    )
    print("   %dx%d, 16-bit, byte order %s, predictor 2, %d strips, %d bytes"
          % (WIDTH, HEIGHT, mm_info["byte_order"], mm_info["nstrips"], mm_info["size"]))
    x, y, w, h = 137, 40, 21, 17
    print("   strip boundaries at rows %s; window rows %d..%d span them"
          % ([index * ROWS_PER_STRIP for index in range(mm_info["nstrips"])],
             y, y + h - 1))
    tracemalloc.start()
    extracted = extract(mm_path, x, y, w, h)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print("   raw integer window x=%d y=%d w=%d h=%d:" % (x, y, w, h))
    show(extracted)
    print("   peak Python allocation during the read: %d bytes" % peak)
    print("   all values are plain Python ints: %s"
          % all(isinstance(value, int) for row in extracted for value in row))
    if extracted != expected(source_rows, x, y, w, h):
        print("DEMO_FAILED: terrain window mismatch")
        return 1
    report(
        "terrain MM predictor 2",
        mm_path,
        source_rows,
        [(0, 0, 1, 1), (WIDTH - 1, HEIGHT - 1, 1, 1), (0, 2, WIDTH, 40),
         (200, 300, 40, 23), (301, 11, 9, 100), (0, 0, WIDTH, HEIGHT)],
    )

    print("\n2) little-endian twin of the same raster, Predictor 1 and 2")
    for predictor in (1, 2):
        ii_path = os.path.join(work_dir, "terrain_II_p%d.tif" % predictor)
        tiffgen.write_tiff(
            ii_path, source_rows, bits=16, byte_order="II", predictor=predictor,
            rows_per_strip=ROWS_PER_STRIP,
        )
        report(
            "terrain II predictor %d" % predictor,
            ii_path,
            source_rows,
            [(x, y, w, h), (0, 0, WIDTH, HEIGHT), (5, 17, 200, 120)],
        )

    print("\n3) 8-bit raster (separate sample width), both byte orders")
    rows8 = [[value & 0xFF for value in row] for row in source_rows]
    for order in ("II", "MM"):
        path = os.path.join(work_dir, "terrain8_%s.tif" % order)
        tiffgen.write_tiff(path, rows8, bits=8, byte_order=order, predictor=2,
                           rows_per_strip=ROWS_PER_STRIP)
        report("8-bit %s predictor 2" % order, path, rows8,
               [(x, y, w, h), (0, 0, WIDTH, HEIGHT)])

    print("\n4) code width 9->12 and dictionary CLEARs")
    noisy = noise(320, 240, seed=99)
    noisy_path = os.path.join(work_dir, "noise_MM_p1.tif")
    noisy_info = tiffgen.write_tiff(
        noisy_path, noisy, bits=16, byte_order="MM", predictor=1, rows_per_strip=60,
    )
    for index, stats in enumerate(noisy_info["statistics"]):
        print("   strip %d: %d codes, widths %s, %d CLEAR(s), max width %d"
              % (index, stats["codes"], stats["transitions"], stats["clears"],
                 stats["max_width"]))
    noisy_ok = report("noisy MM", noisy_path, noisy,
                      [(0, 0, 320, 240), (50, 30, 80, 90), (319, 239, 1, 1)])
    if not any(stats["max_width"] == 12 for stats in noisy_info["statistics"]):
        print("   (fixture did not reach 12-bit codes)")
        noisy_ok = False

    clear_rows = [[(index * 37 + y) & 0xFFFF for index in range(160)] for y in range(120)]
    clear_path = os.path.join(work_dir, "clear_heavy_II_p2.tif")
    clear_info = tiffgen.write_tiff(
        clear_path, clear_rows, bits=16, byte_order="II", predictor=2,
        rows_per_strip=120, clear_interval=9, duplicate_clear=True,
    )
    stats = clear_info["statistics"][0]
    print("   CLEAR-heavy strip: %d CLEAR codes (including consecutive clears), widths %s"
          % (stats["clears"], stats["transitions"]))
    clear_ok = report("clear-heavy II", clear_path, clear_rows,
                      [(0, 0, 160, 120), (60, 10, 40, 90)])
    if stats["clears"] < 5:
        clear_ok = False

    print("\n5) horizontal differencing wrap-around at sample boundaries")
    edges = [[0, 65535, 1, 65534, 0, 12345, 65535, 300] * 20 for _ in range(16)]
    wrap_ok = True
    for order in ("II", "MM"):
        path = os.path.join(work_dir, "wrap_%s.tif" % order)
        tiffgen.write_tiff(path, edges, bits=16, byte_order=order, predictor=2,
                           rows_per_strip=4)
        wrap_ok = report("wrap %s" % order, path, edges, [(0, 0, 160, 16)]) and wrap_ok

    print("\n6) libtiff-encoded twins of the same rasters")
    twins_ok = True
    for order in ("MM", "II"):
        for predictor in (1, 2):
            twin = encode_with_libtiff(source_rows, work_dir, order, predictor, 16)
            if twin is None:
                print("   libtiff twin unavailable for %s predictor %d" % (order, predictor))
                continue
            got = extract(twin, x, y, w, h)
            full = extract(twin, 0, 0, WIDTH, HEIGHT)
            matches = got == expected(source_rows, x, y, w, h) and full == source_rows
            print("   %s predictor %d: window match %s, full image match %s"
                  % (order, predictor, got == expected(source_rows, x, y, w, h),
                     full == source_rows))
            twins_ok = twins_ok and matches

    print("\n7) rejection paths")
    reject_ok = True
    for bad in [(WIDTH, 0, 1, 1), (-1, 0, 1, 1), (0, 0, 0, 1), (0, 0, 1, 0)]:
        try:
            tiffwindow.read(mm_path, *bad)
        except ValueError as exc:
            print("   window %r rejected: %s" % (bad, exc))
        else:
            print("   window %r was NOT rejected" % (bad,))
            reject_ok = False

    print("\nfixtures kept in:", work_dir)
    if not (noisy_ok and clear_ok and wrap_ok and twins_ok and reject_ok):
        print("DEMO_FAILED")
        return 1
    print("DEMO_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
