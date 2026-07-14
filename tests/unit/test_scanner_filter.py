from periprint.infra.bt.scanner import is_peripage_device


def test_matches_peripage_prefix() -> None:
    assert is_peripage_device("PeriPage+BC5F")
    assert is_peripage_device("peripage+bc5f")


def test_matches_ppg_prefix() -> None:
    assert is_peripage_device("PPG_A40_34C4")
    assert is_peripage_device("ppg_a40_34c4")


def test_excludes_ble_variant() -> None:
    assert not is_peripage_device("PPG_A40_34C4_BLE")
    assert not is_peripage_device("PeriPage+BC5F_BLE")


def test_rejects_unrelated_devices() -> None:
    assert not is_peripage_device("HAYLOU S35 ANC")
    assert not is_peripage_device("00-D2-56-52-B3-83")
