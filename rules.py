"""心衰诊断规则引擎，兼容旧 ea 与协议 v3 的 mv_ea 字段。"""

from typing import Optional


REFERENCE_RANGES = {
    "LVEF": (50, None),
    "LVEDD": (42, 58),
    "LVESD": (25, 37),
    "LAD": (27, 38),
    "E/A": (0.8, 2.0),
    "MV_EA": (0.8, 2.0),
}


def classify_hf(lvef: Optional[float]) -> str:
    if lvef is None:
        return "未知"
    if lvef < 40:
        return "HFrEF"
    if lvef < 50:
        return "HFmrEF"
    return "HFpEF"


def flag_abnormal(metric: str, value: Optional[float]) -> bool:
    if value is None:
        return False
    low, high = REFERENCE_RANGES[metric]
    return (low is not None and value < low) or (high is not None and value > high)


def analyze(
    lvef: Optional[float],
    lvedd: Optional[float],
    lvesd: Optional[float],
    lad: Optional[float],
    ea: Optional[float] = None,
    mv_ea: Optional[float] = None,
) -> dict:
    value = mv_ea if mv_ea is not None else ea
    return {
        "lvef": lvef,
        "lvedd": lvedd,
        "lvesd": lvesd,
        "lad": lad,
        "ea": value,
        "mv_ea": value,
        "hf_type": classify_hf(lvef),
    }
