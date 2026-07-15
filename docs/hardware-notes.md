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

## Stage 2 finding: `bluetoothctl scan on` output needs tag-aware parsing

Found live while testing the real scan dialog against this same printer:
`bluetoothctl --timeout N scan on` interleaves `[NEW] Device MAC Name` lines
(an actual device announcement) with `[CHG] Device MAC Property: Value`
lines (a property-change notification for a device bluetoothd *already*
knows about) — both share the exact textual shape `Device <MAC> <rest>`. A
parser that doesn't anchor on the `[NEW]` tag will treat property text like
`LegacyPairing: yes` as the device's name and silently clobber the real one
for any already-paired device (which is precisely the case for a printer
you've used before — the common case, not an edge case). Fixed in
`infra/bt/bluetoothctl_backend.py` by only matching lines starting with
`[NEW]`; `bluetoothctl devices` (used for already-known devices) has its own
separate parser since that output has no tags at all.

## Stage 4: real print testing found we were fighting the wrong problem

First real print through the actual app (`PrintJobManager`, not a probe
script) printed only ~1.5 lines of a short paragraph before silently
dropping the rest — no error, `done` status, chunks "successfully" sent.
Shrinking `chunk_height_px` (180 → 50 → 30) partially helped but never fully
fixed it, which pointed at the printer's own internal receive buffer
overflowing (matches a `peripage-python` GitHub issue: buffer capacity
~60 lines, excess data silently dropped, no error surfaced through the
Bluetooth stack).

**Got a real Bluetooth HCI snoop trace of the official Peripage Android app**
printing to this exact unit (`adb bugreport`, extracted
`FS/data/misc/bluetooth/logs/btsnoop_hci.log`, analyzed with `tshark`). Full
byte-level breakdown (every opcode seen, timing stats, what's confirmed vs.
speculative) is in
[`docs/bluetooth-protocol-trace-analysis.md`](bluetooth-protocol-trace-analysis.md)
— summary of the two findings that actually changed code, in order of how
confident we are in each:

1. **High confidence, applied as a real fix:** the official app sends row
   data with *no manual delay at all* — ~200-byte frames every 2-4ms, an
   entire short print's worth of data (4-6.5KB) in 50-140ms total, paced
   only by natural Bluetooth transport speed. Our own `_ROW_DELAY_SECONDS`
   was 0.05s/row — **15-30x slower**. Slow, evenly-spaced delivery appears to
   desync the printer's own receive/print state machine (plausibly a
   timeout expecting continuous input) rather than helping it — the
   opposite of what Stage 0's small-image tests suggested. Changed the
   default to 0.001s. Confirmed live: this measurably reduced data loss.
   Combined with **not artificially chunking short documents** (a chunk
   that already fits under `chunk_height_px` is sent as one continuous
   `printImage()` call, matching the official app's one-reset()-per-image
   pattern instead of one reset() per small app-chunk), a short test
   paragraph finally printed correctly end-to-end.

2. **Low confidence, applied as a labeled workaround, NOT a real fix — see
   the `TODO` in `models/printer_specs.py`:** the trace's preamble bytes
   decoded to a field reading `row_bytes=208` (1664px), not the peripage
   library's hardcoded 216 (1728px) for A40. Reducing our rendered content
   to 1664px (padded back out to the full 1728px canvas so
   `printer.printImage()`'s forced resize doesn't stretch it) fixed text
   getting clipped at the right edge. **This is a patch applied inside the
   OLD protocol** (peripage's `0x1d763000` image opcode) — the official app
   doesn't use that opcode at all. It uses a completely different,
   unreverse-engineered opcode `0x1f 00 00 d0 01 ...` that we have not
   implemented and only partially decoded. We don't actually know *why*
   1664px is the right number (only that one trace byte suggested it), and
   implementing the real `0x1f` protocol might make this workaround
   unnecessary — or reveal a differently-shaped fix. Don't extend
   `SAFE_CONTENT_WIDTH_PX` to other models by guessing; only add entries
   backed by the same kind of real evidence.

**Bonus bug found while writing a test for the width fix, not from
hardware:** `_pad_to_canvas_width`/`_apply_margins` used
`PIL.Image.new(image.mode, size, color=255)` to fill white space. For
single-channel modes ("L", "1") that's correct, but for "RGB" (the normal
case for a real photo or rendered PDF page, before our own 1-bit
normalization) a bare int color only fills the *red* channel — producing
red, which converts to a dark gray, not white. Fixed by converting to "L"
immediately after rendering, before any padding step. Caught because a new
test asserted the actual pixel value in the padded region instead of just
image dimensions — dimension-only assertions had let this slide through
earlier tests undetected.

**Open, unexplained, and still live:** the last-printed portion of a
document consistently comes out visibly darker/denser than earlier content,
across multiple different chunk configurations — most plausibly cumulative
thermal head warm-up (no way to confirm without the printer's own telemetry,
which this protocol doesn't expose) and/or our adaptive cooldown pause
(`_cooldown_seconds`) incidentally giving darker content more pre-print
settle time. Not blocking — cosmetic — but a real "print from a cold start
looks different than mid-job" characteristic worth knowing about. A
possible future experiment: a short dummy warm-up pulse before real content
starts.

**Follow-up work this section should remind a future session to do:**
Consider actually reverse-engineering and implementing the real `0x1f`
protocol observed in the trace (would need more captures — different
content sizes/darkness — to nail down the full header format and confirm
whether it lifts the current chunk-height/width-safety workarounds
entirely), rather than continuing to patch around the old `0x1d763000` path
indefinitely.

## Protocol implementation phase (2026-07-15) — decompiled official app, real hardware results

Following `docs/research.md` (APK decompilation, security review — no backdoor found) and
`docs/bluetooth-protocol-trace-analysis.md` §7 (byte-exact opcode table from the decompiled
code, not just trace guesses), implemented and tested against this same A40 several new
`PeripageClient` capabilities from `docs/printer-protocol-implementation-plan.md`:

- **`PrinterStatusListener`** (async abort/resume/paper/cover/battery status packets) — wired
  into `PrintJobManager`, connects/starts without error on real hardware, no interference with
  normal printing observed. **Confirmed live end-to-end**: printed a real multi-chunk job
  through the actual app (16 chunks, small `chunk_height_px=25` to spread the job over ~15-20s),
  physically opened the printer's cover mid-job at chunk 9/16 (~9.5s in) — the listener caught
  `cover_open` (0xFF 0x02) immediately, `PrintJobManager` paused the job
  (`JobStatus.PAUSED_ERROR`, `error_message="Принтер сообщил: cover_open"`) instead of
  continuing to blindly send chunks, and the physical printer stopped printing at the exact same
  moment the cover was opened — full agreement between the software's view and physical reality.
  Note: the trigger observed was `cover_open`, not `abort_print`/`fd01` (the one seen in the
  original Stage 4 trace) — both are handled identically (both are in
  `PAUSE_WORTHY_STATUSES`), but it's worth knowing these are two distinct real signals this
  printer sends for different physical causes, not the same event under two names.
- **`print_image_no_height_limit()`** (honest 16-bit height, one `reset()` per image instead of
  one per 255-row library-internal slice) — **confirmed working**: printed a 300-row striped
  test image as a single continuous command, no corruption/discontinuity at the old 255-row
  boundary. Some residual pixel-level banding remains, consistent with the paper-stock-quality
  observation from Stage 0, not a protocol issue.
- **`print_image_fast()`** (new `0x1f` zlib-compressed protocol) — **confirmed BROKEN**: sending
  a `zlib.compress()`-produced payload made the printer hang/power off. Recovered fully after a
  manual restart (battery reads normally, a subsequent print via the known-good
  `print_image_no_height_limit()` path worked immediately) — so this is a firmware-side rejection
  of our specific compressed byte stream, not lasting hardware damage. Root cause not
  identified; leading suspicion is a deflate-window-size mismatch (zlib.compress() defaults to a
  32KB window; an embedded decompressor may expect much less and not fail gracefully on a
  bigger one than it supports). **Do not use `print_image_fast()`** until this is root-caused —
  see the method's docstring in `infra/peripage_client.py` and the Phase 5 section of
  `printer-protocol-implementation-plan.md` for what to try next.
- `set_concentration_raw()`/`choose_paper_type()` — implemented and unit-tested.
  Real-hardware A/B comparison (concentration 2 vs 3 vs 4, 3 labeled bar-count groups printed
  back to back) — **no visible density difference** between 2/3/4 on this unit. The command is
  accepted without error at all three values (confirming the library's 0-2 clamp is needlessly
  narrow, per the decompiled app), but going past 2 isn't worth doing by default here — keep
  concentration=2 as the practical default, this bypass exists for units/firmware where it might
  matter more, not this one.
