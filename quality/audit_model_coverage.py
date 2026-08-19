"""命令行模型覆盖门禁。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from quality.model_coverage import audit_model_coverage


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="审计 Measurement 脚本、权重和逐切面验证 DICOM"
    )
    parser.add_argument("--measurement-dir", type=Path, required=True)
    parser.add_argument("--samples-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = audit_model_coverage(args.measurement_dir, args.samples_root)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    summary = report["summary"]
    print(
        f"资产审计: ready={summary['assetReadyViews']}/{summary['totalViews']} "
        f"missing_weights={len(summary['missingWeights'])} "
        f"missing_samples={len(summary['missingSampleViews'])}"
    )
    return 0 if summary["allAssetsReady"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
