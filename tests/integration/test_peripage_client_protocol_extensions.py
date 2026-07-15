import socket
import time
import zlib

import PIL.Image
import pytest

from periprint.infra.peripage_client import PeripageClient, _pack_1bpp_for_printer
from periprint.models.enums import PaperType, PrinterModel
from tests.integration.fakes.fake_raw_printer import FakeRawPrinter


def _connected_client(fake: FakeRawPrinter) -> PeripageClient:
    client = PeripageClient(
        mac="AA:BB:CC:DD:EE:FF",
        model=PrinterModel.A40,
        printer_factory=lambda mac, model: fake,
    )
    client.connect()
    fake.tell_printer_calls.clear()  # drop the reset()/setConcentration() from connect()
    return client


def _wait_for(predicate, timeout: float = 2.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# -- _pack_1bpp_for_printer: the inversion is the single easiest detail to
# get backwards and silently print a negative image on real paper. --


def test_pack_1bpp_fires_dots_for_content_pixels() -> None:
    # Our pipeline's normalize_to_1bit (infra/renderers/base.py) leaves
    # background=on(255)/content=off(0), no inversion. The wire format
    # wants the opposite: bit=1 means "fire this dot".
    content_image = PIL.Image.new("1", (16, 2), color=0)  # all "content" per our convention
    assert _pack_1bpp_for_printer(content_image) == b"\xff\xff\xff\xff"


def test_pack_1bpp_leaves_background_pixels_blank() -> None:
    background_image = PIL.Image.new("1", (16, 2), color=1)  # all "background"
    assert _pack_1bpp_for_printer(background_image) == b"\x00\x00\x00\x00"


# -- Phase 2: set_concentration_raw --


def test_set_concentration_raw_bypasses_library_clamp() -> None:
    fake = FakeRawPrinter()
    client = _connected_client(fake)

    client.set_concentration_raw(4)

    assert fake.tell_printer_calls == [bytes.fromhex("10ff1000") + bytes([4])]


def test_set_concentration_raw_requires_connection() -> None:
    client = PeripageClient(
        mac="AA:BB", model=PrinterModel.A40, printer_factory=lambda mac, model: FakeRawPrinter()
    )
    with pytest.raises(RuntimeError):
        client.set_concentration_raw(3)


# -- Phase 3: choose_paper_type --


def test_choose_paper_type_sends_correct_opcode() -> None:
    fake = FakeRawPrinter()
    client = _connected_client(fake)

    client.choose_paper_type(PaperType.CONTINUOUS_ROLL)

    assert fake.tell_printer_calls == [bytes.fromhex("10ff1003") + bytes([2])]


# -- Phase 4: print_image_no_height_limit --


def test_print_image_no_height_limit_uses_a_single_reset_above_255_rows() -> None:
    fake = FakeRawPrinter(row_bytes=2)  # 16px-wide test printer
    client = _connected_client(fake)
    fake.reset_calls = 0

    image = PIL.Image.new("1", (16, 300), color=0)  # 300 rows > library's 255-row slice limit
    client.print_image_no_height_limit(image, delay=0)

    assert fake.reset_calls == 1  # one reset for the whole image, not one per 255-row slice


def test_print_image_no_height_limit_header_and_row_bytes() -> None:
    fake = FakeRawPrinter(row_bytes=2)
    client = _connected_client(fake)

    image = PIL.Image.new("1", (16, 300), color=0)
    client.print_image_no_height_limit(image, delay=0)

    header = fake.tell_printer_calls[0]
    assert header[:4] == bytes.fromhex("1d763000")
    assert header[4] == 2 % 256
    assert header[5] == 2 // 256
    assert header[6] == 300 % 256
    assert header[7] == 300 // 256

    row_sends = fake.tell_printer_calls[1:]
    assert len(row_sends) == 300
    assert all(row == b"\xff\xff" for row in row_sends)


# -- Phase 5: print_image_fast (experimental 0x1f protocol) --


def test_print_image_fast_header_fields() -> None:
    fake = FakeRawPrinter(row_bytes=2)
    client = _connected_client(fake)

    image = PIL.Image.new("1", (16, 5), color=0)
    client.print_image_fast(image)

    assert len(fake.tell_printer_calls) == 1
    sent = fake.tell_printer_calls[0]
    assert sent[0] == 0x1F
    assert sent[1] == 0x00
    assert int.from_bytes(sent[2:4], "big") == 2  # row_bytes
    assert int.from_bytes(sent[4:6], "big") == 5  # height
    payload_len = int.from_bytes(sent[6:10], "big")
    assert payload_len == len(sent) - 10


def test_print_image_fast_payload_decompresses_to_expected_bits() -> None:
    fake = FakeRawPrinter(row_bytes=2)
    client = _connected_client(fake)

    image = PIL.Image.new("1", (16, 5), color=0)
    client.print_image_fast(image)

    compressed_payload = fake.tell_printer_calls[0][10:]
    # We strip zlib's 2-byte header before sending (matching the official
    # app — docs/bluetooth-protocol-trace-analysis.md §3.2), leaving a raw
    # deflate stream with a trailing 4-byte Adler-32 that isn't part of the
    # deflate data itself. wbits=-15 decodes raw deflate with no
    # header/trailer expected, so the extra bytes just land in
    # unused_data rather than causing an error.
    decompressor = zlib.decompressobj(-15)
    decompressed = decompressor.decompress(compressed_payload) + decompressor.flush()

    assert decompressed == b"\xff\xff" * 5


# -- Status listening wiring through PeripageClient --


def test_status_listening_delivers_events_end_to_end() -> None:
    local_sock, remote_sock = socket.socketpair()
    fake = FakeRawPrinter()
    fake.sock = local_sock
    client = PeripageClient(
        mac="AA:BB", model=PrinterModel.A40, printer_factory=lambda mac, model: fake
    )
    client.connect()

    events: list[tuple[str, int]] = []
    try:
        client.start_status_listening(lambda meaning, sub: events.append((meaning, sub)))
        remote_sock.sendall(bytes([0xFD, 0x01]))
        assert _wait_for(lambda: events == [("abort_print", 1)])
    finally:
        client.stop_status_listening()
        remote_sock.close()


def test_stop_status_listening_is_safe_when_never_started() -> None:
    fake = FakeRawPrinter()
    client = _connected_client(fake)
    client.stop_status_listening()  # must not raise


def test_disconnect_stops_status_listening() -> None:
    local_sock, remote_sock = socket.socketpair()
    fake = FakeRawPrinter()
    fake.sock = local_sock
    client = PeripageClient(
        mac="AA:BB", model=PrinterModel.A40, printer_factory=lambda mac, model: fake
    )
    client.connect()
    client.start_status_listening(lambda meaning, sub: None)

    client.disconnect()

    assert client._status_listener is None
    remote_sock.close()
