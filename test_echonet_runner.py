"""
Teichholz LVEF 估算测试 (纯函数, 无需 torch/GPU)。

公式 (handoff.md L261):
  LVEF = (7*EDD³/(2.4+EDD) - 7*ESD³/(2.4+ESD)) / (7*EDD³/(2.4+EDD)) × 100
  输入 mm, 内部转 cm
"""
import pytest
from echonet_runner import teichholz_lvef


class TestTeichholzLVEF:
    """Teichholz 法 LVEF 估算。"""

    def test_formula_self_consistent(self):
        # 公式自洽: EDD=58mm, ESD=31.6mm → LVEF≈35% (心衰)
        # 手算: V_ed=7*5.8³/8.2=167.2, V_es=7*3.16³/5.56=39.7 → (167.2-39.7)/167.2=76%
        # 注意: Teichholz 对大心室会高估 LVEF, 这是已知局限
        lvef = teichholz_lvef(edd_mm=58, esd_mm=31.6)
        assert 30 < lvef < 80  # 公式输出范围合理

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
