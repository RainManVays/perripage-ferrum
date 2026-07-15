#!/usr/bin/env python3
"""
Stage 0 hardware probe for a real Peripage printer.

Not packaged with the app — throwaway script to confirm connectivity and
empirically measure a safe chunk_height_px before Stage 1+ bakes in
defaults. Run inside the .venv-bt environment (see docs/BLUETOOTH_SETUP.md).

Usage:
    python scripts/hw_probe.py info          <MAC>
    python scripts/hw_probe.py text           <MAC> [--text "hello"]
    python scripts/hw_probe.py chunk-test     <MAC> [--chunk-height 220] [--pause 2.0] [--chunks 3]
    python scripts/hw_probe.py heat-test      <MAC> [--values 20,40,60,80,100,120]
    python scripts/hw_probe.py break-test     <MAC> [--values 20,60,100,150,200,255]
    python scripts/hw_probe.py gradient-test  <MAC>
    python scripts/hw_probe.py break-after-image-test <MAC>
    python scripts/hw_probe.py listener-break-test <MAC>
"""

import argparse
import os
import sys
import time
from pathlib import Path

import peripage
from PIL import Image, ImageDraw, ImageFont, ImageOps

# Same font/size as infra/renderers/text_renderer.py — kept as a literal
# duplicate here (not imported from periprint) since this script
# deliberately stays dependency-free from the main package and runs in a
# separate venv (.venv-bt).
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
)
_FONT_SIZE_PX = 24

# Matches services/job_manager.py's _FINAL_TEAR_OFF_SIZE — a courtesy
# trailing feed so the user can actually tear off the receipt after a
# test, not a value under test itself. Do NOT use this for printBreak()
# calls that are the thing being measured/compared (break-test,
# break-after-image-test, listener-break-test's GAP A/B/C) — those need
# to stay at whatever size is actually being calibrated.
_FINAL_TEAR_OFF_SIZE = 255


def _load_monospace_font() -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, _FONT_SIZE_PX)
    return ImageFont.load_default(_FONT_SIZE_PX)


def connect(mac: str) -> peripage.Printer:
    printer = peripage.Printer(mac, peripage.PrinterType.A40)
    printer.connect()

    # WORKAROUND (verified on this machine's python3-bluez 0.23-5.1build3 /
    # BlueZ 5.72): PyBluez's own BluetoothSocket.send()/.recv() raise
    # OSError(14, "Bad address") on this build even though the RFCOMM
    # connection itself is fine (confirmed: os.write/os.read on the raw fd
    # work). Bypass PyBluez's C-level send/recv and talk to the fd directly.
    # settimeout() below also puts the fd in non-blocking mode, so a plain
    # os.read() would raise BlockingIOError without the set_blocking(True).
    fd = printer.sock.fileno()
    os.set_blocking(fd, True)
    printer.sock.send = lambda data: os.write(fd, data)
    printer.sock.recv = lambda n: os.read(fd, n)

    printer.reset()
    return printer


def cmd_info(args: argparse.Namespace) -> None:
    printer = connect(args.mac)
    try:
        print("connected:", printer.isConnected())
        print("row_width (native_width_px):", printer.getRowWidth())
        print("row_bytes:", printer.getRowBytes())
        print("row_characters:", printer.getRowCharacters())
        print("device name:", printer.getDeviceName())
        print("firmware:", printer.getDeviceFirmware())
        print("battery %:", printer.getDeviceBattery())
    finally:
        printer.disconnect()


def cmd_text(args: argparse.Namespace) -> None:
    printer = connect(args.mac)
    try:
        printer.setConcentration(args.concentration, wait=True)
        printer.printlnASCII(args.text)
        printer.printBreak(_FINAL_TEAR_OFF_SIZE)
    finally:
        printer.disconnect()


def _striped_test_image(width: int, height: int) -> Image.Image:
    """A test image with a mix of light/dark bands, to exercise the
    'adaptive pause on dark chunks' heuristic, not just a blank/light image."""
    img = Image.new("L", (width, height), color=255)
    draw = ImageDraw.Draw(img)
    band = height // 6 or 1
    for i in range(0, height, band * 2):
        draw.rectangle([0, i, width, min(i + band, height)], fill=0)
    draw.text((10, 10), "PeriPrint hw_probe chunk test", fill=0)
    return img


# Deliberately real prose, not solid bars: on a binary 1-bit thermal head a
# solid black square looks equally black regardless of heat, but a heat
# difference is far more likely to show up as stroke thickness/bleed/
# faintness on fine text — which is also what real documents actually are.
# One English + one Cyrillic line (RFC and most target documents are
# Russian) for representative character coverage.
_HEAT_TEST_LINES = (
    "The quick brown fox jumps over the lazy dog 0123456789",
    "Съешь ещё этих мягких французских булок, да выпей чаю",
)


def _heat_test_chunk(width: int, label: str) -> Image.Image:
    font = _load_monospace_font()
    line_height = _FONT_SIZE_PX + 6
    margin = 12
    height = margin * 2 + line_height * (1 + len(_HEAT_TEST_LINES))
    img = Image.new("L", (width, height), color=255)
    draw = ImageDraw.Draw(img)
    y = margin
    draw.text((margin, y), label, fill=0, font=font)
    y += line_height
    for line in _HEAT_TEST_LINES:
        draw.text((margin, y), line, fill=0, font=font)
        y += line_height
    return img


def cmd_heat_test(args: argparse.Namespace) -> None:
    """docs/stage5-ux-plan.md §0.3: is opcode 10ff81 (methods j()/v() in
    the decompiled app) a real quality knob, unlike concentration
    (confirmed no visible effect at 2/3/4 on this A40)? Result: also no
    visible effect (20-120 tested) — and re-decompiling afterward found
    this opcode's only real call sites are about paper positioning for
    label printing, not print density/heat, which may just explain why.
    See docs/bluetooth-protocol-trace-analysis.md §7.5. Kept for
    reference/reproducibility, not because this is still an open
    question. Prints one labeled group per --values entry, back-to-back,
    for physical side-by-side comparison of text legibility/stroke
    weight/bleed. Values are clamped to 0-120 — the decompiled app's own
    ceiling for "new" chipsets like the A40 (see
    PeripageClient.MAX_PRINT_HEAT) — this script does not attempt to probe
    past what the manufacturer's own app sends to this printer class."""
    values = [max(0, min(120, int(v))) for v in args.values.split(",")]
    printer = connect(args.mac)
    try:
        printer.setConcentration(args.concentration, wait=True)
        width = printer.getRowWidth()
        for i, value in enumerate(values):
            print(f"group {i + 1}/{len(values)}: heat={value}")
            printer.tellPrinter(bytes.fromhex("10ff81") + bytes([value]))
            chunk = _heat_test_chunk(width, f"HEAT={value}")
            printer.printImage(chunk, delay=args.delay)
            if i < len(values) - 1:
                print(f"pausing {args.pause}s to cool down...")
                time.sleep(args.pause)
        printer.printBreak(_FINAL_TEAR_OFF_SIZE)
        print("done — compare groups for visible darkness/contrast/bleed differences.")
    finally:
        printer.disconnect()


def cmd_chunk_test(args: argparse.Namespace) -> None:
    printer = connect(args.mac)
    try:
        printer.setConcentration(args.concentration, wait=True)
        width = printer.getRowWidth()
        for i in range(args.chunks):
            print(f"printing chunk {i + 1}/{args.chunks} (height={args.chunk_height}px)...")
            chunk = _striped_test_image(width, args.chunk_height)
            printer.printImage(chunk, delay=args.delay)
            if i < args.chunks - 1:
                print(f"pausing {args.pause}s to cool down...")
                time.sleep(args.pause)
        printer.printBreak(_FINAL_TEAR_OFF_SIZE)
        print("done — inspect the printout for stalls/skips/uneven darkness.")
    finally:
        printer.disconnect()


def cmd_break_test(args: argparse.Namespace) -> None:
    """Calibrates printBreak()'s `size` byte (opcode 1b4a+size, docs/
    bluetooth-protocol-trace-analysis.md §2 step "конец пачки") against an
    actual physical feed distance. Never measured on real hardware before
    — job_manager.py's trailing tear-off feed (_PAGE_BREAK_SIZE=60) was
    only ever a guess from the docstring's "break size in range (0,0xff)",
    with no unit given. Prints a labeled line, calls printBreak(size), then
    an "END n" line, for each value — measure the blank gap between the
    two labels with a ruler (mm) for each n to find what size gives a
    reliable 5-10mm tear margin."""
    values = [max(1, min(255, int(v))) for v in args.values.split(",")]
    printer = connect(args.mac)
    try:
        printer.setConcentration(2, wait=True)
        for value in values:
            printer.printlnASCII(f"BRK {value} vvv")
            printer.printBreak(value)
            printer.printlnASCII(f"END {value} ^^^")
            printer.printBreak(30)
        print("done — measure the gap between 'BRK n vvv' and 'END n ^^^' for each n (mm).")
    finally:
        printer.disconnect()


_GRADIENT_LEVELS = (0, 32, 64, 96, 128, 160, 192, 224, 255)


def _gradient_test_image(width: int) -> Image.Image:
    """Discrete labeled blocks (dark -> light) followed by one continuous
    smooth gradient bar — both run through the same auto-dithering
    printer.printImage() already uses (PIL's convert("1") without
    dither=NONE defaults to Floyd-Steinberg). Point of this test: our own
    Stage 5 investigation concluded dithering, not any printer opcode, is
    the only thing that can visibly affect "quality" on this 1-bit
    protocol — this is what that actually looks like on paper."""
    font = _load_monospace_font()
    block_h = 100
    label_h = _FONT_SIZE_PX + 10
    gradient_h = 150
    n = len(_GRADIENT_LEVELS)
    block_w = width // n
    height = label_h + block_h + gradient_h + 20
    img = Image.new("L", (width, height), color=255)
    draw = ImageDraw.Draw(img)
    for i, level in enumerate(_GRADIENT_LEVELS):
        x0 = i * block_w
        x1 = width if i == n - 1 else x0 + block_w
        draw.rectangle([x0, label_h, x1, label_h + block_h], fill=level)
        label_fill = 0 if level > 140 else 255
        draw.text((x0 + 4, 4), str(level), fill=label_fill if level > 140 else 0, font=font)
    gradient_y = label_h + block_h + 20
    for x in range(width):
        level = int(255 * x / max(1, width - 1))
        draw.line([(x, gradient_y), (x, gradient_y + gradient_h)], fill=level)
    return img


def _send_raw_image(printer: "peripage.Printer", image: Image.Image, delay: float) -> None:
    """Exact duplicate of PeripageClient.print_image_no_height_limit() +
    _pack_1bpp_for_printer() (infra/peripage_client.py) — reproduced here,
    not imported, to test the *exact* real production call sequence
    (reset() + raw legacy-protocol row send) a real print job uses, as
    opposed to the library's own printImage() the other hw_probe commands
    use. Needed to test the break-after-image hypothesis faithfully."""
    row_bytes = printer.getRowBytes()
    inverted = ImageOps.invert(image.convert("L"))
    packed = inverted.convert("1").tobytes()
    height = len(packed) // row_bytes

    printer.reset()
    header = (
        bytes.fromhex("1d763000")
        + bytes([row_bytes % 256, row_bytes // 256])
        + bytes([height % 256, height // 256])
    )
    printer.tellPrinter(header)
    for i in range(height):
        printer.tellPrinter(packed[i * row_bytes : (i + 1) * row_bytes])
        if delay:
            time.sleep(delay)


def cmd_break_after_image_test(args: argparse.Namespace) -> None:
    """Hypothesis: printBreak(60) in isolation measurably feeds ~9mm
    (confirmed via break-test 2026-07-15), yet the user reports a real
    print job ending with ~0mm gap. The one thing break-test didn't
    reproduce: a real job's trailing printBreak() is called immediately
    after the *last row of image data* sent via the raw legacy protocol
    (print_image_no_height_limit's tellPrinter() loop), not after a plain
    printlnASCII() text line. This prints the same test image twice, back
    to back: once with printBreak() called with zero delay after the last
    row (exactly like job_manager.py today), once with a short 0.3s settle
    delay first — to see whether the gap disappears specifically in the
    zero-delay case."""
    printer = connect(args.mac)
    try:
        printer.setConcentration(2, wait=True)
        width = printer.getRowWidth()
        image = _striped_test_image(width, 150)

        printer.printlnASCII("=== IMMEDIATE ===")
        _send_raw_image(printer, image, delay=0.001)
        printer.printBreak(60)  # zero delay after last row, matching job_manager.py today
        printer.printlnASCII("=== END IMMEDIATE ===")

        printer.printlnASCII("=== DELAYED 0.3s ===")
        _send_raw_image(printer, image, delay=0.001)
        time.sleep(0.3)
        printer.printBreak(60)
        printer.printlnASCII("=== END DELAYED ===")

        print("done — measure the gap in both groups. If IMMEDIATE's gap is ~0mm but")
        print("DELAYED's is ~9mm (matching break-test), the fix is a short settle delay")
        print("before the trailing printBreak() in job_manager.py.")
    finally:
        printer.disconnect()


_DOTS_PER_MM = 8  # 203dpi ≈ 7.99 dots/mm


def _reference_cube(width: int, height_mm: int = 10) -> Image.Image:
    """A solid black block exactly height_mm tall — printed between test
    conditions so the user can compare a gap's size against a known-size
    block by eye/photo, instead of lining up a ruler by hand (which turned
    out error-prone/ambiguous over chat — see the back-and-forth this
    replaced)."""
    return Image.new("L", (width, height_mm * _DOTS_PER_MM), color=0)


def cmd_listener_break_test(args: argparse.Namespace) -> None:
    """break-after-image-test (2026-07-15) showed printBreak(60) feeds a
    healthy ~12mm gap even with zero delay right after raw image data —
    refuting the "needs settle time" hypothesis. That test used a bare
    peripage.Printer though, not PeripageClient — so it never started a
    PrinterStatusListener background thread, unlike a real print job
    (PrintJobManager starts one before sending chunks and only stops it
    after the trailing printBreak(), see job_manager.py). This test uses
    the real PeripageClient/PrinterStatusListener classes to isolate
    whether the listener thread polling the same fd is what's eating the
    real job's tear-off gap. Three 10mm reference cubes bracket two gaps:

    GAP A — printBreak(60), no listener running (control, should match
            break-after-image-test's ~12mm).
    GAP B — printBreak(60), listener running and idle for ~1s first
            (isolates "does a running listener alone break the feed").
    GAP C — image chunk send + immediate printBreak(60), listener
            running throughout — the exact real-job sequence.
    """
    from periprint.infra.peripage_client import PeripageClient
    from periprint.models.enums import PrinterModel

    client = PeripageClient(mac=args.mac, model=PrinterModel.A40)
    client.connect()
    try:
        width = client._printer.getRowWidth()  # private access OK, throwaway diagnostic script
        cube = _reference_cube(width)

        def print_cube(n: int) -> None:
            client._printer.printlnASCII(f"CUBE {n} (10mm ref)")
            client.print_image_no_height_limit(cube, delay=0.001)

        print_cube(1)
        client._printer.printlnASCII("GAP A: break, no listener")
        client.print_break(60)

        print_cube(2)
        client.start_status_listening(lambda meaning, sub: None)
        time.sleep(1.0)  # let the listener settle into steady polling before testing
        client._printer.printlnASCII("GAP B: break, listener idle 1s first")
        client.print_break(60)

        print_cube(3)
        client._printer.printlnASCII("GAP C: image+break, listener running (real job sequence)")
        chunk = _striped_test_image(width, 100)
        client.print_image_no_height_limit(chunk, delay=0.001)
        client.print_break(60)  # zero delay, matching job_manager.py exactly
        client.stop_status_listening()

        print_cube(4)
        client.print_break(_FINAL_TEAR_OFF_SIZE)  # courtesy tear-off, not part of the A/B/C test
        print("done — compare gap A/B/C against the 10mm cube height, and against each other.")
    finally:
        client.disconnect()


def cmd_gradient_test(args: argparse.Namespace) -> None:
    """9 labeled gray blocks (0=black..255=white) plus a smooth continuous
    gradient bar, dithered by printer.printImage()'s default Floyd-
    Steinberg — see docs/stage5-ux-plan.md §0.3 for why this, not a
    printer opcode, is the real "quality" lever on this 1-bit protocol."""
    printer = connect(args.mac)
    try:
        printer.setConcentration(args.concentration, wait=True)
        width = printer.getRowWidth()
        image = _gradient_test_image(width)
        printer.printImage(image, delay=args.delay)
        printer.printBreak(_FINAL_TEAR_OFF_SIZE)
        print("done — inspect how continuous tone renders as a dithered dot pattern.")
    finally:
        printer.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_info = sub.add_parser("info", help="Connect, print device info, disconnect. No paper used.")
    p_info.add_argument("mac")
    p_info.set_defaults(func=cmd_info)

    p_text = sub.add_parser("text", help="Print one short ASCII line. Uses paper.")
    p_text.add_argument("mac")
    p_text.add_argument("--text", default="PeriPrint hw_probe: hello from Stage 0")
    p_text.add_argument("--concentration", type=int, default=2, choices=[0, 1, 2])
    p_text.set_defaults(func=cmd_text)

    p_chunk = sub.add_parser(
        "chunk-test", help="Print N striped chunks with a pause between them. Uses paper."
    )
    p_chunk.add_argument("mac")
    p_chunk.add_argument("--chunk-height", type=int, default=220)
    p_chunk.add_argument("--pause", type=float, default=2.0)
    p_chunk.add_argument("--chunks", type=int, default=3)
    # Defaults per docs/hardware-notes.md empirical findings on the real
    # ALD-Y200/A40 unit: concentration=2, delay=0.05 gave the best density.
    p_chunk.add_argument("--concentration", type=int, default=2, choices=[0, 1, 2])
    p_chunk.add_argument("--delay", type=float, default=0.05)
    p_chunk.set_defaults(func=cmd_chunk_test)

    p_heat = sub.add_parser(
        "heat-test",
        help="Print one labeled group per heat value, back-to-back. Uses paper.",
    )
    p_heat.add_argument("mac")
    p_heat.add_argument(
        "--values", default="20,40,60,80,100,120", help="comma-separated, each 0-120"
    )
    p_heat.add_argument("--pause", type=float, default=2.0)
    p_heat.add_argument("--concentration", type=int, default=2, choices=[0, 1, 2])
    p_heat.add_argument("--delay", type=float, default=0.001)
    p_heat.set_defaults(func=cmd_heat_test)

    p_break = sub.add_parser(
        "break-test", help="Calibrate printBreak() size vs physical feed distance. Uses paper."
    )
    p_break.add_argument("mac")
    p_break.add_argument(
        "--values", default="20,60,100,150,200,255", help="comma-separated, each 1-255"
    )
    p_break.set_defaults(func=cmd_break_test)

    p_gradient = sub.add_parser(
        "gradient-test",
        help="Print dithered gray blocks + a continuous gradient bar. Uses paper.",
    )
    p_gradient.add_argument("mac")
    p_gradient.add_argument("--concentration", type=int, default=2, choices=[0, 1, 2])
    p_gradient.add_argument("--delay", type=float, default=0.001)
    p_gradient.set_defaults(func=cmd_gradient_test)

    p_break_img = sub.add_parser(
        "break-after-image-test",
        help="Test whether printBreak() right after raw image data loses its feed. Uses paper.",
    )
    p_break_img.add_argument("mac")
    p_break_img.set_defaults(func=cmd_break_after_image_test)

    p_listener_break = sub.add_parser(
        "listener-break-test",
        help="Test printBreak() with PrinterStatusListener running (real job_manager"
        " sequence, using real PeripageClient). Uses paper.",
    )
    p_listener_break.add_argument("mac")
    p_listener_break.set_defaults(func=cmd_listener_break_test)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
