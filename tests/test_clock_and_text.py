from datetime import timezone

from weather_terminal.clock import SystemClock
from weather_terminal.text import plain_text


def test_system_clock_returns_utc_and_monotonic_elapsed_time():
    clock = SystemClock()
    assert clock.now().tzinfo is timezone.utc
    first = clock.monotonic()
    assert clock.monotonic() >= first


async def test_clock_sleep_seam():
    await SystemClock().sleep(0)


def test_plain_text_removes_controls_and_preserves_paragraphs():
    text = "[red]Official text[/red]\nInstruction\tvalue\x1b\x00\x9b\u202e\r"
    assert plain_text(text) == "[red]Official text[/red]\nInstruction    value"
    # Square brackets remain literal; UI additionally uses markup=False.
