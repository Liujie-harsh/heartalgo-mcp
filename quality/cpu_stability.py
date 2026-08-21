"""串行 CPU 推理稳定性和进程树内存基准。"""

from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psutil

from algorithm_errors import AlgorithmError, to_public_error
from algorithm_version import resolve_algorithm_version
from metric_catalog import VIEW_METRICS
from task_models import ImgItem


@dataclass(frozen=True)
class BenchmarkScenario:
    name: str
    images: tuple[ImgItem, ...]


class BenchmarkOutputError(AlgorithmError):
    code = "BENCHMARK_OUTPUT_INVALID"
    default_message = "基准场景未产生完整模型结果"


class BenchmarkLimitError(AlgorithmError):
    code = "BENCHMARK_LIMIT_EXCEEDED"
    default_message = "基准场景超过批准的资源或时延上限"


class _PeakProcessTreeRss:
    def __init__(self, interval_seconds: float = 0.05) -> None:
        self.interval_seconds = interval_seconds
        self.peak_bytes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample(self) -> None:
        try:
            root = psutil.Process(os.getpid())
            processes = [root, *root.children(recursive=True)]
            total = sum(process.memory_info().rss for process in processes)
            self.peak_bytes = max(self.peak_bytes, total)
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            return

    def _monitor(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._sample()

    def __enter__(self):
        self._sample()
        self._thread = threading.Thread(
            target=self._monitor, name="heart-algo-memory-probe", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self._sample()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 4))


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _summarize(records: list[dict]) -> dict:
    durations = [item["durationSeconds"] for item in records]
    succeeded = sum(item["status"] == "succeeded" for item in records)
    return {
        "runs": len(records),
        "succeeded": succeeded,
        "failed": len(records) - succeeded,
        "successRate": succeeded / len(records) if records else 0.0,
        "latencySeconds": {
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
            "max": max(durations) if durations else None,
        },
        "peakProcessTreeRssBytes": max(
            (item["peakProcessTreeRssBytes"] for item in records), default=0
        ),
    }


def _scenario_kind(images: tuple[ImgItem, ...]) -> str:
    modalities = {item.imgType for item in images}
    if modalities == {"ECG"}:
        return "ecg"
    if modalities == {"CARDIAC_ULTRASOUND"}:
        return "echo"
    return "mixed"


def _validate_model_result(result: dict, images: tuple[ImgItem, ...]) -> None:
    if not isinstance(result, dict):
        raise BenchmarkOutputError()
    echo_per_image = result.get("echo_per_image", {})
    ecg_predictions = result.get("ecg_predictions", {})
    for image in images:
        if image.imgType == "CARDIAC_ULTRASOUND":
            payload = echo_per_image.get(image.imgId)
            if (
                not isinstance(payload, dict)
                or payload.get("error")
                or payload.get("skipReason")
            ):
                raise BenchmarkOutputError()
        else:
            predictions = ecg_predictions.get(image.imgId)
            if not isinstance(predictions, list) or not predictions:
                raise BenchmarkOutputError()


def run_stability_benchmark(
    runner,
    scenarios: list[BenchmarkScenario],
    *,
    iterations: int,
    work_root: str | Path,
    algorithm_version: str,
    max_run_seconds: float,
    max_peak_rss_bytes: int | None = None,
) -> dict:
    """按固定顺序连续运行场景，只记录性能与脱敏错误。"""
    version = resolve_algorithm_version(
        {"ALGORITHM_VERSION": algorithm_version}, use_fake=False
    )
    if iterations < 1:
        raise ValueError("iterations 必须大于 0")
    if max_run_seconds <= 0 or (
        max_peak_rss_bytes is not None and max_peak_rss_bytes < 1
    ):
        raise ValueError("基准时延和内存上限必须大于 0")
    if not scenarios or any(not item.name or not item.images for item in scenarios):
        raise ValueError("至少需要一个具有输入的命名场景")
    scenario_names = [item.name for item in scenarios]
    if len(set(scenario_names)) != len(scenario_names):
        raise ValueError("基准场景名称不能重复")
    for scenario in scenarios:
        image_ids = [image.imgId for image in scenario.images]
        if len(set(image_ids)) != len(image_ids):
            raise ValueError(f"场景 {scenario.name} 的 imgId 不能重复")
        for image in scenario.images:
            if image.imgType not in {"ECG", "CARDIAC_ULTRASOUND"}:
                raise ValueError(f"不支持的 imgType: {image.imgType!r}")
            if image.imgType == "ECG" and image.dcmType is not None:
                raise ValueError("ECG 基准输入不能设置 dcmType")
            if (
                image.imgType == "CARDIAC_ULTRASOUND"
                and image.dcmType not in VIEW_METRICS
            ):
                raise ValueError(f"不支持的 dcmType: {image.dcmType!r}")
    scenario_kinds = {_scenario_kind(item.images) for item in scenarios}
    if scenario_kinds != {"ecg", "echo", "mixed"}:
        raise ValueError("CPU 基准必须同时包含 ECG、心超和混合三类场景")
    covered_dcm_types = {
        image.dcmType
        for scenario in scenarios
        for image in scenario.images
        if image.imgType == "CARDIAC_ULTRASOUND"
    }
    missing_dcm_types = sorted(set(VIEW_METRICS) - covered_dcm_types)
    if missing_dcm_types:
        raise ValueError(
            "CPU 基准必须逐切面覆盖全部受支持的 dcmType；缺少: "
            + ", ".join(missing_dcm_types)
        )

    records: list[dict] = []
    scenario_metadata = {
        scenario.name: {
            "id": f"scenario-{index}",
            "kind": _scenario_kind(scenario.images),
            "modalities": sorted({item.imgType for item in scenario.images}),
            "dcmTypes": sorted(
                {item.dcmType for item in scenario.images if item.dcmType is not None}
            ),
        }
        for index, scenario in enumerate(scenarios, start=1)
    }
    for iteration in range(1, iterations + 1):
        for scenario in scenarios:
            task_id = f"benchmark-{uuid4().hex}"
            started = time.perf_counter()
            status = "succeeded"
            error_code = None
            error_message = None
            with _PeakProcessTreeRss() as memory:
                try:
                    result = runner.run(
                        list(scenario.images),
                        task_id=task_id,
                        work_root=str(work_root),
                    )
                    _validate_model_result(result, scenario.images)
                except Exception as exc:  # 基准必须继续完成后续场景
                    error = to_public_error(exc)
                    status = "failed"
                    error_code = error.code
                    error_message = error.message
            duration = time.perf_counter() - started
            if status == "succeeded" and (
                duration > max_run_seconds
                or (
                    max_peak_rss_bytes is not None
                    and memory.peak_bytes > max_peak_rss_bytes
                )
            ):
                error = BenchmarkLimitError().public_error
                status = "failed"
                error_code = error.code
                error_message = error.message
            records.append(
                {
                    "scenarioId": scenario_metadata[scenario.name]["id"],
                    "iteration": iteration,
                    "status": status,
                    "durationSeconds": duration,
                    "peakProcessTreeRssBytes": memory.peak_bytes,
                    "errorCode": error_code,
                    "error": error_message,
                }
            )

    scenario_reports = []
    for scenario in scenarios:
        metadata = scenario_metadata[scenario.name]
        selected = [
            item for item in records if item["scenarioId"] == metadata["id"]
        ]
        scenario_reports.append({**metadata, **_summarize(selected)})
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "algorithmVersion": version,
        "mode": "cpu-serial",
        "iterations": iterations,
        "limits": {
            "maxRunSeconds": max_run_seconds,
            "maxPeakProcessTreeRssBytes": max_peak_rss_bytes,
        },
        "summary": _summarize(records),
        "scenarios": scenario_reports,
        "runs": records,
    }
