"""Studentization keeps calendar dependencies, directional bounds and undefined tails."""

from fractions import Fraction

from stock_profiler.modules.prospective.studentized import MetricDefinition, studentized_bounds


def test_complementary_gates_share_every_calendar_sample_and_direction() -> None:
    rows = tuple(None if index in {8, 19} else Fraction(index % 5, 4) for index in range(36))
    observed: dict[str, list[tuple[Fraction | None, ...]]] = {"lower": [], "upper": []}

    def metric(sample: tuple[Fraction | None, ...], key: str) -> Fraction:
        observed[key].append(sample)
        available = tuple(item for item in sample if item is not None)
        value = sum(available, Fraction(0)) / len(available)
        return value if key == "lower" else 1 - value

    gates = studentized_bounds(
        rows,
        {
            "lower": MetricDefinition(lambda sample: metric(sample, "lower"), "LOWER"),
            "upper": MetricDefinition(lambda sample: metric(sample, "upper"), "UPPER"),
        },
        block_lengths=(6, 9, 12),
        repetitions=99,
        inner_repetitions=49,
        alpha=Fraction(1, 20),
        seed=2291,
    )
    assert observed["lower"] == observed["upper"]
    assert any(None in sample for sample in observed["lower"])
    assert gates["lower"].bound is not None and gates["upper"].bound is not None
    assert abs(gates["upper"].bound - (1 - gates["lower"].bound)) < Fraction(1, 10**20)
    assert gates["lower"].bound == min(
        value for value in gates["lower"].block_bounds.values() if value is not None
    )
    assert gates["upper"].bound == max(
        value for value in gates["upper"].block_bounds.values() if value is not None
    )


def test_constant_perfect_results_cannot_fabricate_studentized_confidence() -> None:
    gates = studentized_bounds(
        tuple(Fraction(1) for _ in range(24)),
        {"pass": MetricDefinition(lambda sample: sum(sample, Fraction(0)) / len(sample), "LOWER")},
        block_lengths=(6, 9, 12),
        repetitions=99,
        inner_repetitions=49,
        alpha=Fraction(1, 40),
        seed=2291,
    )
    assert gates["pass"].estimate == 1
    assert gates["pass"].bound is None
    assert set(gates["pass"].block_bounds.values()) == {None}


def test_unresolved_resamples_and_unreliable_tail_order_statistics_are_explicit() -> None:
    gates = studentized_bounds(
        tuple(Fraction(index % 3) for index in range(24)),
        {
            "missing": MetricDefinition(lambda sample: None, "LOWER"),
            "tail": MetricDefinition(
                lambda sample: sum(sample, Fraction(0)) / len(sample), "LOWER"
            ),
        },
        block_lengths=(6, 9, 12),
        repetitions=99,
        inner_repetitions=49,
        alpha=Fraction(1, 10000),
        seed=2291,
    )
    assert gates["missing"].bound is None and gates["tail"].bound is None
    assert all(count > 0 for count in gates["missing"].undefined_resamples.values())
    assert gates["tail"].unreliable_tail
