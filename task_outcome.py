"""将 Runner 原始结果转换为可持久化、可返回的任务输出。"""

from __future__ import annotations

from typing import Any

from metric_catalog import METRIC_META
from task_models import ImgItem


def _echo_rois(result: dict, img_id: str) -> list[dict[str, Any]]:
    segments = result.get("echo_per_image", {}).get(img_id, {}).get("rois", [])
    return [
        {
            "roiType": segment["type"],
            "points": [
                {"xPos": int(point[0]), "yPos": int(point[1])}
                for point in segment["points"]
            ],
        }
        for segment in segments
    ]


def build_success_outcome(task_id: str, images: list[ImgItem], result: dict) -> dict:
    """构建可直接写入 algorithm_report 的成功任务输出。"""
    reports: list[dict] = []
    cardiac_ultrasound: list[dict] = []
    ecg: list[dict] = []
    echo_summary_keys = ("lvef", "lvedd", "lvesd", "lad", "mv_ea", "hf_type")
    has_echo_summary = any(key in result for key in echo_summary_keys)

    for image in images:
        if image.imgType == "ECG":
            report_id = f"{task_id}:{image.imgId}:ecg"
            payload = {
                "ecgId": image.imgId,
                "patientInfo": result.get("ecg_patient_info", {}).get(image.imgId, {}),
                "measurements": result.get("ecg_measurements", {}).get(image.imgId, {}),
                "predictions": result.get("ecg_predictions", {}).get(image.imgId, []),
            }
            reports.append({
                "reportId": report_id,
                "reportType": "ECG",
                "reportResult": payload,
                "inputId": image.imgId,
                "roiData": None,
            })
            ecg.append({
                "ecgId": image.imgId,
                "ecgPath": image.imgPath,
                "reportId": report_id,
            })
            continue

        if image.imgType != "CARDIAC_ULTRASOUND":
            continue

        report_id = f"{task_id}:{image.imgId}:measurement"
        per_image = result.get("echo_per_image", {}).get(image.imgId, {})
        if not per_image:
            per_image = {key: result[key] for key in echo_summary_keys if key in result}

        measurements: dict = {}
        for key, value in per_image.items():
            if key in {"rois", "error", "skipReason"}:
                continue
            if key == "hf_type":
                measurements[key] = value
                continue
            meta = METRIC_META.get(key)
            measurements[key] = {"value": value, **meta} if meta else {"value": value}

        payload = {"dcmId": image.imgId, "measurements": measurements}
        if per_image.get("skipReason"):
            payload["skipReason"] = per_image["skipReason"]
        if per_image.get("error"):
            payload["error"] = per_image["error"]

        roi_data = _echo_rois(result, image.imgId)
        reports.append({
            "reportId": report_id,
            "reportType": "CU-SUB",
            "reportResult": payload,
            "inputId": image.imgId,
            "roiData": roi_data,
        })
        cardiac_ultrasound.append({
            "dcmId": image.imgId,
            "dcmPath": image.imgPath,
            "reportId": report_id,
            "rois": roi_data,
        })

    if has_echo_summary:
        summary_measurements: dict = {}
        for key in echo_summary_keys:
            if key not in result:
                continue
            value = result[key]
            if key == "hf_type":
                summary_measurements[key] = value
                continue
            meta = METRIC_META.get(key)
            summary_measurements[key] = {"value": value, **meta} if meta else {"value": value}

        reports.append({
            "reportId": f"{task_id}:cu-summary",
            "reportType": "CU-SUMMARY",
            "reportResult": {"taskId": task_id, "measurements": summary_measurements},
            "inputId": None,
            "roiData": None,
        })

    return {
        "reports": reports,
        "cardiacUltrasound": cardiac_ultrasound,
        "ecg": ecg,
    }
