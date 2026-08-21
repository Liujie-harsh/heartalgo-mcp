"""Sample one inference process tree into a new, auditable JSONL file."""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path


RESERVED_WINDOWS_PIDS = {0, 4}


def validate_root_pid(pid: int, process_factory) -> object:
    if pid in RESERVED_WINDOWS_PIDS or pid < 1:
        raise ValueError(f"拒绝监控保留 PID: {pid}")
    try:
        process = process_factory(pid)
    except Exception as error:
        raise ValueError(f"根 PID 不存在或不可访问: {pid}") from error
    if not process.is_running():
        raise ValueError(f"根 PID 不存在或未运行: {pid}")
    return process


def create_output(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("x", encoding="utf-8")


def _sample(root, psutil_module) -> dict:
    processes = [root, *root.children(recursive=True)]
    rows = []
    for process in processes:
        try:
            with process.oneshot():
                io = process.io_counters()
                cpu = process.cpu_times()
                rows.append({
                    "pid": process.pid, "ppid": process.ppid(),
                    "name": process.name(), "cmdline": process.cmdline(),
                    "rss_bytes": process.memory_info().rss,
                    "cpu_user_seconds": cpu.user, "cpu_system_seconds": cpu.system,
                    "read_bytes": io.read_bytes, "write_bytes": io.write_bytes,
                })
        except (psutil_module.NoSuchProcess, psutil_module.AccessDenied):
            continue
    return {"timestamp": datetime.now(timezone.utc).isoformat(), "processes": rows}


def monitor(pid: int, output: Path, interval: float, *, task_id: str = "",
            submitted_at: str = "", task_started_at: str = "") -> int:
    if pid in RESERVED_WINDOWS_PIDS or pid < 1:
        raise ValueError(f"拒绝监控保留 PID: {pid}")
    import psutil

    root = validate_root_pid(pid, psutil.Process)
    started = time.perf_counter()
    peak_rss = total_read = total_write = total_cpu = 0
    covered_models: set[str] = set()
    interrupted = False
    with create_output(output) as stream:
        stream.write(json.dumps({"type": "start", "root_pid": pid,
                                 "monitor_pid": os.getpid(), "task_id": task_id,
                                 "submitted_at": submitted_at,
                                 "task_started_at": task_started_at,
                                 "monitor_started_at": datetime.now(timezone.utc).isoformat(),
                                 "partial": True}) + "\n")
        stream.flush()
        while root.is_running():
            sample = _sample(root, psutil)
            if not sample["processes"]:
                interrupted = root.is_running()
                break
            peak_rss = max(peak_rss, sum(item["rss_bytes"] for item in sample["processes"]))
            total_read = max(total_read, sum(item["read_bytes"] for item in sample["processes"]))
            total_write = max(total_write, sum(item["write_bytes"] for item in sample["processes"]))
            total_cpu = max(total_cpu, sum(item["cpu_user_seconds"] + item["cpu_system_seconds"]
                                           for item in sample["processes"]))
            for item in sample["processes"]:
                if "--model_weights" in item["cmdline"]:
                    index = item["cmdline"].index("--model_weights")
                    if index + 1 < len(item["cmdline"]):
                        covered_models.add(item["cmdline"][index + 1])
            stream.write(json.dumps({"type": "sample", **sample}, ensure_ascii=False) + "\n")
            stream.flush()
            time.sleep(interval)
        try:
            exit_code = root.wait(timeout=0)
        except (psutil.TimeoutExpired, psutil.NoSuchProcess):
            exit_code = None
        partial = interrupted or exit_code is None
        stream.write(json.dumps({"type": "end", "root_pid": pid,
                                 "duration_seconds": round(time.perf_counter() - started, 6),
                                 "exit_code": exit_code, "partial": partial,
                                 "stop_reason": "sampling_interrupted" if interrupted else "process_exited",
                                 "covered_models": sorted(covered_models),
                                 "peak_tree_rss_bytes": peak_rss,
                                 "tree_read_bytes": total_read,
                                 "tree_write_bytes": total_write,
                                 "tree_cpu_seconds": round(total_cpu, 6),
                                 "monitor_completed_at": datetime.now(timezone.utc).isoformat()}) + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--task-id", default="")
    parser.add_argument("--submitted-at", default="")
    parser.add_argument("--task-started-at", default="")
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval 必须大于 0")
    try:
        return monitor(args.pid, args.output, args.interval, task_id=args.task_id,
                       submitted_at=args.submitted_at, task_started_at=args.task_started_at)
    except (ValueError, FileExistsError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
