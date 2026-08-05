"""ECG-FM XML inference adapter for the cardiac task API.

输入: HL7 aECG XML 信号文件 (前端传 XML)
流程: XML → parse_ecg_xml 解析测量值+患者信息 → xml_to_ecgfm_mat.py 转 MAT
      → infer_quickstart.py 推理 → predictions_aggregated.csv → Top-K (中文标签)

任务目录隔离 (handoff L517-L552):
  work_root/task_id/outputs/<imgId>/ecg/{mat,output}/...
  产物永久保留, 不自动清理 (handoff L527 决策)。

同事贡献 (合并自 algorithm_merged/ecgfm_runner.py):
  - parse_ecg_xml: 解析 HL7 aECG XML 提取 8 项测量值 + 患者信息
  - LABELS_ZH: 18 个疾病标签英文→中文映射
  - ecg_measurements / ecg_patient_info: 结构化返回 (改进 #8)
  - safe_task / safe_img: taskId/imgId 路径 sanitize
  - health_check_from_env: 静态健康检查 (读环境变量)
  - 多 ECG 拒绝: 每任务仅允许 1 个 ECG (handoff L300)
  - _cmd stderr 透传: 子进程失败时把 stderr 透传到 ValueError
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pandas as pd

from api import ImgItem
from config import ECGFMConfig


# ECG-FM 疾病标签英文 → 中文 (同事贡献)
LABELS_ZH = {
    "Poor data quality": "数据质量差",
    "Sinus rhythm": "窦性心律",
    "Premature ventricular contraction": "室性早搏",
    "Tachycardia": "心动过速",
    "Ventricular tachycardia": "室性心动过速",
    "Supraventricular tachycardia with aberrancy": "室上性心动过速伴差异传导",
    "Bradycardia": "心动过缓",
    "Infarction": "心肌梗死",
    "Atrioventricular block": "房室传导阻滞",
    "Right bundle branch block": "右束支传导阻滞",
    "Left bundle branch block": "左束支传导阻滞",
    "Electronic pacemaker": "起搏心律",
    "Atrial fibrillation": "心房颤动",
    "Atrial flutter": "心房扑动",
    "Accessory pathway conduction": "旁路传导",
    "1st degree atrioventricular block": "一度房室传导阻滞",
    "Bifascicular block": "双分支传导阻滞",
}

# HL7 aECG XML annotation code → 测量字段名 (同事贡献)
CODES = {
    "MDC_ECG_HEART_RATE": "ventRate",
    "MDC_ECG_TIME_PD_PR": "prInterval",
    "MDC_ECG_TIME_PD_QRS": "qrsDuration",
    "MDC_ECG_TIME_PD_QT": "qt",
    "MDC_ECG_TIME_PD_QTC": "qtc",
    "MDC_ECG_ANGLE_P_FRONT": "pAxis",
    "MDC_ECG_ANGLE_QRS_FRONT": "qrsAxis",
    "MDC_ECG_ANGLE_T_FRONT": "tAxis",
}


def _name(e):
    """取 XML 元素 local name (去掉 namespace 前缀)。"""
    return e.tag.rsplit("}", 1)[-1]


def _child(e, n):
    """取指定 local name 的子元素。"""
    return next((x for x in e if _name(x) == n), None)


def _number(v):
    """字符串转数字, 整数转 int, 小数保 float。"""
    n = float(v)
    return int(n) if n.is_integer() else n


def _age(birth, exam):
    """根据出生日期和检查日期算年龄。"""
    try:
        b = datetime.strptime(birth[:8], "%Y%m%d").date()
        e = datetime.strptime(exam[:8], "%Y%m%d").date()
        return e.year - b.year - ((e.month, e.day) < (b.month, b.day))
    except (TypeError, ValueError):
        return None


def parse_ecg_xml(xml_path: Path) -> tuple[dict, dict]:
    """解析 HL7 aECG XML, 提取测量值 + 患者信息 (同事贡献)。

    Returns:
        (measurements, patient_info)
        measurements: {ventRate, prInterval, qrsDuration, qt, qtc, pAxis, qrsAxis, tAxis}
        patient_info: {name, age, sex}
    """
    root = ET.parse(xml_path).getroot()
    m = {}
    family = next((e for e in root.iter() if _name(e) == "family"), None)
    given = next((e for e in root.iter() if _name(e) == "given"), None)
    gender = next((e for e in root.iter() if _name(e) == "administrativeGenderCode"), None)
    birth = next((e for e in root.iter() if _name(e) == "birthTime"), None)
    exam = next((e for e in root.iter() if _name(e) == "effectiveTime"), None)
    low = _child(exam, "low") if exam is not None else None
    patient = {
        "name": " ".join(x for x in [
            ((family.text or "").strip() if family is not None else ""),
            ((given.text or "").strip() if given is not None else ""),
        ] if x) or None,
        "age": _age(birth.get("value") if birth is not None else None,
                    low.get("value") if low is not None else None),
        "sex": gender.get("code") if gender is not None else None,
    }
    for a in root.iter():
        if _name(a) != "annotation":
            continue
        c, v = _child(a, "code"), _child(a, "value")
        field = CODES.get((c.get("code") or "").upper()) if c is not None else None
        if field and v is not None and v.get("value") is not None:
            m[field] = _number(v.get("value"))
    return m, patient


class ECGFMRunner:
    """Run XML -> parse + ECG-FM MAT -> ECG-FM inference in a task-isolated workspace."""

    def __init__(
        self,
        config: ECGFMConfig | None = None,
        *,
        work_root: str | Path | None = None,
        # 旧参数兼容 (优先级低于 config; 测试 / 旧调用方仍可用)
        project_dir: str | Path | None = None,
        checkpoint: str | Path | None = None,
        python_executable: str | None = None,
        top_k: int = 5,
        timeout_seconds: int = 300,
    ):
        if config is None:
            # 兼容旧接口: 从单独参数构造 config (不校验存在性, 与 from_cli 一致)
            work_dir = Path(__file__).resolve().parents[1]
            pd_path = Path(project_dir) if project_dir else work_dir / "ecg-fm" / "ecg-fm"
            ckpt_path = Path(checkpoint) if checkpoint else work_dir / "ecg-fm" / "weights" / "mimic_iv_ecg_finetuned.pt"
            # 兼容权重在项目目录下的情况 (如 G:\ecg-fm\ecg-fm\ecg-fm\weights\)
            if not ckpt_path.is_file():
                alt = pd_path / "weights" / "mimic_iv_ecg_finetuned.pt"
                if alt.is_file():
                    ckpt_path = alt
            config = ECGFMConfig(
                project_dir=pd_path,
                checkpoint=ckpt_path,
                python_executable=Path(python_executable) if python_executable else Path(sys.executable),
                top_k=top_k,
                timeout_seconds=timeout_seconds,
            )
        self.config = config
        self.work_root = Path(work_root) if work_root else None
        # 暴露 config 字段为属性 (兼容旧代码访问 self.project_dir 等)
        self.project_dir = config.project_dir
        self.checkpoint = config.checkpoint
        self.python_executable = config.python_executable
        self.top_k = config.top_k
        self.timeout_seconds = config.timeout_seconds

    def health_check(self) -> dict:
        """实例级健康检查: 读 self.config 而非 os.environ (Q1 决策)。

        与 health_check_from_env 的差异: 配置已绑定到 runner 实例, 不依赖环境变量。
        main.py 应传 runner.health_check 给 create_app 的 ecgfm_health_check 参数。
        """
        return _check_ecgfm_files(
            project_dir=self.config.project_dir,
            checkpoint=self.config.checkpoint,
            python_executable=self.config.python_executable,
        )

    @staticmethod
    def health_check_from_env() -> dict:
        """静态健康检查: 读 ECGFM_* 环境变量 (兼容 test_ecgfm_health.py)。

        Deprecated: 优先用实例级 health_check()。保留此方法仅为兼容旧测试和
        未注入 runner 的场景 (如独立诊断脚本)。
        """
        project_raw = os.environ.get("ECGFM_PROJECT_DIR", "")
        checkpoint_raw = os.environ.get("ECGFM_CHECKPOINT", "")
        python_raw = os.environ.get("ECGFM_PYTHON", "")
        project_dir = Path(project_raw) if project_raw else None
        checkpoint = Path(checkpoint_raw) if checkpoint_raw else None
        python_executable = Path(python_raw) if python_raw else None
        result = _check_ecgfm_files(
            project_dir=project_dir,
            checkpoint=checkpoint,
            python_executable=python_executable,
        )
        # 增补 "是否已配置" 字段 (env 工作流需要区分未配置 vs 配置错)
        result["projectDirConfigured"] = bool(project_raw)
        result["checkpointConfigured"] = bool(checkpoint_raw)
        result["pythonConfigured"] = bool(python_raw)
        return result

    def run(self, imgs: list[ImgItem], task_id: str = "", work_root: str | None = None) -> dict:
        """返回 {ecg_predictions, ecg_measurements, ecg_patient_info}。

        每项都是 {imgId: ...} 结构。

        Raises:
            ValueError: 多 ECG 拒绝 (handoff L300, 每任务仅允许 1 个 ECG)
            FileNotFoundError: XML 文件不存在 / 后缀非 .xml
        """
        ecg = [i for i in imgs if i.imgType == "ECG"]
        predictions: dict[str, list[dict]] = {}
        measurements: dict[str, dict] = {}
        patient_info: dict[str, dict] = {}
        if not ecg:
            return {
                "ecg_predictions": predictions,
                "ecg_measurements": measurements,
                "ecg_patient_info": patient_info,
            }
        # 多 ECG 拒绝 (同事贡献, handoff L300)
        if len(ecg) != 1:
            raise ValueError("ECG-FM requires exactly one ECG image per task")
        self._validate_configuration()

        # 优先用调用方传入的 work_root, 否则用实例配置的 self.work_root
        effective_root = work_root or (str(self.work_root) if self.work_root else None)

        for image in ecg:
            source = Path(image.imgPath)
            if source.suffix.lower() != ".xml" or not source.is_file():
                raise FileNotFoundError(f"ECG XML file not found or invalid: {source}")
            # 同步解析 XML 测量值 + 患者信息 (同事贡献)
            measurements[image.imgId], patient_info[image.imgId] = parse_ecg_xml(source)
            predictions[image.imgId] = self._run_one(source, image.imgId, task_id, effective_root)
        return {
            "ecg_predictions": predictions,
            "ecg_measurements": measurements,
            "ecg_patient_info": patient_info,
        }

    def _validate_configuration(self) -> None:
        converter = self.project_dir / "scripts" / "xml_to_ecgfm_mat.py"
        inference = self.project_dir / "scripts" / "infer_quickstart.py"
        missing = [str(path) for path in (converter, inference, self.checkpoint) if not path.is_file()]
        if missing:
            raise FileNotFoundError("ECG-FM configuration file not found: " + "; ".join(missing))
        if self.top_k < 1:
            raise ValueError("top_k must be at least 1")

    def _run_one(self, xml_path: Path, img_id: str = "", task_id: str = "", work_root: str | None = None) -> list[dict]:
        converter = self.project_dir / "scripts" / "xml_to_ecgfm_mat.py"
        inference = self.project_dir / "scripts" / "infer_quickstart.py"

        # 任务隔离目录: <work_root>/<task_id>/outputs/<img_id>/ecg/
        # work_root 或 task_id 缺失时 fallback 到系统 temp (不隔离, 仅开发用)
        # safe_task / safe_img: taskId/imgId 含非法路径字符时替换为 _ (同事贡献)
        if work_root and task_id:
            safe_task = re.sub(r"[^A-Za-z0-9_.-]", "_", task_id).strip("._")
            safe_img = re.sub(r"[^A-Za-z0-9_.-]", "_", img_id).strip("._") or "ecg"
            if not safe_task:
                raise ValueError("task_id must contain at least one letter or number")
            task_dir = Path(work_root) / safe_task / "outputs" / safe_img / "ecg"
            task_dir.mkdir(parents=True, exist_ok=True)
        else:
            base_dir = str(self.work_root) if self.work_root else None
            # 与 if 分支保持一致: 确保 base_dir 存在 (work_root 配置但未创建时 mkdtemp 会失败)
            if base_dir:
                Path(base_dir).mkdir(parents=True, exist_ok=True)
            task_dir = Path(tempfile.mkdtemp(prefix="ecgfm_", dir=base_dir))

        mat_path = task_dir / "mat" / f"{xml_path.stem}.mat"
        mat_path.parent.mkdir(parents=True, exist_ok=True)
        output_dir = task_dir / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        self._run_command([str(self.python_executable), str(converter), str(xml_path), str(mat_path)], timeout=self.timeout_seconds)
        self._run_command([
            str(self.python_executable), str(inference), str(mat_path),
            "--output-dir", str(output_dir),
            "--checkpoint", str(self.checkpoint),
        ], timeout=self.timeout_seconds)
        return self._read_top_k(output_dir / "predictions_aggregated.csv")

    @staticmethod
    def _run_command(command: list[str], timeout: int = 300) -> None:
        """运行子进程, 超时抛 subprocess.TimeoutExpired (被 _execute 捕获 → taskState=3)。

        子进程失败时把 stderr 透传到 ValueError (同事贡献, 比 CalledProcessError 更友好)。
        """
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or "").strip()
            if detail:
                raise ValueError(f"ECG-FM command failed: {detail}") from error
            raise ValueError(f"ECG-FM command failed with exit code {error.returncode}") from error

    def _read_top_k(self, csv_path: Path) -> list[dict]:
        """读 CSV 取 Top-K, 标签中文化 (同事贡献)。"""
        if not csv_path.is_file():
            raise FileNotFoundError(f"ECG-FM did not produce aggregated predictions: {csv_path}")
        data = pd.read_csv(csv_path, index_col=0)
        if len(data.index) != 1:
            raise ValueError(f"Expected one ECG-FM prediction row, got {len(data.index)}")
        values = data.iloc[0].astype(float).sort_values(ascending=False).head(self.top_k)
        return [
            {"label": LABELS_ZH.get(str(label), str(label)), "probability": round(float(probability), 6)}
            for label, probability in values.items()
        ]


def _check_ecgfm_files(
    project_dir: Path | None,
    checkpoint: Path | None,
    python_executable: Path | None,
) -> dict:
    """检查 ECG-FM 文件和解释器, 不导入也不加载模型 (供实例/静态 health_check 共用)。

    Args:
        project_dir / checkpoint / python_executable: 已解析的 Path 或 None (未配置)
    """
    errors: list[str] = []
    project_exists = bool(project_dir and project_dir.is_dir())
    converter_exists = bool(project_dir and (project_dir / "scripts" / "xml_to_ecgfm_mat.py").is_file())
    inference_exists = bool(project_dir and (project_dir / "scripts" / "infer_quickstart.py").is_file())
    checkpoint_exists = bool(checkpoint and checkpoint.is_file())
    python_exists = bool(python_executable and python_executable.is_file())
    python_callable = False
    python_version = None

    if not project_dir:
        errors.append("ECGFM_PROJECT_DIR is not configured")
    elif not project_exists:
        errors.append(f"ECGFM_PROJECT_DIR does not exist: {project_dir}")
    if project_exists and not converter_exists:
        errors.append("xml_to_ecgfm_mat.py is missing")
    if project_exists and not inference_exists:
        errors.append("infer_quickstart.py is missing")
    if not checkpoint:
        errors.append("ECGFM_CHECKPOINT is not configured")
    elif not checkpoint_exists:
        errors.append(f"ECGFM_CHECKPOINT does not exist: {checkpoint}")
    if not python_executable:
        errors.append("ECGFM_PYTHON is not configured")
    elif not python_exists:
        errors.append(f"ECGFM_PYTHON does not exist: {python_executable}")
    else:
        try:
            process = subprocess.run(
                [str(python_executable), "--version"],
                capture_output=True, text=True, timeout=5, check=True,
            )
            python_callable = True
            python_version = (process.stdout or process.stderr).strip() or None
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(f"ECGFM_PYTHON is not callable: {error}")

    return {
        "status": "healthy" if not errors else "unhealthy",
        "projectDirExists": project_exists,
        "converterScriptExists": converter_exists,
        "inferenceScriptExists": inference_exists,
        "checkpointExists": checkpoint_exists,
        "checkpointSizeBytes": checkpoint.stat().st_size if checkpoint_exists else None,
        "pythonExists": python_exists,
        "pythonCallable": python_callable,
        "pythonVersion": python_version,
        "errors": errors,
    }
