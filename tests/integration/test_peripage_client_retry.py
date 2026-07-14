import pytest

from periprint.infra import peripage_client as peripage_client_module
from periprint.infra.peripage_client import PeripageClient, PeripageConnectionError
from periprint.models.enums import PrinterModel
from tests.integration.fakes.fake_raw_printer import FakeRawPrinter


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(peripage_client_module.time, "sleep", lambda seconds: None)


def _make_client(fake: FakeRawPrinter) -> PeripageClient:
    return PeripageClient(
        mac="28:D4:1E:01:34:C4",
        model=PrinterModel.A40,
        printer_factory=lambda mac, model: fake,
    )


def test_connect_resets_and_sets_concentration() -> None:
    fake = FakeRawPrinter()
    client = _make_client(fake)

    client.connect()

    assert fake.connect_calls == 1
    assert fake.reset_calls == 1
    assert fake.set_concentration_calls == [(2, True)]
    assert client.is_connected()


def test_reconnect_reruns_reset_and_concentration() -> None:
    fake = FakeRawPrinter()
    client = _make_client(fake)

    client.connect()
    client.reconnect()

    assert fake.reset_calls == 2
    assert len(fake.set_concentration_calls) == 2


def test_reconnect_retries_then_succeeds() -> None:
    fake = FakeRawPrinter(fail_connects=2)
    client = _make_client(fake)

    client.reconnect()

    assert fake.connect_calls == 3
    assert client.is_connected()


def test_reconnect_gives_up_after_max_attempts() -> None:
    fake = FakeRawPrinter(fail_connects=99)
    client = _make_client(fake)

    with pytest.raises(PeripageConnectionError):
        client.reconnect()

    assert fake.connect_calls == 3


def test_disconnect_clears_connection_state() -> None:
    fake = FakeRawPrinter()
    client = _make_client(fake)
    client.connect()

    client.disconnect()

    assert fake.disconnect_calls == 1
    assert not client.is_connected()


def test_print_image_requires_connection() -> None:
    fake = FakeRawPrinter()
    client = _make_client(fake)

    with pytest.raises(RuntimeError):
        client.print_image(image=None)
