# Stage 0 hardware notes — real ALD-Y200 / Peripage A40

Probed with `scripts/hw_probe.py` against a real device paired over Bluetooth
Classic SPP.

## Status: Stage 0 closed (2026-07-14)

Core connectivity, the two showstopper software bugs, and a working
concentration/delay starting point are confirmed — enough to unblock Stage 1
(MVP skeleton, no real BT) and Stage 2 (`PeripageClient`, which must carry
forward the `os.write`/`os.read` workaround below). Inter-chunk cooldown
pause calibration for long multi-chunk documents is explicitly deferred to
Stage 4, when a real `PrintJobManager` queue exists to test against — see
the note at the bottom of this file.

## Device identification

- Bluetooth name: `PPG_A40_34C4` (Classic SPP; a `_BLE` variant also
  advertises but is not used by `peripage`).
- MAC: `28:D4:1E:01:34:C4` (this specific unit — store as the printer
  profile's `mac`, not a constant).
- Model: **A40** (`peripage.PrinterType.A40`).
- Firmware: `V1.4.5_SD`.
- Confirmed `native_width_px` = **1728** (matches the library's hardcoded
  `PrinterType.A40.spec.row_width` — no need to measure this independently
  per-unit, it's a model constant).
- `row_bytes` = 216, `row_characters` = 144 (ASCII mode).

## Runtime bugs found (see docs/BLUETOOTH_SETUP.md for full detail)

1. Vanilla PyPI `pybluez` doesn't compile on Python 3.10+ at all (CPython
   `Py_TYPE` API change) — must use the distro-patched `python3-bluez` apt
   package via a `--system-site-packages` venv built from the real system
   Python.
2. Even the distro-patched `PyBluez` has a **runtime** bug: `send()`/`recv()`
   on the `BluetoothSocket` raise `OSError: [Errno 14] Bad address`. Fixed by
   bypassing PyBluez's C wrapper and talking to the raw fd via
   `os.write`/`os.read` (see `scripts/hw_probe.py::connect()`). This must be
   baked into `PeripageClient` (Stage 2), not left as a probe-script-only
   hack.

## Paper orientation (important, non-software gotcha)

First few print attempts (ASCII text and image) produced **zero visible
output while the printer still fed the correct amount of paper** — looked
exactly like a software/protocol bug. Root cause: the thermal-sensitive side
of the roll was facing the wrong way. This was true even though the roll was
believed to be in its original factory orientation — don't trust that
assumption; if paper feeds correctly but nothing appears, flip the roll
before debugging the protocol/software further.

## Row-send `delay` / concentration calibration (single-chunk, printImage())

Confirmed real hardware, this printer produces gappy/"broken pixel" solid
black bands regardless of `delay` alone (tried 0.05, 0.002 — same gappy
result at `concentration=1`). What actually mattered was `concentration`:

- `concentration=1, delay=0.01` — noticeably gappy.
- `concentration=2, delay=0.01` — slightly denser than above.
- **`concentration=2, delay=0.05` — best result so far, most acceptable
  density** (user's call after a 4-way side-by-side comparison print).
- `concentration=2, delay=0.10` — bars visibly *thinner* than the other
  three, not just gappy. Slower per-row delay does not mean "safer"/denser
  here; on this unit it made output worse. Don't assume higher delay ==
  higher quality.

Some residual banding/gaps remain even at the best setting — may be a
thermal-paper-stock quality issue rather than something fixable in software
(user's own observation). Treat `concentration=2, delay≈0.05` as the
starting default for this printer model/unit in `PrinterProfile`, not as a
guarantee of perfect output — real-world print quality on this hardware has
a ceiling we don't fully control.

**Not yet calibrated:** the *inter-chunk* cooldown pause for a long
multi-chunk document (the actual overheat-prevention scenario the whole app
exists for). The above tests were single `printImage()` calls only (40-150px
each print, disconnect between most of them) — a different concern from
sending many consecutive ~150-220px chunks back-to-back with short pauses
over a full-length document. Needs its own test pass before Stage 4.
