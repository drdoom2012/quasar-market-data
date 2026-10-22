from dataclasses import replace

import pytest

from quasar_market_data.domain.server_time import (
    DEFAULT_TIME_TRUST,
    NS_PER_MS,
    ServerTimeObservation,
    TimeRejection,
    TimeTrustConfig,
    TrustedTime,
    UntrustedTime,
    server_lower_bound,
)
from quasar_market_data.domain.timestamps import MAX_TIMESTAMP_MS, MIN_TIMESTAMP_MS
from tests.typing_helpers import untyped

# Fixture-derived observation (rtt 269.136 417 ms).
SERVER_MS = 1_792_677_032_674
SEND_NS = 18_050_152_743_666
RECEIVE_NS = 18_050_421_880_083
RECEIVE_WALL_MS = 1_792_677_032_811
OBS = ServerTimeObservation(SERVER_MS, SEND_NS, RECEIVE_NS, RECEIVE_WALL_MS)


def bound(
    observation: ServerTimeObservation | None = OBS,
    age_ns: int = 0,
    wall_elapsed_ms: int | None = None,
    previous: int | None = None,
    config: TimeTrustConfig = DEFAULT_TIME_TRUST,
) -> TrustedTime | UntrustedTime:
    """Evaluate ``observation`` ``age_ns`` after receipt; the wall clock advances consistently by default."""
    elapsed_ms = age_ns // NS_PER_MS if wall_elapsed_ms is None else wall_elapsed_ms
    return server_lower_bound(
        observation,
        now_monotonic_ns=RECEIVE_NS + age_ns,
        now_wall_ms=RECEIVE_WALL_MS + elapsed_ms,
        previous_accepted_server_time_ms=previous,
        config=config,
    )


def observation_with_rtt(rtt_ns: int) -> ServerTimeObservation:
    return ServerTimeObservation(SERVER_MS, RECEIVE_NS - rtt_ns, RECEIVE_NS, RECEIVE_WALL_MS)


def lower_bound_ms(result: TrustedTime | UntrustedTime) -> int:
    assert isinstance(result, TrustedTime), result
    return result.lower_bound_ms


def test_lower_bound_at_receive_time() -> None:
    assert bound() == TrustedTime(1_792_677_032_624)  # server time minus the 50 ms margin


def test_lower_bound_after_ten_seconds() -> None:
    # age term 9_995 ms after the 500 ppm reserve, minus the 50 ms margin
    assert bound(age_ns=10_000_000_000) == TrustedTime(1_792_677_042_619)


AGES_NS = (0, 1, 999_999, 1_000_000, 12_345_678_901, 60_000_000_000)


@pytest.mark.parametrize("margin_ms", [0, 50])
@pytest.mark.parametrize("ppm", [0, 1, 500, 999_999])
@pytest.mark.parametrize("age_ns", AGES_NS)
def test_lower_bound_satisfies_the_exact_inequality(age_ns: int, ppm: int, margin_ms: int) -> None:
    config = replace(DEFAULT_TIME_TRUST, max_drift_ppm=ppm, precision_margin_ms=margin_ms)
    result = lower_bound_ms(bound(age_ns=age_ns, config=config))
    assert result * 10**12 <= (SERVER_MS - margin_ms) * 10**12 + age_ns * (10**6 - ppm)


@pytest.mark.parametrize("age_ns", [0, 1_000_000_000, 10_000_000_000, 60_000_000_000])
@pytest.mark.parametrize("reading", ["send", "middle", "receive"])
@pytest.mark.parametrize("fraction_ns", [0, 999_999])
@pytest.mark.parametrize("rate_offset_ppm", [-500, 0, 500])
def test_lower_bound_never_exceeds_modelled_true_server_time(
    rate_offset_ppm: int, fraction_ns: int, reading: str, age_ns: int
) -> None:
    # The server clock reads S ms plus a sub-millisecond fraction at t_s (a whole-millisecond reply
    # truncates it), then runs at 1 + rate_offset_ppm / 1e6 times the local monotonic rate.
    t_s_ns = {"send": SEND_NS, "middle": (SEND_NS + RECEIVE_NS) // 2, "receive": RECEIVE_NS}[reading]
    rate = 10**6 + rate_offset_ppm
    server_at_reading_ns = SERVER_MS * NS_PER_MS + fraction_ns
    # True server time at `now`, scaled by 10**6 so the arithmetic stays exact.
    true_now_scaled = server_at_reading_ns * 10**6 + (RECEIVE_NS - t_s_ns + age_ns) * rate
    result = lower_bound_ms(bound(age_ns=age_ns))
    assert result * NS_PER_MS * 10**6 <= true_now_scaled


def test_lower_bound_is_non_decreasing_in_age() -> None:
    bounds = [lower_bound_ms(bound(age_ns=age)) for age in AGES_NS]
    assert bounds == sorted(bounds)


REJECTION_CASES = [
    pytest.param(None, 0, None, None, DEFAULT_TIME_TRUST, TimeRejection.NO_OBSERVATION, id="no_observation"),
    pytest.param(OBS, -1, 0, None, DEFAULT_TIME_TRUST, TimeRejection.MONOTONIC_REGRESSION, id="monotonic_regression"),
    pytest.param(
        observation_with_rtt(2_000_000_001),
        0,
        None,
        None,
        DEFAULT_TIME_TRUST,
        TimeRejection.RTT_TOO_LARGE,
        id="rtt_too_large",
    ),
    pytest.param(OBS, 60_000_000_001, None, None, DEFAULT_TIME_TRUST, TimeRejection.AGE_TOO_LARGE, id="age_too_large"),
    pytest.param(
        OBS,
        0,
        None,
        None,
        replace(DEFAULT_TIME_TRUST, max_uncertainty_ms=200),
        TimeRejection.UNCERTAINTY_TOO_LARGE,
        id="uncertainty_too_large",
    ),
    pytest.param(
        OBS, 0, None, SERVER_MS + 1, DEFAULT_TIME_TRUST, TimeRejection.SERVER_TIME_REGRESSED, id="server_time_regressed"
    ),
    pytest.param(
        OBS, 1_000_000_000, 376_000, None, DEFAULT_TIME_TRUST, TimeRejection.CLOCK_DIVERGENCE, id="clock_divergence"
    ),
]


@pytest.mark.parametrize(
    ("observation", "age_ns", "wall_elapsed_ms", "previous", "config", "expected"), REJECTION_CASES
)
def test_untrusted_time_is_rejected(
    observation: ServerTimeObservation | None,
    age_ns: int,
    wall_elapsed_ms: int | None,
    previous: int | None,
    config: TimeTrustConfig,
    expected: TimeRejection,
) -> None:
    assert bound(observation, age_ns, wall_elapsed_ms, previous, config) == UntrustedTime(expected)


def test_every_rejection_has_a_case() -> None:
    assert {case.id for case in REJECTION_CASES} == {member.name.lower() for member in TimeRejection}


def test_rejections_are_ordered_as_checked() -> None:
    assert list(TimeRejection) == [
        TimeRejection.NO_OBSERVATION,
        TimeRejection.MONOTONIC_REGRESSION,
        TimeRejection.RTT_TOO_LARGE,
        TimeRejection.AGE_TOO_LARGE,
        TimeRejection.UNCERTAINTY_TOO_LARGE,
        TimeRejection.SERVER_TIME_REGRESSED,
        TimeRejection.CLOCK_DIVERGENCE,
    ]


def test_check_order() -> None:
    # Round trip and age both over their limits: the first check in order reports.
    result = bound(observation_with_rtt(3_000_000_000), age_ns=70_000_000_000)
    assert result == UntrustedTime(TimeRejection.RTT_TOO_LARGE)


@pytest.mark.parametrize(
    ("observation", "age_ns", "wall_elapsed_ms", "previous", "config"),
    [
        pytest.param(observation_with_rtt(2_000_000_000), 0, None, None, DEFAULT_TIME_TRUST, id="rtt_exactly_limit"),
        pytest.param(
            OBS, 0, None, None, replace(DEFAULT_TIME_TRUST, max_rtt_ms=270), id="rtt_just_inside_custom_limit"
        ),
        pytest.param(OBS, 60_000_000_000, None, None, DEFAULT_TIME_TRUST, id="age_exactly_limit"),
        pytest.param(OBS, 0, None, SERVER_MS, DEFAULT_TIME_TRUST, id="server_time_equal_to_previous"),
        pytest.param(OBS, 1_000_000_000, 1_100, None, DEFAULT_TIME_TRUST, id="divergence_exactly_limit"),
        pytest.param(OBS, 1_000_000_000, 900, None, DEFAULT_TIME_TRUST, id="divergence_exactly_limit_negative"),
    ],
)
def test_limits_are_inclusive(
    observation: ServerTimeObservation,
    age_ns: int,
    wall_elapsed_ms: int | None,
    previous: int | None,
    config: TimeTrustConfig,
) -> None:
    assert isinstance(bound(observation, age_ns, wall_elapsed_ms, previous, config), TrustedTime)


@pytest.mark.parametrize(
    ("observation", "age_ns", "wall_elapsed_ms", "config", "expected"),
    [
        pytest.param(
            OBS, 0, None, replace(DEFAULT_TIME_TRUST, max_rtt_ms=269), TimeRejection.RTT_TOO_LARGE, id="rtt_over_custom"
        ),
        pytest.param(
            OBS,
            1_000_000_000,
            1_101,
            DEFAULT_TIME_TRUST,
            TimeRejection.CLOCK_DIVERGENCE,
            id="divergence_limit_plus_1ms",
        ),
        pytest.param(
            OBS, 1_000_000_000, 899, DEFAULT_TIME_TRUST, TimeRejection.CLOCK_DIVERGENCE, id="divergence_negative_side"
        ),
    ],
)
def test_one_step_past_a_limit_is_rejected(
    observation: ServerTimeObservation,
    age_ns: int,
    wall_elapsed_ms: int | None,
    config: TimeTrustConfig,
    expected: TimeRejection,
) -> None:
    assert bound(observation, age_ns, wall_elapsed_ms, None, config) == UntrustedTime(expected)


@pytest.mark.parametrize("name", ["now_monotonic_ns", "now_wall_ms", "previous_accepted_server_time_ms"])
@pytest.mark.parametrize("bad", [True, 1.0, "1"])
def test_wrong_argument_types_raise(name: str, bad: object) -> None:
    kwargs = {
        "now_monotonic_ns": RECEIVE_NS,
        "now_wall_ms": RECEIVE_WALL_MS,
        "previous_accepted_server_time_ms": None,
    }
    kwargs[name] = untyped(bad)
    with pytest.raises(TypeError):
        server_lower_bound(OBS, config=DEFAULT_TIME_TRUST, **untyped(kwargs))


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("now_monotonic_ns", -1),
        ("now_wall_ms", MAX_TIMESTAMP_MS + 1),
        ("now_wall_ms", MIN_TIMESTAMP_MS - 1),
        ("previous_accepted_server_time_ms", -1),
        ("previous_accepted_server_time_ms", MAX_TIMESTAMP_MS + 1),
    ],
)
def test_out_of_range_arguments_raise(name: str, bad: int) -> None:
    kwargs: dict[str, int | None] = {
        "now_monotonic_ns": RECEIVE_NS,
        "now_wall_ms": RECEIVE_WALL_MS,
        "previous_accepted_server_time_ms": None,
    }
    kwargs[name] = bad
    with pytest.raises(ValueError):
        server_lower_bound(OBS, config=DEFAULT_TIME_TRUST, **untyped(kwargs))


def test_invalid_arguments_raise_even_without_an_observation() -> None:
    with pytest.raises(TypeError):
        server_lower_bound(
            None,
            now_monotonic_ns=untyped(1.0),
            now_wall_ms=RECEIVE_WALL_MS,
            previous_accepted_server_time_ms=None,
            config=DEFAULT_TIME_TRUST,
        )


@pytest.mark.parametrize("field", ["server_time_ms", "send_monotonic_ns", "receive_monotonic_ns", "receive_wall_ms"])
@pytest.mark.parametrize("bad", [True, 1.0, "1"])
def test_observation_rejects_non_int_fields(field: str, bad: object) -> None:
    with pytest.raises(TypeError):
        replace(OBS, **{field: untyped(bad)})


def test_observation_rejects_receive_before_send() -> None:
    with pytest.raises(ValueError):
        ServerTimeObservation(SERVER_MS, RECEIVE_NS, RECEIVE_NS - 1, RECEIVE_WALL_MS)


@pytest.mark.parametrize("field", ["send_monotonic_ns", "receive_monotonic_ns"])
def test_observation_rejects_negative_monotonic(field: str) -> None:
    with pytest.raises(ValueError):
        replace(OBS, **{field: -1})


@pytest.mark.parametrize("field", ["server_time_ms", "receive_wall_ms"])
@pytest.mark.parametrize("bad", [MIN_TIMESTAMP_MS - 1, MAX_TIMESTAMP_MS + 1])
def test_observation_rejects_timestamps_out_of_range(field: str, bad: int) -> None:
    with pytest.raises(ValueError):
        replace(OBS, **{field: bad})


@pytest.mark.parametrize(
    "field",
    [
        "max_rtt_ms",
        "max_uncertainty_ms",
        "max_age_ms",
        "max_drift_ppm",
        "precision_margin_ms",
        "max_clock_divergence_ms",
    ],
)
class TestConfigValidation:
    def test_rejects_negative(self, field: str) -> None:
        with pytest.raises(ValueError):
            replace(DEFAULT_TIME_TRUST, **{field: -1})

    @pytest.mark.parametrize("bad", [True, 1.0, "1"])
    def test_rejects_non_int(self, field: str, bad: object) -> None:
        with pytest.raises(TypeError):
            replace(DEFAULT_TIME_TRUST, **{field: untyped(bad)})


def test_config_rejects_drift_of_one_million_ppm() -> None:
    with pytest.raises(ValueError):
        replace(DEFAULT_TIME_TRUST, max_drift_ppm=1_000_000)


def test_default_time_trust_matches_adr_values() -> None:
    assert DEFAULT_TIME_TRUST == TimeTrustConfig(
        max_rtt_ms=2000,
        max_uncertainty_ms=2000,
        max_age_ms=60000,
        max_drift_ppm=500,
        precision_margin_ms=50,
        max_clock_divergence_ms=100,
    )


def test_result_types_reject_wrong_field_types() -> None:
    with pytest.raises(TypeError):
        TrustedTime(untyped(1.0))
    with pytest.raises(TypeError):
        TrustedTime(untyped(True))
    with pytest.raises(TypeError):
        UntrustedTime(untyped("RTT_TOO_LARGE"))
