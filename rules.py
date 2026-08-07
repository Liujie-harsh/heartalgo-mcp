"""
心衰诊断规则引擎。

架构决策 (handoff.md L118-L122):
  - 分型 (HFpEF/HFrEF/HFmrEF) 完全由 LVEF 阈值决定, 走确定性规则引擎, 不让 LLM 推理
  - 异常标红走规则
  - LLM 只做自然语言润色, 不参与判定

参考范围:
  LVEF  ≥50%        <50 标红
  LVEDD 42-58 mm    超范围标红
  LVESD 25-37 mm    超范围标红
  LAD   27-38 mm    超范围标红
  E/A   0.8-1.5     超范围标红
"""
from typing import Optional

# 指标参考范围 (闭区间, None 表示无下/上界)
# abnormal = value < low  或  value > high
REFERENCE_RANGES = {
    "LVEF":  (50, None),     # ≥50 正常, <50 标红 (低界异常)
    "LVEDD": (42, 58),       # 42-58 正常, 超范围标红
    "LVESD": (25, 37),       # 25-37 正常, 超范围标红
    "LAD":   (27, 38),       # 27-38 正常, 超范围标红
    "MV_EA": (0.8, 1.5),     # 0.8-1.5 正常, 超范围标红
}


def classify_hf(lvef: Optional[float]) -> str:
    """
    HF 分型, 完全由 LVEF 阈值决定。

    HFrEF  : LVEF < 40%
    HFmrEF : 40% ≤ LVEF ≤ 49%
    HFpEF  : LVEF ≥ 50%
    None   : 无法分型 (所有 2D 图推理失败)
    """
    if lvef is None:
        return "未知"
    if lvef < 40:
        return "HFrEF"
    if lvef < 50:
        return "HFmrEF"
    return "HFpEF"


def flag_abnormal(metric: str, value: Optional[float]) -> bool:
    """
    判定指标是否异常 (需标红)。

    - null: 不标红 (数据缺失不报警)
    - 数值: 超出参考范围即异常
    """
    if value is None:
        return False
    low, high = REFERENCE_RANGES[metric]
    if low is not None and value < low:
        return True
    if high is not None and value > high:
        return True
    return False


def analyze(lvef: Optional[float], lvedd: float, lvesd: float, lad: float,
            mv_ea: Optional[float]) -> dict:
    """
    规则引擎汇总: 5 项指标 + HF 分型。

    返回接口契约: {lvef, lvedd, lvesd, lad, mv_ea, hf_type}
    """
    return {
        "lvef": lvef,
        "lvedd": lvedd,
        "lvesd": lvesd,
        "lad": lad,
        "mv_ea": mv_ea,
        "hf_type": classify_hf(lvef),
    }
