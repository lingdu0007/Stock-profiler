from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "pytest_shard.py"
_SCRIPT_SPEC = spec_from_file_location("pytest_shard", _SCRIPT_PATH)
if _SCRIPT_SPEC is None or _SCRIPT_SPEC.loader is None:
    raise RuntimeError("could not load the pytest sharding script")
_SCRIPT = module_from_spec(_SCRIPT_SPEC)
_SCRIPT_SPEC.loader.exec_module(_SCRIPT)
shard_positions = _SCRIPT.shard_positions


def test_pytest_shards_assign_every_collected_item_exactly_once() -> None:
    item_count = 113
    shard_count = 4

    assignments = tuple(
        shard_positions(item_count, shard_index=index, shard_count=shard_count)
        for index in range(shard_count)
    )

    assert len({position for shard in assignments for position in shard}) == item_count
    assert sorted(position for shard in assignments for position in shard) == list(
        range(item_count)
    )
    assert max(map(len, assignments)) - min(map(len, assignments)) <= 1
