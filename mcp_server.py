"""独立于具体 Agent Harness 的心衰算法 MCP 适配层。"""

from __future__ import annotations

import json
import logging
import os
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from mcp.server import MCPServer

from algorithm_errors import to_public_error
from case_api import get_case_diagnosis_result, submit_case_diagnosis
from case_store import CaseNotFoundError, CaseStoreError, FileCaseStore
from metric_catalog import METRIC_META, VIEW_METRICS


logger = logging.getLogger(__name__)


def _public_mcp_error(exc: HTTPException) -> ValueError:
    detail = exc.detail if isinstance(exc.detail, str) else "请求无法完成"
    return ValueError(detail)


def _safe_mcp_error(exc: Exception) -> ValueError:
    if isinstance(exc, HTTPException):
        return _public_mcp_error(exc)
    if isinstance(exc, CaseStoreError):
        return ValueError(str(exc))
    public = to_public_error(exc)
    logger.exception("MCP 调用失败 error_code=%s", public.code)
    return ValueError(public.message)


def _submit_contract(result: dict) -> dict[str, object]:
    return {
        "case_id": result["caseId"],
        "task_id": result["taskId"],
        "status": result["status"],
        "created": result["created"],
    }


def _result_contract(result: dict) -> dict[str, object]:
    base: dict[str, object] = {
        "case_id": result["caseId"],
        "task_id": result["taskId"],
        "status": result["status"],
    }
    if result["status"] == "processing":
        return base
    if result["status"] == "failed":
        return {**base, "error": result["error"]}
    return {
        **base,
        "hf_type": result.get("hfType"),
        "cardiac_ultrasound": [
            {
                "dcm_id": item["dcmId"],
                "measurements": item["measurements"],
                "rois": item["rois"],
                "error": item.get("error"),
                "skip_reason": item.get("skipReason"),
            }
            for item in result.get("cardiacUltrasound", [])
        ],
        "ecg": [
            {
                "ecg_id": item["ecgId"],
                "patient_info": item["patientInfo"],
                "measurements": item["measurements"],
                "predictions": item["predictions"],
                "error": item.get("error"),
            }
            for item in result.get("ecg", [])
        ],
        "inputs": result.get("inputs", {}),
        "algorithm_version": result.get("algorithmVersion", "unknown"),
        "requires_clinician_review": result.get("requiresClinicianReview", True),
        "review_status": result.get("reviewStatus", "pending"),
        "review": result.get("review"),
    }


def build_mcp(app: FastAPI, service_user_id: str | None = None) -> MCPServer:
    """构建只依赖共享病例/任务服务的 MCP Server。"""
    if not hasattr(app.state, "case_store"):
        raise RuntimeError("构建 MCP 前必须先安装病例服务")
    case_store: FileCaseStore = app.state.case_store
    service_user = service_user_id or os.environ.get(
        "MCP_SERVICE_USER_ID", "mcp-service"
    )
    mcp = MCPServer(
        "heart-failure-algorithm",
        instructions=(
            "只分析已通过病例上传接口登记的心超或 ECG 资产。"
            "结果是辅助分析，必须经过临床人员复核。"
        ),
    )

    @mcp.tool()
    def diagnose_heart_failure(
        case_id: str,
        asset_ids: list[str] | None = None,
    ) -> dict[str, object]:
        """提交已上传病例的心衰分析任务，返回可继续查询的 taskId。"""
        try:
            metadata = case_store.get_case_for_service(case_id, service_user)
            return _submit_contract(
                submit_case_diagnosis(
                    app,
                    case_store,
                    case_id,
                    f"mcp-{uuid4().hex}",
                    metadata["sysUserId"],
                    asset_ids,
                    task_id_prefix="mcp",
                )
            )
        except Exception as exc:
            raise _safe_mcp_error(exc) from exc

    @mcp.tool()
    def get_diagnosis_result(
        task_id: str,
    ) -> dict[str, object]:
        """查询诊断任务，返回 processing、completed 或 failed 结构。"""
        try:
            case_id = case_store.find_case_for_task(task_id, service_user)
            metadata = case_store.get_case_for_service(case_id, service_user)
            return _result_contract(
                get_case_diagnosis_result(
                    app, case_store, case_id, task_id, metadata["sysUserId"]
                )
            )
        except Exception as exc:
            raise _safe_mcp_error(exc) from exc

    @mcp.tool()
    def list_supported_views() -> dict[str, object]:
        """返回支持的心超切面和测量指标元数据。"""
        return {
            "views": [
                {"dcm_type": view, "metrics": list(metrics)}
                for view, metrics in VIEW_METRICS.items()
            ],
            "metrics": METRIC_META,
        }

    @mcp.resource(
        "heart-algo://diagnosis/{task_id}",
        mime_type="application/json",
    )
    def diagnosis_resource(task_id: str) -> str:
        """读取服务账号名下一个已提交诊断的当前结构化结果。"""
        try:
            case_id = case_store.find_case_for_task(task_id, service_user)
            metadata = case_store.get_case_for_service(case_id, service_user)
            if not case_store.diagnosis_belongs_to_case(
                case_id, metadata["sysUserId"], task_id
            ):
                raise CaseNotFoundError("诊断任务不存在")
            task = app.state.store.get_for_user(task_id, metadata["sysUserId"])
            if task is None:
                raise CaseNotFoundError("诊断任务不存在")
            return json.dumps(
                {
                    "case_id": case_id,
                    "task_id": task_id,
                    "task_state": task["taskState"],
                    "reports": (task.get("result") or {}).get("reports", []),
                    "cardiac_ultrasound": [
                        {key: value for key, value in item.items() if key != "dcmPath"}
                        for item in (task.get("result") or {}).get(
                            "cardiacUltrasound", []
                        )
                    ],
                    "ecg": [
                        {key: value for key, value in item.items() if key != "ecgPath"}
                        for item in (task.get("result") or {}).get("ecg", [])
                    ],
                    "failed_reason": task.get("failedReason"),
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            raise _safe_mcp_error(exc) from exc

    @mcp.prompt(name="heart_failure_interpretation")
    def heart_failure_interpretation() -> str:
        """要求模型忠实解释结构化结果，不越权给出自主诊疗决定。"""
        return (
            "请仅依据工具返回的结构化心超和 ECG 结果进行总结。"
            "LVEF<40% 为 HFrEF，40–49% 为 HFmrEF，≥50% 为 HFpEF；"
            "LVEF 由 LVEDD/LVESD 经 Teichholz 公式估算，与金标准可能有偏差。"
            "ECG 是多标签预测，各概率相互独立、总和不要求等于 1。"
            "逐张检查心超的 error 和 skip_reason：任务 completed 不代表每张输入成功。"
            "先列异常指标及单位，再说明模型分型；算法结果仅供辅助，必须由临床人员复核。"
            "不得补造缺失测量、患者信息或治疗建议。"
        )

    return mcp
