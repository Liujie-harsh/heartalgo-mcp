import json
from pathlib import Path

import pytest

from echonet_runner import EchoNetRunner
from quality.plax_process_monitor import create_output, validate_root_pid


class FakeProcess:
    def __init__(self, running=True):
        self._running = running

    def is_running(self):
        return self._running


@pytest.mark.parametrize("pid", [0, 4, -1])
def test_monitor_rejects_reserved_or_invalid_root_pid(pid):
    with pytest.raises(ValueError, match="保留 PID"):
        validate_root_pid(pid, lambda _: FakeProcess())


def test_monitor_requires_a_running_root_pid():
    with pytest.raises(ValueError, match="未运行"):
        validate_root_pid(123, lambda _: FakeProcess(False))


def test_monitor_never_appends_to_an_existing_jsonl(tmp_path):
    target = tmp_path / "process-tree.jsonl"
    target.write_text("old", encoding="utf-8")
    with pytest.raises(FileExistsError):
        create_output(target)
    assert target.read_text(encoding="utf-8") == "old"


def test_stage_summary_is_atomic_and_replaces_same_model(tmp_path):
    model_dir = tmp_path / "echo" / "lvid"
    model_dir.mkdir(parents=True)
    timing = model_dir / "stage-timings.json"
    timing.write_text(json.dumps({"model": "lvid", "stages": {"duration_seconds": 2}}), encoding="utf-8")

    EchoNetRunner._update_plax_timing_summary(str(model_dir), str(timing))
    timing.write_text(json.dumps({"model": "lvid", "stages": {"duration_seconds": 1}}), encoding="utf-8")
    EchoNetRunner._update_plax_timing_summary(str(model_dir), str(timing))

    report = json.loads((tmp_path / "echo" / "plax-stage-timings.json").read_text(encoding="utf-8"))
    assert len(report["models"]) == 1
    assert report["models"][0]["stages"]["duration_seconds"] == 1
    assert not list((tmp_path / "echo").glob("*.tmp"))
