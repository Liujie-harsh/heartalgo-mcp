"""
EchoNet 真实推理 runner (生产用, 需 torch+GPU)。

切面分流 (v3):
  前端按 dcmType 分组传入, runner 查 DCM_TYPE_TASKS 表获取该切面的所有指标任务,
  依次调用对应推理脚本 + 权重, 通用 _parse_by_rule() 按取值规则从 CSV/stdout 提取指标值。

4 个推理脚本接口:
  inference_2D_image.py      --model_weights ivs|lvid|...  --file_path --output_path
  inference_Doppler_image.py --model_weights avvmax|trvmax|... --file_path --output_path
  inference_MV_EperA.py      (无 model_weights)            --file_path --output_path
  inference_TAPSE.py         (无 model_weights)            --file_path --output_path

参考: F:\\1\\Measurement\\run_inference_pipeline.py (TASKS 表 + SLICE_DIR_MAP)

任务目录隔离:
  work_root/task_id/outputs/<imgId>/echo/<weights或metric>/out_*.avi|jpg
  产物永久保留, 不自动清理。
"""
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
import pandas as pd
from algorithm_errors import AlgorithmError
from api import ImgItem
from config import MeasurementConfig


logger = logging.getLogger(__name__)


class EchoInferenceError(AlgorithmError, RuntimeError):
    """心超测量脚本未成功产生可用结果。"""

    code = "ECHO_INFERENCE_FAILED"
    default_message = "心超模型推理失败"


# ────────────────── 切面→子目录映射表 ──────────────────

SLICE_DIR_MAP = {
    # 分支1 B-Mode - 仅文档定义的 4 个切片, a2c/a3c/psax 等预留切片不在此表
    "PLAX":       "plax",
    "A4C":        "a4c",
    "Subcostal":  "subcostal",
    "RVOT":       "rvot",
    # 分支2 Doppler - 英文代号子目录 (服务器实际目录名)
    "MV_EA":      "MV_EA",
    "AV_Vmax":    "AV_Vmax",
    "TR_Vmax":    "TR_Vmax",
    "MR_Vmax":    "MR_Vmax",
    "LVOT_Vmax":  "LVOT_Vmax",
    # 分支3 TDI - 英文代号子目录
    "TDI_Medial":  "TDI_Medial",
    "TDI_Lateral": "TDI_Lateral",
    # 分支4 M-Mode - 英文代号子目录
    "TAPSE":      "TAPSE",
}


# ────────────────── 切面→指标任务映射表 ──────────────────
# 参考 run_inference_pipeline.py TASKS 表, 按 dcmType 分组。
# 每个任务: {metric, script, weights, extra, value_rule}
#
# value_rule 取值规则:
#   "ed_es"        - LVID 专用: ED=max→LVEDD, ES=min→LVESD (cm→mm), 产出 2 个 rois + ED/ES 帧号
#   "ed_frame"     - 取 LVID ED 帧号的值 (cm→mm), 需 LVID 先跑提供 ed_idx (PLAX 内部依赖)
#   "max_bm"       - B-Mode 取最大值 (cm→mm), 如 LA
#   "mean_bm"      - B-Mode 取均值 (cm→mm), 如血管类 Aorta/AorticRoot/RVBase/IVC/PA
#   "stdout_ea"    - MV_EA 终端解析 E_Vel/A_Vel/E/A 三个值, rois 为空
#   "stdout_vmax"  - Doppler/TDI 终端解析 Peak Velocity, rois 为空
#   "stdout_tapse" - TAPSE 终端解析位移值并将 cm 转为 mm, rois 为空

DCM_TYPE_TASKS: dict[str, list[dict]] = {
    # ===== 分支1: B-Mode (inference_2D_image.py, CSV 单位 cm → ×10 转 mm) =====
    "PLAX": [
        # LVID 必须第一个跑: --phase_estimate 产出 ED/ES 帧号, 供 IVS/LVPW 使用
        {"metric": "LVID",       "script": "inference_2D_image.py", "weights": "lvid",        "extra": ["--phase_estimate"], "value_rule": "ed_es"},
        {"metric": "IVS",        "script": "inference_2D_image.py", "weights": "ivs",         "extra": [],                   "value_rule": "ed_frame"},
        {"metric": "LVPW",       "script": "inference_2D_image.py", "weights": "lvpw",        "extra": [],                   "value_rule": "ed_frame"},
        {"metric": "LA",         "script": "inference_2D_image.py", "weights": "la",          "extra": [],                   "value_rule": "max_bm"},
        {"metric": "Aorta",      "script": "inference_2D_image.py", "weights": "aorta",       "extra": [],                   "value_rule": "mean_bm"},
        {"metric": "AorticRoot", "script": "inference_2D_image.py", "weights": "aortic_root", "extra": [],                   "value_rule": "mean_bm"},
    ],
    "A4C": [
        {"metric": "RVBase", "script": "inference_2D_image.py", "weights": "rv_base", "extra": [], "value_rule": "mean_bm"},
    ],
    "Subcostal": [
        {"metric": "IVC", "script": "inference_2D_image.py", "weights": "ivc", "extra": [], "value_rule": "mean_bm"},
    ],
    "RVOT": [
        {"metric": "PA", "script": "inference_2D_image.py", "weights": "pa", "extra": [], "value_rule": "mean_bm"},
    ],

    # ===== 分支2: Spectral Doppler =====
    "MV_EA": [
        {"metric": "MV_EA", "script": "inference_MV_EperA.py", "weights": None, "extra": [], "value_rule": "stdout_ea"},
    ],
    "AV_Vmax": [
        {"metric": "AV_Vmax", "script": "inference_Doppler_image.py", "weights": "avvmax", "extra": [], "value_rule": "stdout_vmax"},
    ],
    "TR_Vmax": [
        {"metric": "TR_Vmax", "script": "inference_Doppler_image.py", "weights": "trvmax", "extra": [], "value_rule": "stdout_vmax"},
    ],
    "MR_Vmax": [
        {"metric": "MR_Vmax", "script": "inference_Doppler_image.py", "weights": "mrvmax", "extra": [], "value_rule": "stdout_vmax"},
    ],
    "LVOT_Vmax": [
        {"metric": "LVOT_Vmax", "script": "inference_Doppler_image.py", "weights": "lvotvmax", "extra": [], "value_rule": "stdout_vmax"},
    ],

    # ===== 分支3: TDI (inference_Doppler_image.py, medevel/latevel) =====
    "TDI_Medial": [
        {"metric": "TDI_Medial", "script": "inference_Doppler_image.py", "weights": "medevel", "extra": [], "value_rule": "stdout_vmax"},
    ],
    "TDI_Lateral": [
        {"metric": "TDI_Lateral", "script": "inference_Doppler_image.py", "weights": "latevel", "extra": [], "value_rule": "stdout_vmax"},
    ],

    # ===== 分支4: M-Mode TAPSE (inference_TAPSE.py) =====
    "TAPSE": [
        {"metric": "TAPSE", "script": "inference_TAPSE.py", "weights": None, "extra": [], "value_rule": "stdout_tapse"},
    ],
}


# ────────────────── Teichholz 公式 (纯函数, 可测) ──────────────────

def teichholz_lvef(edd_mm: float, esd_mm: float) -> float:
    """
    Teichholz 法估算 LVEF。

    LVEF = (Ved - Ves) / Ved * 100
    V = 7 * D³ / (2.4 + D)   (Teichholz 校正)

    handoff.md 验证: EDD≈5.06cm→50.6mm, ESD≈3.16cm→31.6mm → LVEF=35.48%
    """
    edd_cm = edd_mm / 10.0
    esd_cm = esd_mm / 10.0
    v_ed = 7 * edd_cm ** 3 / (2.4 + edd_cm)
    v_es = 7 * esd_cm ** 3 / (2.4 + esd_cm)
    lvef = (v_ed - v_es) / v_ed * 100
    return round(lvef, 2)


# ────────────────── 真实推理 runner ──────────────────


class EchoNetRunner:
    """生产推理器: 按 dcmType 查 DCM_TYPE_TASKS 表分流推理。"""

    def __init__(
        self,
        config: MeasurementConfig | None = None,
        *,
        script_dir: str | None = None,
        python_executable: str | None = None,
    ):
        self.config = config or MeasurementConfig.resolve(
            script_dir=script_dir,
            python_executable=python_executable,
        )
        self.script_dir = str(self.config.script_dir)
        self.python_executable = self.config.python_executable

    def run(self, imgs: list[ImgItem], task_id: str = "", work_root: str | None = None, gpu_device: str | None = None) -> dict:
        """
        按 img.dcmType 查 DCM_TYPE_TASKS 表分流推理。

        每份 imgId 独立推理, 结果按 {imgId: metrics} 结构返回。
        顶层主指标 (lvef/lvedd/lvesd/lad/mv_ea) 供 rules.analyze 使用。
        per_image 内含该切面的所有指标 (如 PLAX 的 ivs/lvpw/aorta 等)。

        PLAX 内部依赖: LVID 必须第一个跑, 产出 ED/ES 帧号供 IVS/LVPW 的 ed_frame 规则使用。
        """
        echo_files = [img for img in imgs if img.imgType == "CARDIAC_ULTRASOUND"]
        per_image: dict[str, dict] = {}

        # 顶层主指标 (供 rules.analyze)
        lvef = lvedd = lvesd = lad = mv_ea = None

        for img in echo_files:
            dcm_type = img.dcmType
            img_metrics: dict = {}
            img_rois: list[dict] = []

            if not dcm_type or dcm_type not in DCM_TYPE_TASKS:
                img_metrics["skipReason"] = f"未知切面类型: {dcm_type!r}, 无法分流推理"
                per_image[img.imgId] = img_metrics
                continue

            tasks = DCM_TYPE_TASKS[dcm_type]
            ed_idx = es_idx = None  # PLAX 内部 LVID → IVS/LVPW 帧号依赖

            try:
                for task in tasks:
                    metric = task["metric"]
                    csv_path, stdout = self._run_task(task, img.imgPath, img.imgId, task_id, work_root, gpu_device)
                    value, rois = self._parse_by_rule(
                        task["value_rule"], metric, csv_path, stdout, ed_idx, es_idx,
                    )

                    # LVID 特殊: 产出 (edd, esd, ed_idx, es_idx) + 更新顶层主指标
                    if metric == "LVID":
                        edd, esd, ed_idx, es_idx = value
                        lvef = teichholz_lvef(edd, esd) if edd and esd else None
                        lvedd, lvesd = edd, esd
                        img_metrics["lvef"] = lvef
                        img_metrics["lvedd"] = edd
                        img_metrics["lvesd"] = esd
                    elif metric == "LA":
                        lad = value
                        img_metrics["lad"] = value
                    elif metric == "MV_EA":
                        # MV_EA 产出三个指标: mv_e, mv_a, mv_ea
                        mv_e, mv_a, mv_ea = value
                        img_metrics["mv_e"] = mv_e
                        img_metrics["mv_a"] = mv_a
                        img_metrics["mv_ea"] = mv_ea
                    else:
                        # IVS/LVPW/Aorta/AorticRoot/RVBase/IVC/PA/Doppler Vmax/TDI e'/TAPSE
                        img_metrics[metric.lower()] = value

                    img_rois.extend(rois)
            except Exception as error:
                # 单图失败记录 error, 不中断其他图 (一个 Doppler 失败不应让已成功的 PLAX 陪葬)
                logger.exception(
                    "心超单图推理失败 task_id=%s img_id=%s dcm_type=%s",
                    task_id,
                    img.imgId,
                    dcm_type,
                )
                img_metrics["error"] = (
                    str(error)
                    if isinstance(error, AlgorithmError)
                    else f"{dcm_type} 模型推理失败"
                )
                img_metrics["rois"] = img_rois
                per_image[img.imgId] = img_metrics
                continue

            img_metrics["rois"] = img_rois
            per_image[img.imgId] = img_metrics

        return {
            "lvef": lvef, "lvedd": lvedd, "lvesd": lvesd,
            "lad": lad, "mv_ea": mv_ea,
            "echo_per_image": per_image,
        }

    # ────────────────── 通用推理调度 ──────────────────

    def _run_task(self, task: dict, dcm_path: str, img_id: str, task_id: str, work_root: str | None, gpu_device: str | None = None) -> tuple:
        """执行单个推理任务, 返回 (csv_path, stdout)。

        输出目录: <work_root>/<task_id>/outputs/<img_id>/echo/<weights或metric>/
        输出文件: out_<metric>.avi (2D) 或 out_<metric>.jpg (Doppler/TAPSE)
        CSV 推断: out_<metric>.avi → out_<metric>_distance.csv
        """
        sub = task["weights"] or task["metric"].lower()
        out_dir = self._task_output_dir(img_id, task_id, work_root, sub)
        ext = ".avi" if task["script"] == "inference_2D_image.py" else ".jpg"
        out_file = os.path.join(out_dir, f"out_{task['metric']}{ext}")

        cmd = [self.python_executable, os.path.join(self.script_dir, task["script"])]
        if task["weights"] is not None:
            cmd += ["--model_weights", task["weights"]]
        cmd += ["--file_path", dcm_path, "--output_path", out_file]
        cmd += task["extra"]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=self.script_dir, env={**os.environ, **({"CUDA_VISIBLE_DEVICES": str(gpu_device)} if gpu_device is not None else {})})
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or e.stdout or "").strip()
            logger.error(
                "心超子进程失败 script=%s return_code=%s stderr=%s",
                task["script"],
                e.returncode,
                detail[-4000:],
            )
            raise EchoInferenceError(self._script_error_message(detail)) from e
        csv_path = out_file.replace(ext, "_distance.csv") if ext == ".avi" else None
        return csv_path, result.stdout

    @staticmethod
    def _script_error_message(detail: str) -> str:
        """把稳定的子进程错误特征翻译为可公开中文消息。"""
        normalized = detail.lower()
        if "out of memory" in normalized:
            return "心超模型显存不足，请稍后重试"
        if "no such file or directory" in normalized or "file not found" in normalized:
            return "心超输入或模型文件不存在"
        if "y0" in normalized and (
            "out of" in normalized or "outside" in normalized or "range" in normalized
        ):
            return "心超图像超出模型支持范围"
        return "心超模型推理失败"

    def _task_output_dir(self, img_id: str, task_id: str, work_root: str | None, sub: str) -> str:
        """构造任务隔离输出目录: <work_root>/<task_id>/outputs/<img_id>/echo/<sub>/。

        work_root 或 task_id 缺失时 fallback 到系统 temp (不隔离, 仅开发用)。
        目录永久保留, 不自动清理。
        """
        if work_root and task_id:
            out_dir = Path(work_root) / task_id / "outputs" / img_id / "echo" / sub
            out_dir.mkdir(parents=True, exist_ok=True)
            return str(out_dir)
        return tempfile.mkdtemp()

    # ────────────────── 通用指标解析 ──────────────────

    def _parse_by_rule(
        self, rule: str, metric: str, csv_path: str | None, stdout: str,
        ed_idx: int | None = None, es_idx: int | None = None,
    ) -> tuple:
        """按取值规则从 CSV/stdout 解析指标值和 ROI 线段。

        Returns:
            (value, rois)
            value: float, 或 LVID 专用 tuple (edd, esd, ed_idx, es_idx)
            rois: list[dict], 每项 {"type": roiType, "frameIndex": int, "points": [(x,y),(x,y)]}
                  Doppler/TDI/M-Mode 返回空 rois (无坐标数据)
        """
        # ── stdout 类规则 (MV_EA / TAPSE / Doppler Vmax / TDI) ──
        if rule == "stdout_ea":
            # MV_EA 脚本输出三行: E_Vel / A_Vel / E/A
            e_match = re.search(r"E_Vel\s*=\s*([\d.]+)", stdout or "")
            a_match = re.search(r"A_Vel\s*=\s*([\d.]+)", stdout or "")
            ea_match = re.search(r"E/A\s*=\s*([\d.]+)", stdout or "")
            if not ea_match:
                raise ValueError(f"未解析到 E/A 值, 终端输出: {(stdout or '')[-200:]}")
            mv_e = round(float(e_match.group(1)), 2) if e_match else None
            mv_a = round(float(a_match.group(1)), 2) if a_match else None
            mv_ea = round(float(ea_match.group(1)), 3)
            return (mv_e, mv_a, mv_ea), []

        if rule == "stdout_tapse":
            match = re.search(r"TAPSE\s*[:=]\s*([\d.]+)", stdout or "")
            if not match:
                raise ValueError(f"未解析到 TAPSE 值, 终端输出: {(stdout or '')[-200:]}")
            tapse_cm = float(match.group(1))
            return round(tapse_cm * 10, 2), []

        if rule == "stdout_vmax":
            # Doppler/TDI 脚本输出: Peak Velocity = X cm/s
            match = re.search(r"Peak Velocity\s*=\s*([\d.]+)", stdout or "")
            if not match:
                raise ValueError(f"未解析到 Peak Velocity, 终端输出: {(stdout or '')[-200:]}")
            return round(float(match.group(1)), 2), []

        # ── CSV 类规则 (B-Mode) ──
        if not csv_path or not os.path.exists(csv_path):
            raise ValueError(f"CSV 文件不存在: {csv_path}")

        df = pd.read_csv(csv_path)
        dist_col = [c for c in df.columns if "distance" in c.lower() or "diameter" in c.lower()]
        if not dist_col:
            raise ValueError(f"CSV 无 distance/diameter 列: {df.columns.tolist()}")
        distances = df[dist_col[0]].dropna()

        # ── LVID: ED=max→LVEDD, ES=min→LVESD, cm→mm ──
        if rule == "ed_es":
            edd_idx = int(distances.idxmax())
            esd_idx = int(distances.idxmin())
            edd = round(float(distances.max()) * 10, 2)
            esd = round(float(distances.min()) * 10, 2)
            rois = self._extract_roi_segments(df, [("LVEDD", edd_idx), ("LVESD", esd_idx)])
            return (edd, esd, edd_idx, esd_idx), rois

        # ── IVS/LVPW: 取 LVID ED 帧号的值, cm→mm (需 LVID 先跑) ──
        if rule == "ed_frame":
            if ed_idx is None:
                raise ValueError("ed_frame 规则需要 LVID 先跑提供 ed_idx")
            value = round(float(df[dist_col[0]].iloc[ed_idx]) * 10, 2)
            rois = self._extract_roi_segments(df, [(metric, ed_idx)])
            return value, rois

        # ── B-Mode 取最大值 (LA), cm→mm ──
        if rule == "max_bm":
            max_idx = int(distances.idxmax())
            value = round(float(distances.max()) * 10, 2)
            rois = self._extract_roi_segments(df, [("LA", max_idx)])
            return value, rois

        # ── B-Mode 取均值 (血管类 Aorta/AorticRoot/RVBase/IVC/PA), cm→mm ──
        if rule == "mean_bm":
            value = round(float(distances.mean()) * 10, 2)
            # mean 无特定帧, 取最接近均值的帧作为代表
            mean_idx = int((distances - distances.mean()).abs().idxmin())
            rois = self._extract_roi_segments(df, [(metric, mean_idx)])
            return value, rois

        raise ValueError(f"未知取值规则: {rule!r}")

    def _extract_roi_segments(self, df: pd.DataFrame, segments_spec: list[tuple[str, int]]) -> list[dict]:
        """从 CSV 指定行提取 ROI 线段坐标。

        Args:
            df: 含 pred_x1/pred_y1/pred_x2/pred_y2 列的 DataFrame
            segments_spec: [(roiType, frameIndex), ...]
        Returns:
            [{"type": roiType, "frameIndex": int, "points": [(x1,y1),(x2,y2)]}, ...]
        """
        segments = []
        for roi_type, idx in segments_spec:
            row = df.iloc[idx]
            segments.append({
                "type": roi_type,
                "frameIndex": int(idx),
                "points": [
                    (int(row["pred_x1"]), int(row["pred_y1"])),
                    (int(row["pred_x2"]), int(row["pred_y2"])),
                ],
            })
        return segments
