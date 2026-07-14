#!/usr/bin/env python3
"""
Stage 0 hardware probe for a real Peripage printer.

Not packaged with the app — throwaway script to confirm connectivity and
empirically measure a safe chunk_height_px before Stage 1+ bakes in
defaults. Run inside the .venv-bt environment (see docs/BLUETOOTH_SETUP.md).

Usage:
    python scripts/hw_probe.py info      <MAC>
    python scripts/hw_probe.py text       <MAC> [--text "hello"]
    python scripts/hw_probe.py chunk-test <MAC> [--chunk-height 220] [--pause 2.0] [--chunks 3]
"""

import argparse
import os
import sys
import time

import peripage
from PIL import Image, ImageDraw


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
        printer.printBreak(60)
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
        printer.printBreak(60)
        print("done — inspect the printout for stalls/skips/uneven darkness.")
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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
