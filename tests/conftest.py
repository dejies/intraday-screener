"""Shared test setup."""
import pytest

from screener import market_calendar


@pytest.fixture(autouse=True)
def no_real_holidays():
    """Tests simulate trading on today's date (the demo does too), so the real NSE holiday list must not
    close the simulated market. Holiday tests install their own calendar."""
    market_calendar.set_calendar(market_calendar.Calendar())
    yield
    market_calendar.set_calendar(None)
