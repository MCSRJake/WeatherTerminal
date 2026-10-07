import pytest

from weather_terminal.formatting import format_number


@pytest.mark.parametrize("value,suffix,decimals,expected", [
    (84.5509, " °F", 1, "84.6 °F"),
    (5.74893, " mph", 1, "5.7 mph"),
    (35.2872, "%", 0, "35%"),
    (35.7872, "%", 0, "36%"),
    (184.678, "°", 0, "185°"),
    (-12.36, " °F", 1, "-12.4 °F"),
    (0, " °F", 1, "0.0 °F"),
    (0, " mph", 1, "0.0 mph"),
    (0, "%", 0, "0%"),
    (100, "%", 0, "100%"),
    (-0.0, " °F", 1, "0.0 °F"),
    (-0.04, " °F", 1, "0.0 °F"),
    (-0.4, "", 0, "0"),
    (None, " °F", 1, "Unavailable"),
    (None, "%", 0, "Unavailable"),
])
def test_display_precision_units_zero_and_missing(value, suffix, decimals, expected):
    assert format_number(value, suffix, decimals=decimals) == expected


def test_missing_placeholder_does_not_get_units_and_is_not_zero():
    assert format_number(None, "%", decimals=0, missing="—") == "—"
    assert format_number(0, "%", decimals=0, missing="—") == "0%"
