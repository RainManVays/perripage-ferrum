import pytest

from periprint.utils.page_range import parse_page_range


def test_empty_range_means_all_pages() -> None:
    assert parse_page_range("", total_pages=5) == [0, 1, 2, 3, 4]


def test_single_page() -> None:
    assert parse_page_range("3", total_pages=5) == [2]


def test_range_and_single_page_combined() -> None:
    assert parse_page_range("2-4,7", total_pages=10) == [1, 2, 3, 6]


def test_out_of_range_pages_are_clamped_away() -> None:
    assert parse_page_range("2-100", total_pages=5) == [1, 2, 3, 4]


def test_duplicates_are_removed_keeping_first_occurrence() -> None:
    assert parse_page_range("2,2-3,3", total_pages=5) == [1, 2]


def test_whitespace_around_tokens_is_tolerated() -> None:
    assert parse_page_range(" 2 - 4 , 7 ", total_pages=10) == [1, 2, 3, 6]


@pytest.mark.parametrize("bad_range", ["abc", "2-", "-3", "0", "3-1", "2,x,3", "1.5"])
def test_invalid_syntax_raises(bad_range: str) -> None:
    with pytest.raises(ValueError):
        parse_page_range(bad_range, total_pages=10)


def test_empty_tokens_between_commas_are_tolerated() -> None:
    assert parse_page_range("2,,3", total_pages=10) == [1, 2]


def test_zero_or_negative_page_numbers_are_invalid() -> None:
    with pytest.raises(ValueError):
        parse_page_range("0-3", total_pages=10)
