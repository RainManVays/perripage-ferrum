# Bluetooth setup (PyBluez / `peripage` extra)

The `peripage` library (installed via the optional `bluetooth` extra:
`pip install -e ".[bluetooth]"`) depends on `PyBluez` for Bluetooth Classic
SPP/RFCOMM transport.

**Verified finding: the vanilla PyPI `pybluez` source cannot be built at all
on Python 3.10+.** It's not just a `setuptools`/`use_2to3` packaging issue —
its C extension does `Py_TYPE(&sock_type) = &PyType_Type;`, assigning
directly to the `Py_TYPE()` macro, which CPython removed the ability to do
in 3.10+ (`error: lvalue required as left operand of assignment`). No
setuptools pin fixes this; the upstream project is unmaintained for modern
Python. Don't waste time trying to `pip install pybluez` from source.

**The only working path is the distro-patched package.** Debian/Ubuntu/Mint
ship a working prebuilt `python3-bluez` (currently 0.23-5.1build3) that *is*
patched for modern CPython — but it's built against the distro's own system
Python (e.g. `/usr/bin/python3.12`), not against pyenv or any other Python
you might have as your default `python3`.

## Required one-time system setup (Debian/Ubuntu/Mint)

Run this yourself — Claude/CI never installs system packages automatically:

```bash
sudo apt install python3-bluez libbluetooth-dev
```

## Creating a venv that can see it

The venv **must be created from the real system Python binary**
(`/usr/bin/python3`, not a pyenv/asdf/other shim — check with `which -a
python3` if unsure) with `--system-site-packages`, so it inherits the
apt-installed `PyBluez` .so, which is ABI-locked to that exact Python
version:

```bash
/usr/bin/python3 -m venv --system-site-packages .venv-bt
source .venv-bt/bin/activate
pip install -e ".[dev,bluetooth]"
```

This is a **separate venv from day-to-day development** (`.venv`, created
normally e.g. via pyenv, used for lint/type-check/tests — none of which
touch Bluetooth). Only use `.venv-bt` for hardware-facing work: the Stage 0
probe script, `PeripageClient`, and anything under `tests/hardware/`.

## Verifying it worked

```bash
python -c "import bluetooth; print(bluetooth.__file__)"   # PyBluez itself
python -c "import peripage; print(peripage.__file__)"     # peripage on top
```

If both import without error, the Bluetooth transport is ready.

## Second verified bug: PyBluez's send()/recv() raise "Bad address" at runtime

Even after the above is installed and `import peripage` succeeds, calling
`printer.reset()`/any command **raises `OSError: [Errno 14] Bad address`**
on this machine's `python3-bluez` 0.23-5.1build3 + BlueZ 5.72 combo — a
runtime bug in PyBluez's C-level `BluetoothSocket.send()`/`.recv()`
implementation, not a pairing or permissions problem. Confirmed by testing
in isolation:

- `sock.connect((mac, 1))` succeeds, `getpeername()`/`getsockname()` work.
- Plain `os.write(sock.fileno(), data)` on the same fd works fine.
- PyBluez's own `sock.send(data)` fails with `Bad address` on the exact same
  socket.

**Workaround** (applied in `scripts/hw_probe.py::connect()` and required in
`PeripageClient` — see the note there): after `printer.connect()`, grab the
raw fd and monkeypatch `send`/`recv` to bypass PyBluez's wrapper:

```python
fd = printer.sock.fileno()
os.set_blocking(fd, True)   # settimeout() left the fd non-blocking; undo it
printer.sock.send = lambda data: os.write(fd, data)
printer.sock.recv = lambda n: os.read(fd, n)
```

`os.set_blocking(fd, True)` is required too: `Printer.connect()` calls
`sock.settimeout(1.0)`, which puts the fd in non-blocking mode; a bare
`os.read()` on a non-blocking fd with no data ready yet raises
`BlockingIOError` instead of waiting. This does mean the patched recv loses
PyBluez's timeout behavior (it will block indefinitely on a truly wedged
device) — `PeripageClient` should implement its own `select()`-based timeout
around `os.read()` rather than relying on the removed one.

If a future `python3-bluez` release fixes this upstream, re-test without the
patch before assuming it's still needed.

## Pairing the printer

Bluetooth Classic SPP/RFCOMM needs the printer paired & trusted with the
system Bluetooth stack first (`bluetoothctl`), independent of Python:

```bash
bluetoothctl power on
bluetoothctl scan on         # look for a device named PPG_<model>_<id>,
                              # NOT the _BLE variant — peripage uses Classic SPP
bluetoothctl scan off
bluetoothctl pair   <MAC>
bluetoothctl trust  <MAC>
```
