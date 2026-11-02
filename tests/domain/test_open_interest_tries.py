import random

import pytest

from quasar_market_data.domain.open_interest_tries import MAX_TRIES, OpenInterestTryBook
from quasar_market_data.domain.timeframe import Timeframe

P = Timeframe("5m")
STEP = P.milliseconds
BASE = 1_792_677_000_000 // STEP * STEP
HOUR = 3_600_000
SYM = "S/USDT:USDT"


def slot(n: int) -> int:
    return BASE + n * STEP


def test_a_fresh_slot_is_available() -> None:
    assert OpenInterestTryBook(P).available(SYM, [slot(1)], 0) == (slot(1),)


def test_cooldowns_are_exact_and_the_third_try_is_final() -> None:
    book = OpenInterestTryBook(P)
    book.record_tries(SYM, [slot(1)], 1_000)
    assert book.available(SYM, [slot(1)], 1_000 + HOUR - 1) == ()
    assert book.available(SYM, [slot(1)], 1_000 + HOUR) == (slot(1),)
    book.record_tries(SYM, [slot(1)], 5_000_000)
    assert book.available(SYM, [slot(1)], 5_000_000 + 6 * HOUR - 1) == ()
    assert book.available(SYM, [slot(1)], 5_000_000 + 6 * HOUR) == (slot(1),)
    book.record_tries(SYM, [slot(1)], 40_000_000)
    assert book.available(SYM, [slot(1)], 40_000_000 + 100 * 24 * HOUR) == ()
    assert book.exhausted(SYM) == 1 and MAX_TRIES == 3


def test_a_cooling_slot_in_a_page_keeps_its_count_and_cooldown() -> None:
    book = OpenInterestTryBook(P)
    book.record_tries(SYM, [slot(2)], 0)  # slot 2 tried once at t = 0
    planned = book.available(SYM, [slot(1), slot(2), slot(3)], 10)  # a page spans 1..3; 2 is cooling
    assert planned == (slot(1), slot(3))
    book.record_tries(SYM, planned, 10)
    assert book.available(SYM, [slot(2)], HOUR) == (slot(2),)  # still on its first cooldown (from t = 0)
    assert book.available(SYM, [slot(1), slot(3)], HOUR) == ()  # theirs run from t = 10


def test_forget_present_starts_a_returning_slot_fresh() -> None:
    book = OpenInterestTryBook(P)
    book.record_tries(SYM, [slot(1), slot(2)], 0)
    book.forget_present(SYM, [slot(2)])
    assert book.available(SYM, [slot(1)], 1) == (slot(1),)
    assert book.available(SYM, [slot(2)], 1) == ()


def test_a_new_book_is_a_restart() -> None:
    book = OpenInterestTryBook(P)
    for _ in range(3):
        book.record_tries(SYM, [slot(1)], 0)
    assert book.available(SYM, [slot(1)], 10**12) == ()
    assert OpenInterestTryBook(P).available(SYM, [slot(1)], 0) == (slot(1),)


def test_8352_slots_tried_together_are_one_run() -> None:
    book = OpenInterestTryBook(P)
    slots = [slot(n) for n in range(8352)]
    book.record_tries(SYM, slots, 0)
    assert book.run_count(SYM) == 1
    book.record_tries(SYM, slots[:100], 10)
    assert book.run_count(SYM) == 2


def test_exhausted_counts_are_per_symbol_and_only_nonzero() -> None:
    book = OpenInterestTryBook(P)
    for _ in range(3):
        book.record_tries("A/USDT:USDT", [slot(1), slot(2)], 0)
    book.record_tries("B/USDT:USDT", [slot(1)], 0)
    assert book.exhausted_counts() == {"A/USDT:USDT": 2}


def test_unaligned_or_mistyped_input_is_rejected() -> None:
    book = OpenInterestTryBook(P)
    with pytest.raises(ValueError):
        book.record_tries(SYM, [slot(1) + 1], 0)
    with pytest.raises(TypeError):
        book.available(SYM, [float(slot(1))], 0)  # type: ignore[list-item]
    with pytest.raises(TypeError):
        book.available(SYM, [slot(1)], 1.5)  # type: ignore[arg-type]


class Naive:
    def __init__(self) -> None:
        self.state: dict[tuple[str, int], tuple[int, int | None]] = {}

    def available(self, symbol: str, slots: list[int], now: int) -> tuple[int, ...]:
        out = []
        for s in slots:
            entry = self.state.get((symbol, s))
            if entry is None or (entry[0] < 3 and entry[1] is not None and now >= entry[1]):
                out.append(s)
        return tuple(out)

    def record(self, symbol: str, slots: list[int], now: int) -> None:
        for s in slots:
            tries = self.state.get((symbol, s), (0, 0))[0] + 1
            self.state[(symbol, s)] = (tries, None if tries >= 3 else now + (HOUR if tries == 1 else 6 * HOUR))

    def forget(self, symbol: str, holes: set[int]) -> None:
        for key in [k for k in self.state if k[0] == symbol and k[1] not in holes]:
            del self.state[key]

    def exhausted(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for (symbol, _), (tries, _) in self.state.items():
            if tries >= 3:
                counts[symbol] = counts.get(symbol, 0) + 1
        return dict(sorted(counts.items()))


@pytest.mark.parametrize("seed", range(40))
def test_the_book_matches_a_naive_model(seed: int) -> None:
    rng = random.Random(seed)
    symbols = ["A/USDT:USDT", "B/USDT:USDT", "C/USDT:USDT"]
    book, model = OpenInterestTryBook(P), Naive()
    now = 0
    for _ in range(200):
        now += rng.choice([0, 1, 1_000, HOUR - 1, HOUR, 6 * HOUR])
        symbol = rng.choice(symbols)
        chosen = sorted(rng.sample(range(60), rng.randint(0, 25)))
        slots = [slot(n) for n in chosen]
        action = rng.choice(["record", "record", "forget", "check"])
        if action == "record":
            planned = list(book.available(symbol, slots, now))
            assert planned == list(model.available(symbol, slots, now))
            book.record_tries(symbol, planned, now)
            model.record(symbol, planned, now)
        elif action == "forget":
            book.forget_present(symbol, slots)
            model.forget(symbol, set(slots))
        every = [slot(n) for n in range(60)]
        for s in symbols:
            assert book.available(s, every, now) == model.available(s, every, now)
        assert dict(book.exhausted_counts()) == model.exhausted()
