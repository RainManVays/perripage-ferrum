import periprint
from periprint.ui.main_window import MainWindow


def test_version_is_set() -> None:
    assert periprint.__version__ == "0.1.0"


def test_main_window_importable() -> None:
    assert callable(MainWindow)
