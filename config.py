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
from typing import ClassVar


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

    # ECG-FM 项目根目录: 需包含 XML 转换和推理脚本。
    DEFAULT_PROJECT_DIR: ClassVar[Path] = Path(r"G:\ecg-fm\ecg-fm\ecg-fm")
    # ECG-FM 微调权重文件路径。
    DEFAULT_CHECKPOINT: ClassVar[Path] = Path(r"G:\ecg-fm\ecg-fm\weights\mimic_iv_ecg_finetuned.pt")
    # ECG-FM 专用 Python 解释器路径。
    DEFAULT_PYTHON_EXECUTABLE: ClassVar[Path] = Path(r"C:\Users\Administrator\miniconda3\envs\ecg_env\python.exe")
    # ECG 返回概率最高的标签数量。
    DEFAULT_TOP_K: ClassVar[int] = 5
    # ECG 单阶段转换或推理超时秒数。
    DEFAULT_TIMEOUT_SECONDS: ClassVar[int] = 300

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


@dataclass(frozen=True)
class MeasurementConfig:
    """Measurement 心超模型配置。

    两种构造方式:
      - resolve(): 从 CLI 参数构造, 未传入时使用默认值 (不读环境变量)
      - 直接实例化: 传入 script_dir 和 python_executable
    """

    script_dir: Path
    python_executable: str

    # Measurement 项目目录: 需包含心超推理脚本 (inference_2D_image.py 等)
    DEFAULT_SCRIPT_DIR: ClassVar[Path] = Path(r"G:\meaurements\measurements\Measurement")
    # Measurement 推理使用的 Python 解释器
    DEFAULT_PYTHON_EXECUTABLE: ClassVar[str] = "python"

    @classmethod
    def resolve(cls, *, script_dir: str | Path | None = None,
                python_executable: str | None = None) -> "MeasurementConfig":
        """使用 CLI 覆盖值; 未传入时返回本文件默认配置。"""
        return cls(Path(script_dir).expanduser() if script_dir else cls.DEFAULT_SCRIPT_DIR,
                   python_executable or cls.DEFAULT_PYTHON_EXECUTABLE)


class ProductionConfigError(RuntimeError):
    """生产部署配置无效时抛出。"""


@dataclass(frozen=True)
class ProductionSettings:
    """MySQL、Redis、共享存储和 GPU 的占位生产配置。"""
    database_url: str
    redis_url: str
    queue_name: str
    task_work_root: str
    gpu_slots_per_device: int = 1

    DEFAULT_DATABASE_URL: ClassVar[str] = "mysql+pymysql://<db_user>:<db_password>@<db_host>:3306/<db_name>?charset=utf8mb4"
    DEFAULT_REDIS_URL: ClassVar[str] = "redis://:<redis_password>@<redis_host>:6379/0"
    DEFAULT_QUEUE_NAME: ClassVar[str] = "heart_algo_gpu"
    DEFAULT_TASK_WORK_ROOT: ClassVar[str] = r"<shared_storage_root>\runtime"

    @classmethod
    def resolve(cls, *, database_url: str | None = None, redis_url: str | None = None,
                queue_name: str | None = None, task_work_root: str | None = None,
                gpu_slots_per_device: int | None = None) -> "ProductionSettings":
        slots = 1 if gpu_slots_per_device is None else int(gpu_slots_per_device)
        if slots < 1:
            raise ProductionConfigError("单卡并发任务数必须大于 0")
        return cls(database_url or cls.DEFAULT_DATABASE_URL, redis_url or cls.DEFAULT_REDIS_URL,
                   queue_name or cls.DEFAULT_QUEUE_NAME, task_work_root or cls.DEFAULT_TASK_WORK_ROOT, slots)
