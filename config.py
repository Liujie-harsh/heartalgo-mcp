"""ECG-FM runtime configuration.

两种构造方式:
  - from_env(): 从 ECGFM_* 环境变量构造, 严格校验路径存在 (供 from_env 工作流使用)
  - from_cli(): 从 CLI 字符串参数构造, 不校验存在性 (路径有效性交给 health_check 检测,
                这样服务能启动, /health 能返回 unhealthy 让运维排查)

config 对象作为连接 CLI 参数和 ECGFMRunner / health_check 的桥梁 (Q3 决策)。
"""
# $env:ECGFM_PROJECT_DIR = "G:\ecg-fm\ecg-fm\ecg-fm"
# $env:ECGFM_CHECKPOINT = "G:\ecg-fm\ecg-fm\weights\mimic_iv_ecg_finetuned.pt"
# $env:ECGFM_PYTHON = "C:\Users\Administrator\miniconda3\envs\ecg_env\python.exe"
# $env:ECGFM_TOP_K = "5"
# $env:ECGFM_TIMEOUT_SECONDS = "300"
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ECGFMConfigError(RuntimeError):
    """Raised when the ECG-FM runtime configuration is invalid."""


@dataclass(frozen=True)
class ECGFMConfig:
    """Configuration required to execute the ECG-FM XML inference pipeline.

    Attributes:
        project_dir: ECG-FM 项目目录 (含 scripts/ 子目录)
        checkpoint: 微调权重路径
        python_executable: ECG-FM conda 环境的 python.exe
        top_k: 返回疾病概率 Top-K
        timeout_seconds: 单次 ECG 子进程超时 (秒)
    """

    project_dir: Path
    checkpoint: Path
    python_executable: Path
    top_k: int = 5
    timeout_seconds: int = 300

    @classmethod
    def from_env(cls) -> "ECGFMConfig":
        """Read ECG-FM settings from environment variables and validate them.

        Required:
            ECGFM_PROJECT_DIR, ECGFM_CHECKPOINT, ECGFM_PYTHON
        Optional:
            ECGFM_TOP_K (default 5), ECGFM_TIMEOUT_SECONDS (default 300)

        路径必须存在, 否则抛 ECGFMConfigError。
        """
        project_dir = cls._required_path("ECGFM_PROJECT_DIR", expected="dir")
        checkpoint = cls._required_path("ECGFM_CHECKPOINT", expected="file")
        python_executable = cls._required_path("ECGFM_PYTHON", expected="file")
        top_k = cls._positive_int("ECGFM_TOP_K", default=5)
        timeout_seconds = cls._positive_int("ECGFM_TIMEOUT_SECONDS", default=300)

        cls._validate_scripts(project_dir)

        return cls(
            project_dir=project_dir,
            checkpoint=checkpoint,
            python_executable=python_executable,
            top_k=top_k,
            timeout_seconds=timeout_seconds,
        )

    @classmethod
    def from_cli(
        cls,
        project_dir: str | Path,
        checkpoint: str | Path,
        python_executable: str | Path,
        top_k: int = 5,
        timeout_seconds: int = 300,
    ) -> "ECGFMConfig":
        """从 CLI 字符串参数构造 config (Q3 决策)。

        与 from_env 的差异: 不校验路径存在性。这样服务能启动, /health 端点能返回
        unhealthy 状态让运维排查 (而非启动即崩)。路径有效性由 health_check / run 检测。
        """
        return cls(
            project_dir=Path(project_dir).expanduser(),
            checkpoint=Path(checkpoint).expanduser(),
            python_executable=Path(python_executable).expanduser(),
            top_k=top_k,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def _validate_scripts(project_dir: Path) -> None:
        converter = project_dir / "scripts" / "xml_to_ecgfm_mat.py"
        inference = project_dir / "scripts" / "infer_quickstart.py"
        missing = [str(path) for path in (converter, inference) if not path.is_file()]
        if missing:
            raise ECGFMConfigError(
                "ECGFM_PROJECT_DIR does not contain required scripts: " + "; ".join(missing)
            )

    @staticmethod
    def _required_path(name: str, expected: str) -> Path:
        raw_value = os.environ.get(name)
        if not raw_value:
            raise ECGFMConfigError(f"Missing required environment variable: {name}")
        path = Path(raw_value).expanduser().resolve()
        is_valid = path.is_dir() if expected == "dir" else path.is_file()
        if not is_valid:
            raise ECGFMConfigError(f"{name} must point to an existing {expected}: {path}")
        return path

    @staticmethod
    def _positive_int(name: str, default: int) -> int:
        raw_value = os.environ.get(name)
        if raw_value is None or raw_value == "":
            return default
        try:
            value = int(raw_value)
        except ValueError as error:
            raise ECGFMConfigError(f"{name} must be an integer, got: {raw_value!r}") from error
        if value < 1:
            raise ECGFMConfigError(f"{name} must be greater than 0, got: {value}")
        return value
