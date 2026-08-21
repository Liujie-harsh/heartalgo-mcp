"""ECG-FM XML inference adapter for the cardiac task API.

输入: HL7 aECG XML 信号文件 (前端传 XML)
流程: XML → parse_ecg_xml 解析测量值+患者信息 → xml_to_ecgfm_mat.py 转 MAT
      → infer_quickstart.py 推理 → predictions_aggregated.csv → Top-K (中文标签)

任务目录隔离 (handoff L517-L552):
  work_root/task_id/outputs/<imgId>/ecg/{mat,output}/...
  产物永久保留, 不自动清理 (handoff L527 决策)。

合并自 algorithm_merged/ecgfm_runner.py:
  - parse_ecg_xml: 解析 HL7 aECG XML 提取 8 项测量值 + 患者信息
  - LABELS_ZH: 18 个疾病标签英文→中文映射
  - ecg_measurements / ecg_patient_info: 结构化返回 (改进 #8)
  - safe_task / safe_img: taskId/imgId 路径 sanitize
  - 多 ECG 拒绝: 每任务仅允许 1 个 ECG (handoff L300)
  - 子进程错误脱敏: 已知输入错误翻译为中文，未知 stderr 不向 API 暴露
"""
from __future__ import annotations

import math
import logging
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pandas as pd

from algorithm_errors import AlgorithmError
from api import ImgItem
from config import ECGFMConfig


logger = logging.getLogger(__name__)


class ECGInputError(AlgorithmError, ValueError):
    """ECG XML 文件或其波形数据不符合模型输入要求。"""

    code = "ECG_INVALID_INPUT"
    default_message = "ECG 输入文件不符合模型要求"


class ECGConversionError(AlgorithmError, RuntimeError):
    """ECG XML 转换为 ECG-FM MAT 文件失败。"""

    code = "ECG_CONVERSION_FAILED"
    default_message = "ECG 数据转换失败"


class ECGInferenceError(AlgorithmError, RuntimeError):
    """ECG-FM 未产生可用的预测结果。"""

    code = "ECG_INFERENCE_FAILED"
    default_message = "ECG 模型推理失败"


class ECGTimeoutError(AlgorithmError, TimeoutError):
    """ECG 数据转换或模型推理超过时间限制。"""

    code = "ECG_TIMEOUT"
    default_message = "ECG 模型处理超时"
    retryable = True


# ECG-FM 疾病标签英文 → 中文
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

# HL7 aECG XML annotation code → 测量字段名
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
        patient_info: {patientId, age, sex}
    """
    root = ET.parse(xml_path).getroot()
    m = {}
    # 取 XML 中首个带 extension 的业务 ID；后续 extension="0" 为设备序列 ID，不作为患者标识。
    patient_id = next((e.get("extension") for e in root.iter()
                       if _name(e) == "id" and e.get("extension")), None)
    gender = next((e for e in root.iter() if _name(e) == "administrativeGenderCode"), None)
    birth = next((e for e in root.iter() if _name(e) == "birthTime"), None)
    exam = next((e for e in root.iter() if _name(e) == "effectiveTime"), None)
    low = _child(exam, "low") if exam is not None else None
    patient = {
        "patientId": patient_id,
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

    def run(self, imgs: list[ImgItem], task_id: str = "", work_root: str | None = None, gpu_device: str | None = None) -> dict:
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
        # 多 ECG 拒绝
        if len(ecg) != 1:
            raise ValueError("ECG-FM requires exactly one ECG image per task")
        self._validate_configuration()

        # 优先用调用方传入的 work_root, 否则用实例配置的 self.work_root
        effective_root = work_root or (str(self.work_root) if self.work_root else None)

        for image in ecg:
            source = Path(image.imgPath)
            if source.suffix.lower() != ".xml":
                raise ECGInputError("ECG 输入文件格式不支持：仅支持 XML 文件")
            if not source.is_file():
                logger.warning("ECG 输入文件不存在 path=%s", source)
                raise ECGInputError("ECG 输入文件不存在")
            # 缺少测量/患者标签不是失败；解析成功时按 {} / null 返回。
            try:
                measurements[image.imgId], patient_info[image.imgId] = parse_ecg_xml(source)
            except ET.ParseError as error:
                line, column = error.position
                raise ECGInputError(f"ECG XML 格式错误（第 {line} 行，第 {column} 列）") from error
            except (TypeError, ValueError) as error:
                raise ECGInputError("ECG XML 测量数据格式错误") from error
            predictions[image.imgId] = self._run_one(source, image.imgId, task_id, effective_root, gpu_device)
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

    def _run_one(self, xml_path: Path, img_id: str = "", task_id: str = "", work_root: str | None = None, gpu_device: str | None = None) -> list[dict]:
        converter = self.project_dir / "scripts" / "xml_to_ecgfm_mat.py"
        inference = self.project_dir / "scripts" / "infer_quickstart.py"

        # 任务隔离目录: <work_root>/<task_id>/outputs/<img_id>/ecg/
        # work_root 或 task_id 缺失时 fallback 到系统 temp (不隔离, 仅开发用)
        # safe_task / safe_img: taskId/imgId 含非法路径字符时替换为 _
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
        self._run_command([str(self.python_executable), str(converter), str(xml_path), str(mat_path)], timeout=self.timeout_seconds, stage="数据转换", gpu_device=gpu_device)
        self._run_command([
            str(self.python_executable), str(inference), str(mat_path),
            "--output-dir", str(output_dir),
            "--checkpoint", str(self.checkpoint),
        ], timeout=self.timeout_seconds, gpu_device=gpu_device)
        return self._read_top_k(output_dir / "predictions_aggregated.csv")

    @staticmethod
    def _conversion_error_message(detail: str) -> str:
        """将转换器可识别的输入错误翻译为面向 API 的中文失败原因。"""
        marker = "Missing long rhythm leads:"
        if marker in detail:
            leads = detail.split(marker, 1)[1].splitlines()[0].strip()
            return f"ECG 输入不完整：缺少十二导联长节律信号（{leads.replace(', ', '、')}）"
        if "Lead lengths do not match" in detail:
            return "ECG 输入不完整：十二导联采样点数量不一致"
        if "No sample interval found" in detail:
            return "ECG 输入不完整：采样率缺失或不合法"
        return "ECG 数据转换失败"

    @staticmethod
    def _run_command(command: list[str], timeout: int = 300, stage: str = "模型推理", gpu_device: str | None = None) -> None:
        """运行 ECG 子进程，转换为稳定的业务异常，不向 API 暴露 stderr。"""
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout, env={**os.environ, **({"CUDA_VISIBLE_DEVICES": str(gpu_device)} if gpu_device is not None else {})})
        except subprocess.TimeoutExpired as error:
            raise ECGTimeoutError(f"ECG {stage}超时（超过 {timeout} 秒）") from error
        except subprocess.CalledProcessError as error:
            detail = (error.stderr or error.stdout or "").strip()
            logger.error(
                "ECG 子进程失败 stage=%s return_code=%s stderr=%s",
                stage,
                error.returncode,
                detail[-4000:],
            )
            if stage == "数据转换":
                raise ECGConversionError(ECGFMRunner._conversion_error_message(detail)) from error
            raise ECGInferenceError("ECG 模型推理失败") from error

    def _read_top_k(self, csv_path: Path) -> list[dict]:
        """读取 CSV，校验有效概率后返回中文标签 Top-K。"""
        if not csv_path.is_file():
            raise ECGInferenceError("ECG 模型未返回预测结果文件")
        try:
            data = pd.read_csv(csv_path, index_col=0)
        except (OSError, UnicodeError, pd.errors.EmptyDataError, pd.errors.ParserError) as error:
            raise ECGInferenceError("ECG 模型未返回有效预测结果") from error
        if data.empty:
            raise ECGInferenceError("ECG 模型未返回有效预测结果")
        if len(data.index) != 1:
            raise ECGInferenceError(f"ECG 模型预测结果行数异常：期望 1 行，实际 {len(data.index)} 行")

        numeric_values = pd.to_numeric(data.iloc[0], errors="coerce")
        valid_values = numeric_values[
            numeric_values.map(lambda value: pd.notna(value) and math.isfinite(float(value)))
        ]
        if valid_values.empty:
            raise ECGInferenceError("ECG 模型预测概率为空或无效")

        values = valid_values.sort_values(ascending=False).head(self.top_k)
        return [
            {"label": LABELS_ZH.get(str(label), str(label)), "probability": round(float(probability), 6)}
            for label, probability in values.items()
        ]
