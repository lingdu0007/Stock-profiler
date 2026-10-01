"""Run a pytest collection as one deterministic, disjoint shard."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

import pytest
from _pytest.config import Config
from _pytest.main import Session
from _pytest.nodes import Item


def shard_positions(item_count: int, *, shard_index: int, shard_count: int) -> tuple[int, ...]:
    """Return this shard's collection indexes using round-robin assignment."""
    if item_count < 0:
        raise ValueError("item_count must not be negative")
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    if not 0 <= shard_index < shard_count:
        raise ValueError("shard_index must be within the shard count")
    return tuple(range(shard_index, item_count, shard_count))


class _CollectionShard:
    def __init__(self, *, shard_index: int, shard_count: int) -> None:
        self._shard_index = shard_index
        self._shard_count = shard_count

    def pytest_collection_modifyitems(
        self, session: Session, config: Config, items: list[Item]
    ) -> None:
        del session
        selected_positions = set(
            shard_positions(
                len(items), shard_index=self._shard_index, shard_count=self._shard_count
            )
        )
        selected = [item for index, item in enumerate(items) if index in selected_positions]
        deselected = [item for index, item in enumerate(items) if index not in selected_positions]
        items[:] = selected
        if deselected:
            config.hook.pytest_deselected(items=deselected)


def main(arguments: Sequence[str] | None = None) -> int:
    values = list(sys.argv[1:] if arguments is None else arguments)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    try:
        separator_index = values.index("--")
    except ValueError:
        parser.error("pytest arguments must follow --")
    options = parser.parse_args(values[:separator_index])
    pytest_arguments = values[separator_index + 1 :]
    if not pytest_arguments:
        parser.error("at least one pytest argument must follow --")
    if not 0 <= options.shard_index < options.shard_count:
        parser.error("shard index must be within the positive shard count")
    return int(
        pytest.main(
            pytest_arguments,
            plugins=[
                _CollectionShard(
                    shard_index=options.shard_index,
                    shard_count=options.shard_count,
                )
            ],
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
