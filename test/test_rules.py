"""
心衰诊断规则引擎测试。

测试行为 (非实现):
  1. HF 分型: 由 LVEF 阈值决定 (HFrEF <40 / HFmrEF 40-49 / HFpEF ≥50)
  2. 核心指标异常标红: 按参考范围判定
  3. 接口契约: analyze() 返回 {lvef, lvedd, lvesd, lad, ea, mv_ea, hf_type}
  4. v3 兼容: ea → mv_ea 字段名统一, 两者同时返回 (向后兼容)
  5. 阶段 2 容错: lvef=None → hf_type="未知" (所有 2D 图失败时不抛异常)

参考范围 (handoff.md L25-L32):
  LVEF  ≥50%        <50 标红
  LVEDD 42-58 mm    超范围标红
  LVESD 25-37 mm    超范围标红
  LAD   27-38 mm    超范围标红
  E/A   0.8-2.0     超范围标红
"""
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

    def test_lvef_none_returns_unknown(self):
        # 阶段 2 容错: 所有 2D 图失败时 lvef=None → "未知" (不抛 TypeError)
        assert classify_hf(lvef=None) == "未知"


# ────────────────── 指标异常标红 ──────────────────

class TestFlagAbnormal:
    """核心指标按参考范围判定是否异常 (标红)。"""

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

    def test_ea_within_08_20_is_normal(self):
        assert flag_abnormal("E/A", 1.8) is False

    def test_ea_above_20_is_abnormal(self):
        assert flag_abnormal("E/A", 2.02) is True

    def test_mv_ea_within_08_20_is_normal(self):
        # v3: MV_EA 与 E/A 共用参考范围
        assert flag_abnormal("MV_EA", 1.8) is False

    def test_mv_ea_above_20_is_abnormal(self):
        assert flag_abnormal("MV_EA", 2.02) is True


# ────────────────── 接口契约 (端到端) ──────────────────

class TestAnalyzeContract:
    """analyze() 返回结构必须包含核心指标, 契约稳定。"""

    def test_returns_six_metrics_keys(self):
        result = analyze(lvef=35.48, lvedd=55.0, lvesd=40.0, lad=35.0, ea=2.02)
        required = {"lvef", "lvedd", "lvesd", "lad", "ea", "mv_ea"}
        assert required.issubset(result.keys())
        assert "gls" not in result

    def test_includes_hf_classification(self):
        # 报告需输出心衰分型诊断
        result = analyze(lvef=35.48, lvedd=55.0, lvesd=40.0, lad=35.0, ea=2.02)
        assert result["hf_type"] == "HFrEF"


# ────────────────── v3 mv_ea 字段兼容 ──────────────────

class TestAnalyzeMvEa:
    """v3: ea → mv_ea 字段名统一, analyze() 同时返回两者 (向后兼容)。"""

    def test_mv_ea_param_populates_both_keys(self):
        # v3: 传 mv_ea 参数 → ea 和 mv_ea 都有值
        result = analyze(lvef=50.0, lvedd=50.0, lvesd=30.0, lad=35.0, mv_ea=1.2)
        assert result["mv_ea"] == 1.2
        assert result["ea"] == 1.2

    def test_ea_param_still_works(self):
        # 旧接口: 传 ea 参数 → ea 和 mv_ea 都有值
        result = analyze(lvef=50.0, lvedd=50.0, lvesd=30.0, lad=35.0, ea=1.2)
        assert result["ea"] == 1.2
        assert result["mv_ea"] == 1.2

    def test_mv_ea_takes_priority_over_ea(self):
        # 同时传 ea 和 mv_ea → mv_ea 优先
        result = analyze(lvef=50.0, lvedd=50.0, lvesd=30.0, lad=35.0, ea=1.0, mv_ea=2.0)
        assert result["mv_ea"] == 2.0
        assert result["ea"] == 2.0

    def test_mv_ea_none_falls_back_to_ea(self):
        # mv_ea=None, ea=1.5 → 使用 ea
        result = analyze(lvef=50.0, lvedd=50.0, lvesd=30.0, lad=35.0, ea=1.5, mv_ea=None)
        assert result["mv_ea"] == 1.5
        assert result["ea"] == 1.5

    def test_both_none_returns_none(self):
        # 都不传 → ea=None, mv_ea=None
        result = analyze(lvef=50.0, lvedd=50.0, lvesd=30.0, lad=35.0)
        assert result["ea"] is None
        assert result["mv_ea"] is None


# ────────────────── 阶段 2: lvef=None 容错 ──────────────────

class TestAnalyzeLvefNone:
    """阶段 2: 所有 2D 图失败时 lvef=None, analyze() 不抛异常, hf_type="未知"。"""

    def test_lvef_none_returns_unknown_hf_type(self):
        result = analyze(lvef=None, lvedd=None, lvesd=None, lad=None, mv_ea=None)
        assert result["hf_type"] == "未知"

    def test_lvef_none_preserves_other_metrics(self):
        # lvef=None 但其他指标有值 → hf_type="未知", 其他指标原样返回
        result = analyze(lvef=None, lvedd=55.0, lvesd=40.0, lad=35.0, mv_ea=2.02)
        assert result["hf_type"] == "未知"
        assert result["lvedd"] == 55.0
        assert result["mv_ea"] == 2.02
