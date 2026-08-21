"""从真实 DICOM 和人工金标准清单生成 PhysicalDelta 校准报告。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from quality.physical_delta import (
    PhysicalDeltaError,
    build_calibration_report,
    extract_ultrasound_regions,
)


def calibrate_file(dicom_path: Path, manifest_path: Path) -> dict:
    try:
        import pydicom
    except ImportError as exc:  # pragma: no cover - 由部署依赖保证
        raise PhysicalDeltaError("运行校准工具需要安装 pydicom") from exc
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PhysicalDeltaError("无法读取校准 JSON 清单") from exc
    try:
        digest = hashlib.sha256()
        with dicom_path.open("rb") as source:
            while chunk := source.read(4 * 1024 * 1024):
                digest.update(chunk)
        source_sha256 = digest.hexdigest()
        dataset = pydicom.dcmread(str(dicom_path), stop_before_pixels=True)
    except (OSError, EOFError, ValueError, pydicom.errors.InvalidDicomError) as exc:
        raise PhysicalDeltaError("无法读取 DICOM 元数据") from exc
    return build_calibration_report(
        regions=extract_ultrasound_regions(dataset),
        source_sha256=source_sha256,
        manifest=manifest,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="生成去标识化的 DICOM PhysicalDelta 临床校准报告"
    )
    parser.add_argument("--dicom", type=Path, required=True, help="待校准 DICOM")
    parser.add_argument(
        "--manifest", type=Path, required=True, help="模型坐标与人工金标准 JSON"
    )
    parser.add_argument("--output", type=Path, required=True, help="输出报告 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = calibrate_file(args.dicom, args.manifest)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        f"校准完成: total={report['summary']['total']} "
        f"passed={report['summary']['passed']} failed={report['summary']['failed']}"
    )
    return (
        0
        if report["summary"]["failed"] == 0
        and report["summary"]["unevaluated"] == 0
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
