"""
EchoNet 真实推理 runner (生产用, 需 torch+GPU)。

调用已有推理脚本:
  - inference_2D_image.py --model_weights lvid --phase_estimate → LVEDD/LVESD → Teichholz LVEF
  - inference_2D_image.py --model_weights la                    → LAD
  - inference_MV_EperA.py                                        → E/A
  - GLS = None (架构决策, 砍掉)

在服务器 G:\\meaurements\\measurements\\Measurement\\ 下运行。
本机无 torch, 无法测试; 通过 FakeRunner 覆盖 API 契约。

任务目录隔离 (handoff L517-L552):
  work_root/task_id/outputs/<imgId>/echo/{lvid,la,mvpeak}/...
  产物永久保留, 不自动清理。
"""
import os
import re
import subprocess
import tempfile
from pathlib import Path
import pandas as pd
from typing import Protocol
from api import ImgItem


# ────────────────── Teichholz 公式 (纯函数, 可测) ──────────────────

def teichholz_lvef(edd_mm: float, esd_mm: float) -> float:
    """
    Teichholz 法估算 LVEF。

    LVEF = (Ved - Ves) / Ved * 100
    V = 7 * D³ / (2.4 + D)   (Teichholz 校正)

    handoff.md L261 验证: EDD≈5.06cm, ESD≈3.16cm → LVEF=35.48%
    """
    edd_cm = edd_mm / 10.0
    esd_cm = esd_mm / 10.0
    v_ed = 7 * edd_cm ** 3 / (2.4 + edd_cm)
    v_es = 7 * esd_cm ** 3 / (2.4 + esd_cm)
    lvef = (v_ed - v_es) / v_ed * 100
    return round(lvef, 2)


# ────────────────── 真实推理 runner ──────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


class EchoNetRunner:
    """
    生产推理器: subprocess 调用推理脚本, 解析 CSV/终端输出。

    放在服务器推理脚本同目录 (G:\\meaurements\\...\\Measurement\\)。
    """

    def __init__(self, script_dir: str = None):
        self.script_dir = script_dir or SCRIPT_DIR

    def run(self, imgs: list[ImgItem], task_id: str = "", work_root: str | None = None) -> dict:
        """
        输入 imgs, 返回超声指标 dict。

        约定: 心超文件按 imgPath 传入, runner 自动判断类型:
          - 2D B-mode (含 cm region) → lvid + la
          - Spectral Doppler → mvpeak E/A

        每份 imgId 独立推理, 结果按 {imgId: metrics} 结构返回 (handoff L424 决策)。
        主指标 (lvef/lvedd/lvesd/lad/ea/gls) 保留在顶层供 _execute 走 rules.analyze。
        """
        echo_files = [img for img in imgs if img.imgType == "Cardiac Ultrasound"]
        per_image: dict[str, dict] = {}

        lvef = lvedd = lvesd = lad = None
        ea = None

        for img in echo_files:
            dcm_path = img.imgPath
            img_metrics: dict = {"gls": None}
            img_rois: list[dict] = []  # 结构化线段 [{type, frameIndex, points}]

            try:
                region_type = self._detect_region_type(dcm_path)

                if region_type == "2D":
                    lvedd, lvesd, lvid_rois = self._run_lvid(dcm_path, img.imgId, task_id, work_root)
                    if lvedd and lvesd:
                        lvef = teichholz_lvef(lvedd, lvesd)
                    lad, la_rois = self._run_la(dcm_path, img.imgId, task_id, work_root)
                    img_metrics.update({"lvef": lvef, "lvedd": lvedd, "lvesd": lvesd, "lad": lad})
                    img_rois = lvid_rois + la_rois  # LVEDD + LVESD + LAD = 3条线段
                elif region_type == "Doppler":
                    ea = self._run_mvpeak(dcm_path, img.imgId, task_id, work_root)
                    img_metrics["ea"] = ea
                # region_type == "skip": 单帧图, 不跑推理, img_metrics 只保留 gls=None + rois=[]
            except Exception as exc:
                # 阶段 2: 单图失败不中断整任务, 记录错误信息到 per_image
                img_metrics = {"gls": None, "rois": [], "error": str(exc)}

            img_metrics["rois"] = img_rois
            per_image[img.imgId] = img_metrics

        return {
            "lvef": lvef, "lvedd": lvedd, "lvesd": lvesd,
            "lad": lad, "ea": ea, "gls": None,
            "echo_per_image": per_image,
        }

    def _task_output_dir(self, img_id: str, task_id: str, work_root: str | None, sub: str) -> str:
        """构造任务隔离输出目录: <work_root>/<task_id>/outputs/<img_id>/echo/<sub>/。

        work_root 或 task_id 缺失时 fallback 到系统 temp (不隔离, 仅开发用)。
        目录永久保留, 不自动清理 (handoff L527 决策)。
        """
        if work_root and task_id:
            out_dir = Path(work_root) / task_id / "outputs" / img_id / "echo" / sub
            out_dir.mkdir(parents=True, exist_ok=True)
            return str(out_dir)
        return tempfile.mkdtemp()

    def _detect_region_type(self, dcm_path: str) -> str:
        """判断 DICOM 类型: skip / 2D / Doppler。

        - skip: 单帧图 (nframes<=1), EchoNet-LVEF 需多帧视频做 phase_estimate, 单帧图跳过
        - Doppler: 含 Spectral Doppler region (SpatialFormat==3)
        - 2D: 多帧且非 Doppler, 跑 lvid + la
        """
        import pydicom
        ds = pydicom.dcmread(dcm_path, stop_before_pixels=True)
        # 单帧图无法做 phase_estimate (需要逐帧分析心动周期), 跳过
        num_frames = int(getattr(ds, "NumberOfFrames", 1))
        if num_frames <= 1:
            return "skip"
        if (0x0018, 0x6011) not in ds:
            return "2D"
        for region in ds[(0x0018, 0x6011)].value:
            sf = region[(0x0018, 0x6012)].value if (0x0018, 0x6012) in region else None
            if sf == 3:  # Spectral Doppler
                return "Doppler"
        return "2D"

    def _run_lvid(self, dcm_path: str, img_id: str = "", task_id: str = "", work_root: str | None = None) -> tuple:
        """跑 lvid + phase_estimate, CSV 取 ESD/EDD (mm), LVEF 由 run() 用 Teichholz 自算。

        返回 (EDD_mm, ESD_mm, segments)。
        segments: [LVEDD 线段, LVESD 线段], 每个线段含 type/frameIndex/points(2点)
        """
        out_dir = self._task_output_dir(img_id, task_id, work_root, "lvid")
        out_avi = os.path.join(out_dir, "out_lvid.avi")
        cmd = [
            "python", os.path.join(self.script_dir, "inference_2D_image.py"),
            "--model_weights", "lvid", "--phase_estimate",
            "--file_path", dcm_path, "--output_path", out_avi,
        ]
        subprocess.run(cmd, check=True, cwd=self.script_dir)
        csv_path = out_avi.replace(".avi", "_distance.csv")
        return self._parse_lvid_distance(csv_path)

    def _run_la(self, dcm_path: str, img_id: str = "", task_id: str = "", work_root: str | None = None) -> tuple:
        """跑 la, 解析 _distance.csv 取 LAD (mm)。

        返回 (LAD_mm, segments)。
        segments: [LAD 线段], 含 type/frameIndex/points(2点)
        """
        out_dir = self._task_output_dir(img_id, task_id, work_root, "la")
        out_avi = os.path.join(out_dir, "out_la.avi")
        cmd = [
            "python", os.path.join(self.script_dir, "inference_2D_image.py"),
            "--model_weights", "la",
            "--file_path", dcm_path, "--output_path", out_avi,
        ]
        subprocess.run(cmd, check=True, cwd=self.script_dir)
        csv_path = out_avi.replace(".avi", "_distance.csv")
        return self._parse_la_distance(csv_path)

    def _run_mvpeak(self, dcm_path: str, img_id: str = "", task_id: str = "", work_root: str | None = None) -> float:
        """跑 mvpeak E/A, 解析终端输出取 E/A 比值。"""
        out_dir = self._task_output_dir(img_id, task_id, work_root, "mvpeak")
        out_jpg = os.path.join(out_dir, "out_mvpeak.jpg")
        cmd = [
            "python", os.path.join(self.script_dir, "inference_MV_EperA.py"),
            "--file_path", dcm_path, "--output_path", out_jpg,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=self.script_dir)
        # 终端输出含 "E/A   = 2.02"
        match = re.search(r"E/A\s*=\s*([\d.]+)", result.stdout)
        if not match:
            raise ValueError(f"未解析到 E/A 值, 输出: {result.stdout[-200:]}")
        return round(float(match.group(1)), 3)

    def _parse_lvid_distance(self, csv_path: str) -> tuple:
        """从 lvid _distance.csv 取 ESD(最小) 和 EDD(最大), 单位 mm。

        注意: 推理脚本 CSV 的 diameter 单位是 cm (Physical delta≈0.033 cm),
        Teichholz 公式需要 mm 输入 → 返回前 ×10 转 mm。
        handoff L261 验证: EDD≈5.06cm→50.6mm, ESD≈3.16cm→31.6mm → LVEF=35.48%。

        Returns:
            (edd_mm, esd_mm, segments)
            segments: [
                {"type": "LVEDD", "frameIndex": int, "points": [(x1,y1),(x2,y2)]},
                {"type": "LVESD", "frameIndex": int, "points": [(x1,y1),(x2,y2)]},
            ]
        """
        df = pd.read_csv(csv_path)
        # distance 列名可能因脚本版本不同, 取含 distance 的列
        dist_col = [c for c in df.columns if "distance" in c.lower() or "diameter" in c.lower()]
        if not dist_col:
            raise ValueError(f"CSV 无 distance 列: {df.columns.tolist()}")
        distances = df[dist_col[0]].dropna()
        edd_idx = distances.idxmax()  # 舒张末 (最大) 帧号
        esd_idx = distances.idxmin()  # 收缩末 (最小) 帧号
        esd = round(float(distances.min()) * 10, 2)  # cm→mm
        edd = round(float(distances.max()) * 10, 2)  # cm→mm
        # 结构化线段: LVEDD 帧 + LVESD 帧, 每帧2点 (LV 直径两端)
        segments = []
        for seg_type, idx in (("LVEDD", edd_idx), ("LVESD", esd_idx)):
            row = df.loc[idx]
            segments.append({
                "type": seg_type,
                "frameIndex": int(idx),
                "points": [
                    (int(row["pred_x1"]), int(row["pred_y1"])),
                    (int(row["pred_x2"]), int(row["pred_y2"])),
                ],
            })
        return edd, esd, segments

    def _parse_la_distance(self, csv_path: str) -> tuple:
        """从 la _distance.csv 取 LAD (收缩末最大值), 单位 mm。

        与 lvid 同理: CSV diameter 单位是 cm, 返回前 ×10 转 mm。

        Returns:
            (lad_mm, segments)
            segments: [{"type": "LAD", "frameIndex": int, "points": [(x1,y1),(x2,y2)]}]
        """
        df = pd.read_csv(csv_path)
        dist_col = [c for c in df.columns if "distance" in c.lower() or "diameter" in c.lower()]
        if not dist_col:
            raise ValueError(f"CSV 无 distance 列: {df.columns.tolist()}")
        distances = df[dist_col[0]].dropna()
        # LA 取收缩末最大值 (左房在收缩末最大), cm→mm
        lad = round(float(distances.max()) * 10, 2)
        # 结构化线段: LAD 最大直径帧, 2点
        max_idx = distances.idxmax()
        row = df.loc[max_idx]
        segments = [{
            "type": "LAD",
            "frameIndex": int(max_idx),
            "points": [
                (int(row["pred_x1"]), int(row["pred_y1"])),
                (int(row["pred_x2"]), int(row["pred_y2"])),
            ],
        }]
        return lad, segments
