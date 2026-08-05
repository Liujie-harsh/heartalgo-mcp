"""
心衰诊断规则引擎测试。

测试行为 (非实现):
  1. HF 分型: 由 LVEF 阈值决定 (HFrEF <40 / HFmrEF 40-49 / HFpEF ≥50)
  2. 6 项指标异常标红: 按 handoff 参考范围判定
  3. GLS=null: 不标红, 标注"未测量"
  4. 接口契约: analyze() 返回 {lvef, lvedd, lvesd, lad, ea, gls}

参考范围 (handoff.md L25-L32):
  LVEF  ≥50%        <50 标红
  LVEDD 42-58 mm    超范围标红
  LVESD 25-37 mm    超范围标红
  LAD   27-38 mm    超范围标红
  E/A   0.8-1.5     超范围标红
  GLS   ≤-16%       >-16 标红
"""
import pytest
from rules import classify_hf, flag_abnormal, analyze


# ────────────────── HF 分型 ──────────────────

class TestClassifyHF:
    """HF 分型完全由 LVEF 阈值决定, 不让 LLM 推理。"""

    def test_lvef_below_40_is_hfref(self):
        # 黄锡南实测 LVEF=35.48 (心衰病例), 应判 HFrEF
        assert classify_hf(lvef=35.48) == "HFrEF"

    def test_lvef_40_is_hfmrEF_boundary(self):
        # 40 是 HFmrEF 下界 (含)
        assert classify_hf(lvef=40) == "HFmrEF"

    def test_lvef_49_is_hfmrEF_upper_boundary(self):
        # 49 是 HFmrEF 上界 (含)
        assert classify_hf(lvef=49) == "HFmrEF"

    def test_lvef_50_is_hfpef_boundary(self):
        # 50 是 HFpEF 下界 (含)
        assert classify_hf(lvef=50) == "HFpEF"

    def test_lvef_65_is_hfpef(self):
        assert classify_hf(lvef=65) == "HFpEF"


# ────────────────── 指标异常标红 ──────────────────

class TestFlagAbnormal:
    """6 项指标按参考范围判定是否异常 (标红)。"""

    def test_lvef_below_50_is_abnormal(self):
        assert flag_abnormal("LVEF", 35) is True

    def test_lvef_50_is_normal(self):
        assert flag_abnormal("LVEF", 50) is False

    def test_lvedd_within_42_58_is_normal(self):
        assert flag_abnormal("LVEDD", 50) is False

    def test_lvedd_above_58_is_abnormal(self):
        assert flag_abnormal("LVEDD", 60) is True

    def test_lvedd_below_42_is_abnormal(self):
        assert flag_abnormal("LVEDD", 40) is True

    def test_lvesd_within_25_37_is_normal(self):
        assert flag_abnormal("LVESD", 30) is False

    def test_lvesd_above_37_is_abnormal(self):
        assert flag_abnormal("LVESD", 40) is True

    def test_lad_above_38_is_abnormal(self):
        assert flag_abnormal("LAD", 42) is True

    def test_ea_within_08_15_is_normal(self):
        # 高汉平 00020 PW 实测 E/A=2.02, 超出 1.5 上界 → 异常
        assert flag_abnormal("E/A", 1.2) is False

    def test_ea_above_15_is_abnormal(self):
        assert flag_abnormal("E/A", 2.02) is True

    def test_gls_above_minus16_is_abnormal(self):
        # GLS > -16 标红 (如 -12 大于 -16)
        assert flag_abnormal("GLS", -12) is True

    def test_gls_equal_minus16_is_normal(self):
        assert flag_abnormal("GLS", -16) is False

    def test_gls_null_is_not_abnormal(self):
        # GLS 砍掉返回 null, 不标红 (架构决策)
        assert flag_abnormal("GLS", None) is False


# ────────────────── 接口契约 (端到端) ──────────────────

class TestAnalyzeContract:
    """analyze() 返回结构必须包含 6 项指标, 契约稳定。"""

    def test_returns_six_metrics_keys(self):
        # handoff L34 契约: 6 项指标必须存在; hf_type 为报告附加字段
        result = analyze(lvef=35.48, lvedd=55.0, lvesd=40.0, lad=35.0, ea=2.02, gls=None)
        required = {"lvef", "lvedd", "lvesd", "lad", "ea", "gls"}
        assert required.issubset(result.keys())

    def test_gls_null_preserved(self):
        # GLS 砍掉, null 必须原样保留在输出里
        result = analyze(lvef=35.48, lvedd=55.0, lvesd=40.0, lad=35.0, ea=2.02, gls=None)
        assert result["gls"] is None

    def test_includes_hf_classification(self):
        # 报告需输出心衰分型诊断
        result = analyze(lvef=35.48, lvedd=55.0, lvesd=40.0, lad=35.0, ea=2.02, gls=None)
        assert result["hf_type"] == "HFrEF"
