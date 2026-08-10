"""EchoNet runner 测试。

覆盖:
  - teichholz_lvef(): Teichholz 公式纯函数 (无需 torch/GPU)
  - SLICE_DIR_MAP: 12 切面→子目录映射
  - DCM_TYPE_TASKS: 12 切面→指标任务映射, PLAX 内部 LVID 依赖
  - _parse_by_rule(): 6 种取值规则 (ed_es/ed_frame/max_bm/mean_bm/stdout_ea/stdout_vmax/stdout_tapse)
  - run(): 切面分流 + 单图失败隔离 + {imgId: metrics} 返回结构

公式 (handoff.md L261):
  LVEF = (7*EDD³/(2.4+EDD) - 7*ESD³/(2.4+ESD)) / (7*EDD³/(2.4+EDD)) × 100
  输入 mm, 内部转 cm
"""
import os
import pandas as pd
import pytest

from echonet_runner import teichholz_lvef, SLICE_DIR_MAP, DCM_TYPE_TASKS, EchoNetRunner
from api import ImgItem


# ────────────────── Teichholz LVEF 纯函数 ──────────────────

class TestTeichholzLVEF:
    """Teichholz 法 LVEF 估算。"""

    def test_formula_self_consistent(self):
        # 公式自洽: EDD=58mm, ESD=31.6mm → LVEF≈35% (心衰)
        lvef = teichholz_lvef(edd_mm=58, esd_mm=31.6)
        assert 30 < lvef < 80

    def test_normal_lvef(self):
        # 正常心室: EDD=45mm, ESD=28mm → LVEF 应 ≥50%
        lvef = teichholz_lvef(edd_mm=45, esd_mm=28)
        assert lvef >= 50

    def test_dilated_ventricle_low_lvef(self):
        # 扩张心室: EDD=65mm, ESD=55mm → LVEF 偏低 (<35%)
        lvef = teichholz_lvef(edd_mm=65, esd_mm=55)
        assert lvef < 35

    def test_esd_zero_is_full_ef(self):
        # ESD=0 → Ves=0 → LVEF=100% (数学边界)
        lvef = teichholz_lvef(edd_mm=50, esd_mm=0)
        assert lvef == 100.0

    def test_esd_equals_edd_is_zero_ef(self):
        # ESD=EDD → 无收缩 → LVEF=0% (数学边界)
        lvef = teichholz_lvef(edd_mm=50, esd_mm=50)
        assert lvef == 0.0

    def test_handoff_verified_value(self):
        # handoff.md L113/L301: 自算 Teichholz (全局 max/min) 黄锡南约 68.65% (HFpEF)。
        # 35.48% 是脚本 phase_estimate 偶发输出值, 非自算结果, 不在此测试范围。
        lvef = teichholz_lvef(edd_mm=50.59, esd_mm=31.06)
        assert abs(lvef - 68.65) < 1.0


# ────────────────── SLICE_DIR_MAP 映射表 ──────────────────

class TestSliceDirMap:
    """12 切面→子目录映射 (SLICE_DIR_MAP)。"""

    def test_has_12_slice_types(self):
        # handoff: 12 个切面 (4 B-Mode + 5 Doppler + 2 TDI + 1 M-Mode)
        assert len(SLICE_DIR_MAP) == 12

    def test_b_mode_slices(self):
        # B-Mode: PLAX / A4C / Subcostal / RVOT
        assert SLICE_DIR_MAP["PLAX"] == "plax"
        assert SLICE_DIR_MAP["A4C"] == "a4c"
        assert SLICE_DIR_MAP["Subcostal"] == "subcostal"
        assert SLICE_DIR_MAP["RVOT"] == "rvot"

    def test_doppler_slices(self):
        # Doppler: MV_EA / AV_Vmax / TR_Vmax / MR_Vmax / LVOT_Vmax
        assert SLICE_DIR_MAP["MV_EA"] == "MV_EA"
        assert SLICE_DIR_MAP["AV_Vmax"] == "AV_Vmax"
        assert SLICE_DIR_MAP["TR_Vmax"] == "TR_Vmax"
        assert SLICE_DIR_MAP["MR_Vmax"] == "MR_Vmax"
        assert SLICE_DIR_MAP["LVOT_Vmax"] == "LVOT_Vmax"

    def test_tdi_slices(self):
        # TDI: TDI_Medial / TDI_Lateral
        assert SLICE_DIR_MAP["TDI_Medial"] == "TDI_Medial"
        assert SLICE_DIR_MAP["TDI_Lateral"] == "TDI_Lateral"

    def test_mmode_slice(self):
        # M-Mode: TAPSE
        assert SLICE_DIR_MAP["TAPSE"] == "TAPSE"


# ────────────────── DCM_TYPE_TASKS 切面分流表 ──────────────────

class TestDcmTypeTasks:
    """12 切面→指标任务映射 (DCM_TYPE_TASKS)。"""

    def test_plax_has_6_tasks_and_lvid_first(self):
        # PLAX: LVID 必须第一个跑 (产出 ED/ES 帧号供 IVS/LVPW)
        tasks = DCM_TYPE_TASKS["PLAX"]
        assert len(tasks) == 6
        assert tasks[0]["metric"] == "LVID"
        metrics = [t["metric"] for t in tasks]
        assert metrics == ["LVID", "IVS", "LVPW", "LA", "Aorta", "AorticRoot"]

    def test_plax_lvid_has_phase_estimate(self):
        # LVID 需要 --phase_estimate 产出 ED/ES 帧号
        lvid_task = DCM_TYPE_TASKS["PLAX"][0]
        assert "--phase_estimate" in lvid_task["extra"]
        assert lvid_task["value_rule"] == "ed_es"

    def test_plax_ivs_lvpw_use_ed_frame_rule(self):
        # IVS/LVPW 用 ed_frame 规则 (依赖 LVID 的 ed_idx)
        tasks = {t["metric"]: t for t in DCM_TYPE_TASKS["PLAX"]}
        assert tasks["IVS"]["value_rule"] == "ed_frame"
        assert tasks["LVPW"]["value_rule"] == "ed_frame"

    def test_a4c_has_rvbase(self):
        assert len(DCM_TYPE_TASKS["A4C"]) == 1
        assert DCM_TYPE_TASKS["A4C"][0]["metric"] == "RVBase"

    def test_mv_ea_uses_stdout_ea_rule(self):
        # MV_EA 从 stdout 解析 E/A, 无 CSV
        task = DCM_TYPE_TASKS["MV_EA"][0]
        assert task["value_rule"] == "stdout_ea"
        assert task["weights"] is None

    def test_doppler_vmax_uses_stdout_vmax_rule(self):
        # Doppler Vmax 从 stdout 解析 Peak Velocity
        for dcm_type in ("AV_Vmax", "TR_Vmax", "MR_Vmax", "LVOT_Vmax"):
            task = DCM_TYPE_TASKS[dcm_type][0]
            assert task["value_rule"] == "stdout_vmax"

    def test_tdi_uses_stdout_vmax_rule(self):
        # TDI e' 从 stdout 解析 Peak Velocity
        for dcm_type in ("TDI_Medial", "TDI_Lateral"):
            task = DCM_TYPE_TASKS[dcm_type][0]
            assert task["value_rule"] == "stdout_vmax"

    def test_tapse_uses_stdout_tapse_rule(self):
        task = DCM_TYPE_TASKS["TAPSE"][0]
        assert task["value_rule"] == "stdout_tapse"
        assert task["weights"] is None

    def test_all_12_dcm_types_present(self):
        # DCM_TYPE_TASKS 覆盖全部 12 切面
        assert set(DCM_TYPE_TASKS.keys()) == set(SLICE_DIR_MAP.keys())


# ────────────────── _parse_by_rule 取值规则 ──────────────────

def _make_csv(tmp_path, name, distances, frames=None):
    """构造 EchoNet distance CSV: frame_number, pred_x1, pred_y1, pred_x2, pred_y2, diameter。"""
    frames = frames or list(range(len(distances)))
    df = pd.DataFrame({
        "frame_number": frames,
        "pred_x1": [300 + i for i in range(len(distances))],
        "pred_y1": [200 + i for i in range(len(distances))],
        "pred_x2": [400 + i for i in range(len(distances))],
        "pred_y2": [300 + i for i in range(len(distances))],
        "diameter": distances,
    })
    path = tmp_path / name
    df.to_csv(path, index=False)
    return str(path)


class TestParseByRule:
    """6 种取值规则 (ed_es/ed_frame/max_bm/mean_bm/stdout_ea/stdout_vmax/stdout_tapse)。"""

    @pytest.fixture
    def runner(self):
        return EchoNetRunner(script_dir=".", python_executable="python")

    def test_ed_es_rule(self, runner, tmp_path):
        # LVID: ED=max→LVEDD, ES=min→LVESD, cm→mm (×10)
        csv_path = _make_csv(tmp_path, "lvid.csv", [3.0, 5.0, 4.0, 2.0])
        value, rois = runner._parse_by_rule("ed_es", "LVID", csv_path, "")
        edd, esd, ed_idx, es_idx = value
        assert edd == 50.0  # 5.0 cm × 10
        assert esd == 20.0  # 2.0 cm × 10
        assert ed_idx == 1  # max at index 1
        assert es_idx == 3  # min at index 3
        # rois: LVEDD + LVESD 各 2 端点 = 2 条线段
        assert len(rois) == 2
        assert rois[0]["type"] == "LVEDD"
        assert rois[1]["type"] == "LVESD"
        assert len(rois[0]["points"]) == 2

    def test_ed_frame_rule(self, runner, tmp_path):
        # IVS/LVPW: 取 LVID ED 帧号的值, cm→mm
        csv_path = _make_csv(tmp_path, "ivs.csv", [0.6, 0.9, 0.7, 0.5])
        value, rois = runner._parse_by_rule("ed_frame", "IVS", csv_path, "", ed_idx=1)
        assert value == 9.0  # 0.9 cm × 10
        assert len(rois) == 1
        assert rois[0]["type"] == "IVS"

    def test_ed_frame_rule_without_ed_idx_raises(self, runner, tmp_path):
        # ed_frame 规则需要 LVID 先跑提供 ed_idx
        csv_path = _make_csv(tmp_path, "ivs.csv", [0.6, 0.9])
        with pytest.raises(ValueError, match="ed_idx"):
            runner._parse_by_rule("ed_frame", "IVS", csv_path, "", ed_idx=None)

    def test_max_bm_rule(self, runner, tmp_path):
        # LA: 取最大值, cm→mm
        csv_path = _make_csv(tmp_path, "la.csv", [2.0, 3.5, 3.0, 2.5])
        value, rois = runner._parse_by_rule("max_bm", "LA", csv_path, "")
        assert value == 35.0  # 3.5 cm × 10
        assert len(rois) == 1
        assert rois[0]["type"] == "LA"

    def test_mean_bm_rule(self, runner, tmp_path):
        # Aorta: 取均值, cm→mm
        csv_path = _make_csv(tmp_path, "aorta.csv", [2.0, 3.0, 2.5, 2.5])
        value, rois = runner._parse_by_rule("mean_bm", "Aorta", csv_path, "")
        assert value == 25.0  # mean(2.0,3.0,2.5,2.5)=2.5 cm × 10
        assert len(rois) == 1

    def test_stdout_ea_rule(self, runner):
        # MV_EA: 终端解析 E_Vel / A_Vel / E/A
        stdout = "E_Vel = 99.75\nA_Vel = 49.37\nE/A = 2.02"
        value, rois = runner._parse_by_rule("stdout_ea", "MV_EA", None, stdout)
        mv_e, mv_a, mv_ea = value
        assert mv_e == 99.75
        assert mv_a == 49.37
        assert mv_ea == 2.02
        assert rois == []

    def test_stdout_ea_rule_missing_raises(self, runner):
        # 未解析到 E/A → ValueError
        with pytest.raises(ValueError, match="E/A"):
            runner._parse_by_rule("stdout_ea", "MV_EA", None, "no output")

    def test_stdout_vmax_rule(self, runner):
        # Doppler: Peak Velocity 解析
        stdout = "Peak Velocity = 64.12 cm/s"
        value, rois = runner._parse_by_rule("stdout_vmax", "TR_Vmax", None, stdout)
        assert value == 64.12
        assert rois == []

    def test_stdout_vmax_rule_missing_raises(self, runner):
        with pytest.raises(ValueError, match="Peak Velocity"):
            runner._parse_by_rule("stdout_vmax", "TR_Vmax", None, "no output")

    def test_stdout_tapse_rule(self, runner):
        # TAPSE: 位移解析
        stdout = "TAPSE = 1.87"
        value, rois = runner._parse_by_rule("stdout_tapse", "TAPSE", None, stdout)
        assert value == 1.87
        assert rois == []

    def test_csv_not_found_raises(self, runner, tmp_path):
        # CSV 不存在 → ValueError
        with pytest.raises(ValueError, match="CSV"):
            runner._parse_by_rule("ed_es", "LVID", str(tmp_path / "missing.csv"), "")

    def test_csv_no_distance_column_raises(self, runner, tmp_path):
        # CSV 无 distance/diameter 列 → ValueError
        df = pd.DataFrame({"frame_number": [0, 1], "value": [1.0, 2.0]})
        path = tmp_path / "no_dist.csv"
        df.to_csv(path, index=False)
        with pytest.raises(ValueError, match="distance"):
            runner._parse_by_rule("ed_es", "LVID", str(path), "")


# ────────────────── run() 切面分流 + 单图失败隔离 ──────────────────

class TestRunDispatch:
    """run() 按 dcmType 分流 + {imgId: metrics} 返回 + 单图失败隔离。"""

    @pytest.fixture
    def runner(self):
        return EchoNetRunner(script_dir=".", python_executable="python")

    def test_skip_unknown_dcm_type(self, runner):
        # 未知 dcmType → skipReason, 不进任何推理分支
        imgs = [ImgItem(imgId="x", imgPath="x.dcm", imgType="CARDIAC_ULTRASOUND", dcmType="Unknown")]
        result = runner.run(imgs)
        assert result["lvef"] is None
        per_img = result["echo_per_image"]["x"]
        assert "skipReason" in per_img

    def test_skip_none_dcm_type(self, runner):
        # dcmType=None → skipReason
        imgs = [ImgItem(imgId="x", imgPath="x.dcm", imgType="CARDIAC_ULTRASOUND", dcmType=None)]
        result = runner.run(imgs)
        assert "skipReason" in result["echo_per_image"]["x"]

    def test_filters_non_echo_imgs(self, runner):
        # ECG 图不进心超推理
        imgs = [ImgItem(imgId="ecg-1", imgPath="ecg.xml", imgType="ECG")]
        result = runner.run(imgs)
        assert result["echo_per_image"] == {}
        assert result["lvef"] is None

    def test_plax_run_with_mocked_subprocess(self, runner, tmp_path, monkeypatch):
        """PLAX 完整分流: LVID→IVS→LVPW→LA→Aorta→AorticRoot, mock subprocess + 预建 CSV。"""
        # 为每个 task 预建 CSV
        csv_contents = {
            "LVID":       [3.5, 5.06, 4.0, 3.16],  # EDD=50.6mm, ESD=31.6mm
            "IVS":        [0.6, 0.9, 0.7, 0.5],
            "LVPW":       [0.6, 0.9, 0.7, 0.5],
            "LA":         [2.0, 3.91, 3.0, 2.5],   # LAD=39.1mm
            "Aorta":      [2.5, 3.0, 2.8, 2.7],
            "AorticRoot": [2.5, 3.0, 2.8, 2.7],
        }
        csv_paths = {}
        for metric, dists in csv_contents.items():
            csv_paths[metric] = _make_csv(tmp_path, f"out_{metric}_distance.csv", dists)

        def fake_run_task(task, dcm_path, img_id, task_id, work_root, gpu_device=None):
            return csv_paths[task["metric"]], ""

        monkeypatch.setattr(runner, "_run_task", fake_run_task)

        imgs = [ImgItem(imgId="plax-1", imgPath="plax.dcm",
                        imgType="CARDIAC_ULTRASOUND", dcmType="PLAX")]
        result = runner.run(imgs, task_id="t1", work_root=str(tmp_path))

        # 顶层主指标
        assert result["lvef"] is not None
        assert result["lvedd"] == 50.6
        assert result["lvesd"] == 31.6
        assert result["lad"] == 39.1
        # per-image 含全部 6 指标
        per_img = result["echo_per_image"]["plax-1"]
        assert per_img["lvedd"] == 50.6
        assert per_img["lvesd"] == 31.6
        assert "ivs" in per_img
        assert "lvpw" in per_img
        assert "aorta" in per_img
        assert "aorticroot" in per_img
        # rois: LVID(2) + IVS(1) + LVPW(1) + LA(1) + Aorta(1) + AorticRoot(1) = 7 条线段
        assert len(per_img["rois"]) == 7

    def test_mv_ea_run_with_mocked_stdout(self, runner, tmp_path, monkeypatch):
        """MV_EA: stdout 解析 E_Vel/A_Vel/E/A, rois 为空。"""
        def fake_run_task(task, dcm_path, img_id, task_id, work_root, gpu_device=None):
            return None, "E_Vel = 99.75\nA_Vel = 49.37\nE/A = 2.02"

        monkeypatch.setattr(runner, "_run_task", fake_run_task)

        imgs = [ImgItem(imgId="mv-1", imgPath="mv.dcm",
                        imgType="CARDIAC_ULTRASOUND", dcmType="MV_EA")]
        result = runner.run(imgs, task_id="t1", work_root=str(tmp_path))

        per_img = result["echo_per_image"]["mv-1"]
        assert per_img["mv_e"] == 99.75
        assert per_img["mv_a"] == 49.37
        assert per_img["mv_ea"] == 2.02
        assert per_img["rois"] == []
        # 顶层 mv_ea
        assert result["mv_ea"] == 2.02

    def test_single_image_failure_isolated(self, runner, tmp_path, monkeypatch):
        """单图失败记录 error, 不中断其他图 (阶段 2 per-image try/except)。"""
        call_count = [0]

        def fake_run_task(task, dcm_path, img_id, task_id, work_root, gpu_device=None):
            call_count[0] += 1
            if img_id == "bad":
                raise RuntimeError("phase_estimate failed")
            return _make_csv(tmp_path, f"out_{task['metric']}_distance.csv",
                             [3.5, 5.06, 4.0, 3.16]), ""

        monkeypatch.setattr(runner, "_run_task", fake_run_task)

        imgs = [
            ImgItem(imgId="bad", imgPath="bad.dcm",
                    imgType="CARDIAC_ULTRASOUND", dcmType="PLAX"),
            ImgItem(imgId="good", imgPath="good.dcm",
                    imgType="CARDIAC_ULTRASOUND", dcmType="PLAX"),
        ]
        result = runner.run(imgs, task_id="t1", work_root=str(tmp_path))

        # bad 图有 error, good 图正常
        assert "error" in result["echo_per_image"]["bad"]
        assert "lvedd" in result["echo_per_image"]["good"]
        # 顶层主指标来自 good 图 (后跑覆盖)
        assert result["lvedd"] == 50.6
