"""真实 ECG/心超/混合场景的串行 CPU 稳定性基准入口。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from combined_runner import CombinedRunner
from config import ECGFMConfig, MeasurementConfig
from ecgfm_runner import ECGFMRunner
from echonet_runner import EchoNetRunner
from quality.cpu_stability import BenchmarkScenario, run_stability_benchmark
from task_models import ImgItem


def _load_manifest(path: Path) -> tuple[dict, list[BenchmarkScenario]]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
        # Required acceptance thresholds are validated here so malformed
        # manifests consistently produce the CLI's public exit code 2.
        float(manifest["maxRunSeconds"])
        if manifest.get("maxPeakProcessTreeRssBytes") is not None:
            int(manifest["maxPeakProcessTreeRssBytes"])
        scenarios = []
        for scenario in manifest["scenarios"]:
            images = tuple(
                ImgItem(
                    imgId=str(item["imgId"]),
                    imgPath=str(item["imgPath"]),
                    imgType=str(item["imgType"]),
                    dcmType=item.get("dcmType"),
                )
                for item in scenario["inputs"]
            )
            scenarios.append(BenchmarkScenario(str(scenario["name"]), images))
        return manifest, scenarios
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("CPU 基准清单格式无效") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行真实算法串行 CPU 稳定性基准")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--measurement-dir", type=Path, required=True)
    parser.add_argument("--measurement-python", type=Path, required=True)
    parser.add_argument("--measurement-timeout-seconds", type=int, default=900)
    parser.add_argument("--ecg-project-dir", type=Path, required=True)
    parser.add_argument("--ecg-checkpoint", type=Path, required=True)
    parser.add_argument("--ecg-python", type=Path, required=True)
    parser.add_argument("--ecg-timeout-seconds", type=int, default=900)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        manifest, scenarios = _load_manifest(args.manifest)
        # 子进程继承该值；即使安装的是 CUDA 构建，也强制模型走 CPU。
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
        echo_runner = EchoNetRunner(
            config=MeasurementConfig(
                script_dir=args.measurement_dir,
                python_executable=str(args.measurement_python),
                timeout_seconds=args.measurement_timeout_seconds,
            )
        )
        ecg_runner = ECGFMRunner(
            config=ECGFMConfig(
                project_dir=args.ecg_project_dir,
                checkpoint=args.ecg_checkpoint,
                python_executable=args.ecg_python,
                timeout_seconds=args.ecg_timeout_seconds,
            ),
            work_root=args.work_root,
        )
        runner = CombinedRunner(
            echo_runner=echo_runner,
            ecg_runner=ecg_runner,
            gpu_pool=None,
        )
        report = run_stability_benchmark(
            runner,
            scenarios,
            iterations=int(manifest.get("iterations", 1)),
            work_root=args.work_root,
            algorithm_version=str(manifest.get("algorithmVersion", "")),
            max_run_seconds=float(manifest["maxRunSeconds"]),
            max_peak_rss_bytes=(
                int(manifest["maxPeakProcessTreeRssBytes"])
                if manifest.get("maxPeakProcessTreeRssBytes") is not None
                else None
            ),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    summary = report["summary"]
    print(
        f"CPU 基准完成: succeeded={summary['succeeded']}/{summary['runs']} "
        f"p95={summary['latencySeconds']['p95']}s "
        f"peak_rss={summary['peakProcessTreeRssBytes']}B"
    )
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
