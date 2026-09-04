"""Run the heartbeat-only worker process."""

from stock_profiler.entrypoints.process_runner import run_heartbeat_loop
from stock_profiler.entrypoints.worker.service import worker_snapshot

run_heartbeat_loop(worker_snapshot, expected_process_role="worker")
