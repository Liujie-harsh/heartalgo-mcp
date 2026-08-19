"""算法服务统一配置。

所有模型和服务器配置遵循：CLI 参数优先；未指定时使用本文件默认值。
不读取环境变量；部署时直接修改本文件默认值即可。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar


class ECGFMConfigError(RuntimeError):
    """ECG-FM 配置无效时抛出。"""


@dataclass(frozen=True)
class ECGFMConfig:
    """ECG-FM 推理配置。"""
    project_dir: Path
    checkpoint: Path
    python_executable: Path
    top_k: int = 5
    timeout_seconds: int = 300

    # ECG-FM 项目根目录：需包含 XML 转换和推理脚本。
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
    def resolve(cls, *, project_dir: str | Path | None = None, checkpoint: str | Path | None = None,
                python_executable: str | Path | None = None, top_k: int | None = None,
                timeout_seconds: int | None = None) -> "ECGFMConfig":
        """使用 CLI 覆盖值；未传入时返回本文件默认配置。"""
        return cls(
            Path(project_dir).expanduser() if project_dir else cls.DEFAULT_PROJECT_DIR,
            Path(checkpoint).expanduser() if checkpoint else cls.DEFAULT_CHECKPOINT,
            Path(python_executable).expanduser() if python_executable else cls.DEFAULT_PYTHON_EXECUTABLE,
            cls._positive(top_k, cls.DEFAULT_TOP_K),
            cls._positive(timeout_seconds, cls.DEFAULT_TIMEOUT_SECONDS),
        )

    @staticmethod
    def _positive(value: int | None, default: int) -> int:
        """校验 CLI 正整数，缺省时返回默认值。"""
        result = default if value is None else int(value)
        if result < 1:
            raise ECGFMConfigError("数值配置必须大于 0")
        return result

    def validate(self) -> None:
        """在实际推理前校验项目目录、权重、解释器和必需脚本。"""
        required = (self.project_dir, self.checkpoint, self.python_executable,
                    self.project_dir / "scripts" / "xml_to_ecgfm_mat.py",
                    self.project_dir / "scripts" / "infer_quickstart.py")
        if not self.project_dir.is_dir() or any(not item.is_file() for item in required[1:]):
            raise ECGFMConfigError("ECG-FM 默认配置中的目录、权重、解释器或脚本不存在")


@dataclass(frozen=True)
class MeasurementConfig:
    """Measurement 心超模型配置。"""
    script_dir: Path
    python_executable: str
    timeout_seconds: int = 900

    # Measurement 项目目录：需包含心超推理脚本。
    DEFAULT_SCRIPT_DIR: ClassVar[Path] = Path(r"G:\meaurements\measurements\Measurement")
    # Measurement 推理使用的 Python 解释器。
    DEFAULT_PYTHON_EXECUTABLE: ClassVar[str] = "python"
    DEFAULT_TIMEOUT_SECONDS: ClassVar[int] = 900

    @classmethod
    def resolve(cls, *, script_dir: str | Path | None = None,
                python_executable: str | None = None,
                timeout_seconds: int | None = None) -> "MeasurementConfig":
        """使用 CLI 覆盖值；未传入时返回本文件默认配置。"""
        resolved_timeout = (
            cls.DEFAULT_TIMEOUT_SECONDS
            if timeout_seconds is None
            else int(timeout_seconds)
        )
        if resolved_timeout < 1:
            raise ValueError("Measurement 推理超时必须大于 0")
        return cls(
            Path(script_dir).expanduser() if script_dir else cls.DEFAULT_SCRIPT_DIR,
            python_executable or cls.DEFAULT_PYTHON_EXECUTABLE,
            resolved_timeout,
        )



class ProductionConfigError(RuntimeError):
    """生产部署配置无效时抛出。"""


@dataclass(frozen=True)
class ProductionSettings:
    """MySQL、共享存储和进程内 Python 队列的生产配置。"""
    database_url: str
    queue_name: str
    task_work_root: str
    python_queue_workers: int = 1

    # MySQL 连接地址：部署时替换占位符。
    DEFAULT_DATABASE_URL: ClassVar[str] = "mysql+pymysql://<db_user>:<db_password>@<db_host>:3306/<db_name>?charset=utf8mb4"
    # 本地 Python 队列名称：仅用于日志和执行记录，不依赖 Redis。
    DEFAULT_QUEUE_NAME: ClassVar[str] = "heart_algo_python_gpu"
    # 任务运行文件根目录：保存 MAT、CSV 等推理产物。
    DEFAULT_TASK_WORK_ROOT: ClassVar[str] = r"G:\heart-algo\runtime"

    @classmethod
    def resolve(cls, *, database_url: str | None = None, queue_name: str | None = None,
                task_work_root: str | None = None, python_queue_workers: int | None = None) -> "ProductionSettings":
        """使用 CLI 覆盖值；未传入时返回本文件服务器占位默认值。"""
        workers = 1 if python_queue_workers is None else int(python_queue_workers)
        if workers < 1:
            raise ProductionConfigError("Python 队列 Worker 数必须大于 0")
        return cls(database_url or cls.DEFAULT_DATABASE_URL,
                   queue_name or cls.DEFAULT_QUEUE_NAME,
                   task_work_root or cls.DEFAULT_TASK_WORK_ROOT,
                   workers)
