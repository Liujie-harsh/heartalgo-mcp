"""基于服务版本和模型文件内容生成可追溯发布标识。"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from algorithm_version import resolve_algorithm_version


_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        first_chunk = source.read(4 * 1024 * 1024)
        if first_chunk.startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise ValueError(
                f"artifact 仍是 Git LFS 指针，请先执行 git lfs pull: {path.name}"
            )
        digest.update(first_chunk)
        size += len(first_chunk)
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def build_release_manifest(
    service_version: str,
    artifacts: dict[str, str | Path],
) -> dict:
    """哈希模型文件；清单不保存服务器路径。"""
    service_version = service_version.strip()
    if not service_version or not _NAME_PATTERN.fullmatch(service_version):
        raise ValueError("service_version 格式无效")
    if not artifacts:
        raise ValueError("至少需要一个模型 artifact")
    artifact_manifest = {}
    for name, raw_path in sorted(artifacts.items()):
        if not _NAME_PATTERN.fullmatch(name):
            raise ValueError(f"artifact 名称格式无效: {name!r}")
        path = Path(raw_path)
        if not path.is_file():
            raise ValueError(f"artifact 文件不存在: {name}")
        sha256, size_bytes = _hash_file(path)
        artifact_manifest[name] = {"sha256": sha256, "sizeBytes": size_bytes}

    canonical = json.dumps(
        {"serviceVersion": service_version, "artifacts": artifact_manifest},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    artifact_set_sha256 = hashlib.sha256(canonical).hexdigest()
    algorithm_version = (
        f"heart@{service_version}+models@{artifact_set_sha256[:16]}"
    )
    resolve_algorithm_version(
        {"ALGORITHM_VERSION": algorithm_version}, use_fake=False
    )
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "serviceVersion": service_version,
        "artifactSetSha256": artifact_set_sha256,
        "algorithmVersion": algorithm_version,
        "artifacts": artifact_manifest,
    }


def _artifact_argument(value: str) -> tuple[str, Path]:
    name, separator, path = value.partition("=")
    if not separator or not name or not path:
        raise argparse.ArgumentTypeError("--artifact 必须使用 NAME=PATH")
    return name, Path(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="生成模型发布 SHA-256 清单和版本标识")
    parser.add_argument("--service-version", required=True)
    parser.add_argument(
        "--artifact", action="append", type=_artifact_argument, required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        artifacts = dict(args.artifact)
        if len(artifacts) != len(args.artifact):
            raise ValueError("artifact 名称不能重复")
        manifest = build_release_manifest(args.service_version, artifacts)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(manifest["algorithmVersion"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
