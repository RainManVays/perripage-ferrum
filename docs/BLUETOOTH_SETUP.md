# Bluetooth setup (PyBluez / `peripage` extra)

The `peripage` library (installed via the optional `bluetooth` extra:
`pip install -e ".[bluetooth]"`) depends on `PyBluez` for Bluetooth Classic
SPP/RFCOMM transport. `PyBluez` does **not** `pip install` cleanly on modern
Python/setuptools out of the box:

- On Python 3.11+, its legacy `setup.py` uses `use_2to3`, which recent
  `setuptools` no longer supports (`error in PyBluez setup command: use_2to3
  is invalid`).
- Even after working around that, its C extension needs the system
  `libbluetooth` headers to build (`fatal error: bluetooth/bluetooth.h: No
  such file or directory`).

## Required one-time system setup (Debian/Ubuntu/Mint)

Run this yourself — Claude/CI never installs system packages automatically:

```bash
sudo apt install python3-bluez libbluetooth-dev
```

`python3-bluez` ships a prebuilt system-Python `PyBluez`, so the venv should
be created with access to system site-packages:

```bash
python3 -m venv --system-site-packages .venv
source .venv/bin/activate
pip install -e ".[dev,bluetooth]"
```

If you'd rather build `PyBluez` from source inside an isolated venv instead
of relying on the system package, pin `setuptools<58` before installing it:

```bash
pip install "setuptools<58"
pip install pybluez
```

(the `libbluetooth-dev` headers above are still required either way).

## Verifying it worked

```bash
python -c "import peripage; print(peripage.__file__)"
```

If this imports without error, the Bluetooth transport is ready. This step
is only needed for real hardware work (Stage 0 hardware probe, Stage 2
Bluetooth integration) — the default `pip install -e ".[dev]"` (no
`bluetooth` extra) and the full test suite never require any of this.
