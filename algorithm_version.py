"""算法发布标识解析与生产启动保护。"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping


_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+@:/-]{0,254}$")


class AlgorithmVersionError(ValueError):
    """算法发布标识缺失或不可用于结果追溯。"""


def resolve_algorithm_version(
    environment: Mapping[str, str] | None = None,
    *,
    use_fake: bool,
) -> str:
    """返回规范化版本；真实推理禁止缺失或使用 ``unknown``。"""
    source = os.environ if environment is None else environment
    configured = source.get("ALGORITHM_VERSION", "").strip()
    if not configured:
        if use_fake:
            return "fake"
        raise AlgorithmVersionError(
            "真实算法服务必须配置 ALGORITHM_VERSION（发布 tag/commit/build ID）"
        )
    if configured.lower() == "unknown" or not _VERSION_PATTERN.fullmatch(configured):
        raise AlgorithmVersionError(
            "ALGORITHM_VERSION 必须是可追溯的单行发布标识，且不能为 unknown"
        )
    return configured
