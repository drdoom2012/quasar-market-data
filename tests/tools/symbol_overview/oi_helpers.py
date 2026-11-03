"""Shared constants and builders for the open interest tests of the symbol overview tool (OI-6b)."""

from tests.tools.symbol_overview.helpers import T0
from tools.symbol_overview.model import TF_MS
from tools.symbol_overview.window import open_interest_window, window_for

WINDOW = window_for(T0)  # the candle window [S, E)
OI_WINDOW = open_interest_window(WINDOW)  # [S + tf, E + tf): slot T is a period END
S1 = OI_WINDOW.start  # S'
E1 = OI_WINDOW.end  # E'


def slot(n: int) -> int:
    """The ``n``-th slot of the open interest window (``S' + n x tf``)."""
    return S1 + n * TF_MS
