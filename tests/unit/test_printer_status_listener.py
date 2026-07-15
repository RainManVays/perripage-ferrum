import socket
import time

from periprint.infra.printer_status_listener import (
    PAUSE_WORTHY_STATUSES,
    STATUS_MEANINGS,
    PrinterStatusListener,
)


def _wait_for(predicate, timeout: float = 2.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_status_meanings_cover_known_wire_bytes() -> None:
    # (0xFD, 1)/(0xFD, 2) are the only pairs actually seen on the wire in a
    # real capture (docs/bluetooth-protocol-trace-analysis.md §4); the rest
    # are confirmed by decompiling the official app only.
    assert STATUS_MEANINGS[(0xFD, 1)] == "abort_print"
    assert STATUS_MEANINGS[(0xFD, 2)] == "resume_print"


def test_pause_worthy_excludes_resolutions() -> None:
    assert "abort_print" in PAUSE_WORTHY_STATUSES
    assert "out_of_paper" in PAUSE_WORTHY_STATUSES
    # Resolutions/acks, not problems — must not pause a job.
    assert "cover_closed" not in PAUSE_WORTHY_STATUSES
    assert "resume_print" not in PAUSE_WORTHY_STATUSES


def test_listener_delivers_abort_print_event() -> None:
    local_sock, remote_sock = socket.socketpair()
    events: list[tuple[str, int]] = []
    listener = PrinterStatusListener(local_sock, lambda meaning, sub: events.append((meaning, sub)))
    listener.start()
    try:
        remote_sock.sendall(bytes([0xFD, 0x01]))
        assert _wait_for(lambda: events == [("abort_print", 1)])
    finally:
        listener.stop()
        local_sock.close()
        remote_sock.close()


def test_listener_delivers_paper_type_mismatch_with_raw_subcode() -> None:
    local_sock, remote_sock = socket.socketpair()
    events: list[tuple[str, int]] = []
    listener = PrinterStatusListener(local_sock, lambda meaning, sub: events.append((meaning, sub)))
    listener.start()
    try:
        remote_sock.sendall(bytes([0xFE, 0x02]))
        assert _wait_for(lambda: events == [("paper_type_mismatch", 2)])
    finally:
        listener.stop()
        local_sock.close()
        remote_sock.close()


def test_listener_ignores_unrecognized_pairs_without_crashing() -> None:
    local_sock, remote_sock = socket.socketpair()
    events: list[tuple[str, int]] = []
    listener = PrinterStatusListener(local_sock, lambda meaning, sub: events.append((meaning, sub)))
    listener.start()
    try:
        remote_sock.sendall(bytes([0x99, 0x99]))
        time.sleep(0.3)
        assert events == []
    finally:
        listener.stop()
        local_sock.close()
        remote_sock.close()


def test_stop_terminates_the_background_thread() -> None:
    local_sock, remote_sock = socket.socketpair()
    listener = PrinterStatusListener(local_sock, lambda meaning, sub: None)
    listener.start()
    listener.stop()
    assert not listener._thread.is_alive()
    local_sock.close()
    remote_sock.close()
