"""把 completed 的结构化诊断结果渲染成 Markdown 报告草稿。

报告是算法输出的展示层：所有判定都来自 interpretation 的规则比对，
不添加任何诊断结论，必须保留临床复核声明。
"""

from __future__ import annotations

from interpretation import evaluate_metric, extract_value

_STATUS_CN = {"high": "偏高", "low": "偏低", "normal": "正常"}

_FALLBACK_NOTES = (
    "LVEF 由 LVEDD/LVESD 经 Teichholz 公式估算，与金标准可能存在偏差。",
    "hf_type 为模型分型结果，不构成医生诊断。",
    "本报告是算法输出草稿，必须由临床人员复核后使用。",
)


def _cell(value: object) -> str:
    if value is None:
        return "—"
    text = str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def _value_text(entry: object) -> str:
    value = extract_value(entry)
    return f"{value:g}" if value is not None else "—"


def _flag(metric: str, entry: object) -> str:
    verdict = evaluate_metric(metric, extract_value(entry))
    if verdict is None:
        return "—"
    return _STATUS_CN.get(verdict["status"], "—")


def render_markdown_report(result: dict, interpretation: dict | None = None) -> str:
    """渲染报告草稿；interpretation 传入规则解读结果以填充异常与组合指标。"""
    interpretation = interpretation or {}
    lines: list[str] = ["# 心衰辅助分析报告（草稿）", ""]
    lines.append(f"- 病例 ID：{_cell(result.get('case_id'))}")
    lines.append(f"- 任务 ID：{_cell(result.get('task_id'))}")
    lines.append(f"- 算法版本：{_cell(result.get('algorithm_version'))}")
    lines.append(f"- 模型分型 hf_type：{_cell(result.get('hf_type'))}")
    if interpretation.get("lvef_value") is not None:
        lines.append(
            f"- LVEF（Teichholz 估算）：{interpretation['lvef_value']:g}%，"
            f"规则分型：{_cell(interpretation.get('lvef_classification')) or '无法判定'}"
        )
    if result.get("requires_clinician_review", True):
        lines.append(
            f"- 复核状态：{_cell(result.get('review_status'))}"
            "（**尚未经临床复核，不能作为最终临床结论**）"
        )
    else:
        lines.append(f"- 复核状态：{_cell(result.get('review_status'))}（已经临床复核）")

    abnormal = interpretation.get("abnormal_findings", [])
    lines += ["", "## 异常提示", "",
              "| 指标 | 数值 | 单位 | 参考范围 | 提示 | 来源 |",
              "|---|---|---|---|---|---|"]
    if abnormal:
        for finding in abnormal:
            lines.append(
                f"| {_cell(finding.get('name_cn'))} | {_cell(finding.get('value'))} "
                f"| {_cell(finding.get('unit'))} | {_cell(finding.get('reference'))} "
                f"| {_STATUS_CN.get(finding.get('status'), '—')} "
                f"| {_cell(finding.get('scope'))} |"
            )
    else:
        lines.append("| — | — | — | — | 未发现超出参考范围的指标 | — |")

    combined = interpretation.get("combined_indicators", [])
    if combined:
        lines += ["", "## 组合指标", "",
                  "| 指标 | 数值 | 参考范围 | 提示 | 计算依据 |",
                  "|---|---|---|---|---|"]
        for item in combined:
            lines.append(
                f"| {_cell(item.get('name'))} | {_cell(item.get('value'))} "
                f"| {_cell(item.get('reference'))} "
                f"| {_STATUS_CN.get(item.get('status'), '—')} "
                f"| {_cell(item.get('basis'))} |"
            )

    echo_items = result.get("cardiac_ultrasound") or []
    lines += ["", "## 心超测量"]
    if not echo_items:
        lines += ["", "本任务没有心超资产。"]
    for item in echo_items:
        lines += ["", f"### {_cell(item.get('dcm_id'))}"]
        if item.get("error"):
            lines.append(f"- 分析失败：{_cell(item['error'])}")
        if item.get("skip_reason"):
            lines.append(f"- 跳过原因：{_cell(item['skip_reason'])}")
        measurements = item.get("measurements") or {}
        if measurements:
            lines += ["", "| 指标 | 数值 | 单位 | 参考范围 | 提示 |",
                      "|---|---|---|---|---|"]
            for metric, entry in measurements.items():
                meta = entry if isinstance(entry, dict) else {}
                lines.append(
                    f"| {_cell(metric)} | {_cell(_value_text(entry))} "
                    f"| {_cell(meta.get('unit'))} | {_cell(meta.get('reference'))} "
                    f"| {_flag(metric, entry)} |"
                )

    ecg_items = result.get("ecg") or []
    lines += ["", "## 心电图"]
    if not ecg_items:
        lines += ["", "本任务没有 ECG 资产。"]
    for item in ecg_items:
        lines += ["", f"### {_cell(item.get('ecg_id'))}"]
        if item.get("error"):
            lines.append(f"- 分析失败：{_cell(item['error'])}")
        patient_info = item.get("patient_info") or {}
        if patient_info:
            lines.append(f"- 患者信息：{_cell(patient_info)}")
        measurements = item.get("measurements") or {}
        if measurements:
            rendered = "、".join(
                f"{key}={_cell(value)}" for key, value in measurements.items()
            )
            lines.append(f"- 测量：{rendered}")
        predictions = item.get("predictions") or []
        if predictions:
            lines += ["", "| 标签 | 概率 |", "|---|---|"]
            for prediction in predictions:
                lines.append(
                    f"| {_cell(prediction.get('label'))} "
                    f"| {_cell(prediction.get('probability'))} |"
                )

    inputs = result.get("inputs") or {}
    if inputs:
        lines += ["", "## 输入可追溯性", "",
                  "| 资产 | SHA-256 | 大小(字节) |", "|---|---|---|"]
        for asset_id, info in inputs.items():
            lines.append(
                f"| {_cell(asset_id)} | `{_cell((info or {}).get('sha256'))}` "
                f"| {_cell((info or {}).get('sizeBytes'))} |"
            )

    lines += ["", "## 说明与限制"]
    for note in interpretation.get("notes") or _FALLBACK_NOTES:
        lines.append(f"- {note}")
    review = result.get("review")
    if review:
        lines += [
            "",
            f"- 最近复核：{_cell(review.get('reviewerId'))} 于 "
            f"{_cell(review.get('reviewedAt'))} 作出「{_cell(review.get('decision'))}」",
            f"  - 意见：{_cell(review.get('comment'))}",
        ]
    lines.append("")
    return "\n".join(lines)
