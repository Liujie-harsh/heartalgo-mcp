"""规则驱动的结构化结果解读。

只做参考范围比对、LVEF 分型和组合指标计算，不生成诊断结论；
输出必须经过临床人员复核（requires_clinician_review 语义保持不变）。
输入是 mcp_server._result_contract 产出的 completed 结构。
"""

from __future__ import annotations

from metric_catalog import METRIC_META

LVEF_HFREF_BELOW = 40.0        # <40% 为 HFrEF
LVEF_HFPEF_FROM = 50.0         # ≥50% 为 HFpEF，40–49% 为 HFmrEF
EE_PRIME_ABNORMAL_FROM = 14.0  # 平均 e' 计算 E/e' ≥14 提示充盈压升高

TEICHHOLZ_NOTE = "LVEF 由 LVEDD/LVESD 经 Teichholz 公式估算，与金标准可能存在偏差。"
MODEL_TYPING_NOTE = "hf_type 为模型分型结果，不构成医生诊断。"
ECG_INDEPENDENT_NOTE = "ECG 为多标签预测，各概率相互独立，总和不要求等于 1。"
REVIEW_NOTE = "本解读是规则比对输出，仅供辅助，必须由临床人员复核后使用。"

_BASE_NOTES = (TEICHHOLZ_NOTE, MODEL_TYPING_NOTE, ECG_INDEPENDENT_NOTE, REVIEW_NOTE)


def _to_number(text: object) -> float | None:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def parse_reference(reference: str | None) -> tuple[float | None, float | None]:
    """解析 METRIC_META 参考范围：'20–37' / '≤280' / '≥17' / '—'。"""
    if reference is None:
        return (None, None)
    text = str(reference).strip()
    if not text or text in {"—", "-", "–"}:
        return (None, None)
    normalized = text.replace("–", "-").replace("—", "-")
    if normalized.startswith("≤"):
        return (None, _to_number(normalized[1:]))
    if normalized.startswith("≥"):
        return (_to_number(normalized[1:]), None)
    low_text, separator, high_text = normalized.partition("-")
    if separator:
        return (_to_number(low_text), _to_number(high_text))
    value = _to_number(normalized)
    return (value, value) if value is not None else (None, None)


def extract_value(measurement: object) -> float | None:
    """测量条目兼容 {'value': x}、裸数字和数字字符串。"""
    if isinstance(measurement, dict):
        measurement = measurement.get("value")
    if isinstance(measurement, bool):
        return None
    if isinstance(measurement, (int, float)):
        return float(measurement)
    if isinstance(measurement, str):
        return _to_number(measurement)
    return None


def evaluate_metric(metric: str, value: float | None) -> dict | None:
    """按参考范围给单个指标打标；目录外的指标返回 None。"""
    meta = METRIC_META.get(metric)
    if meta is None or value is None:
        return None
    low, high = parse_reference(meta.get("reference"))
    if low is None and high is None:
        return None
    if low is not None and value < low:
        status = "low"
    elif high is not None and value > high:
        status = "high"
    else:
        status = "normal"
    return {
        "metric": metric,
        "name_cn": meta.get("name_cn", metric),
        "value": value,
        "unit": meta.get("unit", ""),
        "reference": meta.get("reference", ""),
        "status": status,
    }


def classify_lvef(lvef: float | None) -> str | None:
    """按协议切点分型：<40 HFrEF，40–49 HFmrEF，≥50 HFpEF。"""
    if lvef is None:
        return None
    if lvef < LVEF_HFREF_BELOW:
        return "HFrEF"
    if lvef < LVEF_HFPEF_FROM:
        return "HFmrEF"
    return "HFpEF"


def collect_echo_metrics(result: dict) -> dict[str, float]:
    """跨心超资产汇总指标，同名指标取首个成功测量。"""
    collected: dict[str, float] = {}
    for item in result.get("cardiac_ultrasound", []):
        for metric, entry in (item.get("measurements") or {}).items():
            if metric in collected:
                continue
            value = extract_value(entry)
            if value is not None:
                collected[metric] = value
    return collected


def _status_between(value: float, low: float | None, high: float | None) -> str:
    if low is not None and value < low:
        return "low"
    if high is not None and value > high:
        return "high"
    return "normal"


def _combined_indicators(metrics: dict[str, float]) -> list[dict]:
    indicators: list[dict] = []

    ea = metrics.get("mv_ea")
    basis = "measured"
    mv_e, mv_a = metrics.get("mv_e"), metrics.get("mv_a")
    if ea is None and mv_e is not None and mv_a:
        ea = mv_e / mv_a
        basis = "derived(mv_e/mv_a)"
    if ea is not None:
        low, high = parse_reference(METRIC_META["mv_ea"]["reference"])
        indicators.append({
            "name": "E/A",
            "value": round(ea, 4),
            "reference": METRIC_META["mv_ea"]["reference"],
            "status": _status_between(ea, low, high),
            "basis": basis,
        })

    e_prime_keys = [key for key in ("tdi_medial", "tdi_lateral") if metrics.get(key) is not None]
    if mv_e is not None and e_prime_keys:
        average_e = sum(metrics[key] for key in e_prime_keys) / len(e_prime_keys)
        ee = mv_e / average_e
        indicators.append({
            "name": "E/e'",
            "value": round(ee, 4),
            "reference": f"≥{EE_PRIME_ABNORMAL_FROM:.0f} 提示充盈压升高",
            "status": "high" if ee >= EE_PRIME_ABNORMAL_FROM else "normal",
            "basis": f"mv_e/avg({', '.join(e_prime_keys)})",
        })
    return indicators


def _ecg_highlights(result: dict) -> list[dict]:
    highlights: list[dict] = []
    for item in result.get("ecg", []):
        predictions = [
            {"label": prediction.get("label"), "probability": prediction.get("probability")}
            for prediction in (item.get("predictions") or [])
            if isinstance(prediction, dict)
            and isinstance(prediction.get("probability"), (int, float))
        ]
        predictions.sort(key=lambda entry: entry["probability"], reverse=True)
        highlights.append({
            "ecg_id": item.get("ecg_id"),
            "top_predictions": [
                entry for entry in predictions if entry["probability"] >= 0.5
            ][:5],
            "error": item.get("error"),
        })
    return highlights


def interpret_diagnosis_result(result: dict) -> dict:
    """对 completed 结构做规则解读，输出异常标注、分型与组合指标。"""
    abnormal: list[dict] = []
    unavailable: list[dict] = []
    for item in result.get("cardiac_ultrasound", []):
        if item.get("error") or item.get("skip_reason"):
            unavailable.append({
                "dcm_id": item.get("dcm_id"),
                "error": item.get("error"),
                "skip_reason": item.get("skip_reason"),
            })
        for metric, entry in (item.get("measurements") or {}).items():
            verdict = evaluate_metric(metric, extract_value(entry))
            if verdict is not None and verdict["status"] != "normal":
                abnormal.append({"scope": item.get("dcm_id"), **verdict})
    metrics = collect_echo_metrics(result)
    lvef = metrics.get("lvef")
    return {
        "lvef_value": lvef,
        "lvef_classification": classify_lvef(lvef),
        "abnormal_findings": abnormal,
        "unavailable_assets": unavailable,
        "combined_indicators": _combined_indicators(metrics),
        "ecg_highlights": _ecg_highlights(result),
        "notes": list(_BASE_NOTES),
    }


def compare_diagnosis_results(result_a: dict, result_b: dict) -> dict:
    """按指标对比两次 completed 结果，输出绝对/相对变化与分型迁移。"""
    metrics_a = collect_echo_metrics(result_a)
    metrics_b = collect_echo_metrics(result_b)
    shared = set(metrics_a) & set(metrics_b)
    ordered = [metric for metric in METRIC_META if metric in shared]
    ordered += sorted(shared - set(ordered))
    rows: list[dict] = []
    for metric in ordered:
        value_a, value_b = metrics_a[metric], metrics_b[metric]
        delta = value_b - value_a
        pct = (delta / abs(value_a) * 100.0) if value_a else None
        if metric == "lvef":
            notable = abs(delta) >= 5.0
        elif pct is not None:
            notable = abs(pct) >= 10.0 and abs(delta) > 1e-9
        else:
            notable = abs(delta) > 1e-9
        meta = METRIC_META.get(metric, {})
        rows.append({
            "metric": metric,
            "name_cn": meta.get("name_cn", metric),
            "unit": meta.get("unit", ""),
            "value_a": value_a,
            "value_b": value_b,
            "delta": round(delta, 4),
            "pct_change": round(pct, 2) if pct is not None else None,
            "direction": (
                "increased" if delta > 1e-9
                else "decreased" if delta < -1e-9
                else "unchanged"
            ),
            "notable": notable,
        })
    classification_a = classify_lvef(metrics_a.get("lvef"))
    classification_b = classify_lvef(metrics_b.get("lvef"))
    return {
        "metrics": rows,
        "lvef_classification": (
            {"from": classification_a, "to": classification_b}
            if classification_a is not None or classification_b is not None
            else None
        ),
        "notes": [
            "对比仅覆盖两次任务中都成功测量的同名指标；"
            "差异可能来自图像质量与操作者变异，不构成病情结论。",
            TEICHHOLZ_NOTE,
            REVIEW_NOTE,
        ],
    }
