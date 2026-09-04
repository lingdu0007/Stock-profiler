"""Run the heartbeat-only scheduler process."""

from stock_profiler.entrypoints.process_runner import run_heartbeat_loop
from stock_profiler.entrypoints.scheduler.service import scheduler_snapshot

run_heartbeat_loop(scheduler_snapshot, expected_process_role="scheduler")
