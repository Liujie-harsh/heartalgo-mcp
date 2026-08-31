"""独立于具体 Agent Harness 的心衰算法 MCP 适配层。

工具面覆盖完整闭环：
  输入   analyze_case_files          本地文件一站式建病例 + 登记 + 提交
  分析   diagnose_heart_failure      已登记病例提交分析
         get_diagnosis_result        查询任务结果
  解读   interpret_diagnosis         规则解读（异常标注/分型/组合指标）
         generate_report             报告草稿（markdown/json，可存回病例）
         compare_diagnoses           同病例两次任务纵向对比
  检索   list_cases                  服务账号可见病例摘要
         get_case_detail             病例资产/任务/复核/工件详情
         list_tasks                  任务列表（可按病例过滤）
  复核   get_review_status           查询临床复核状态
         submit_review               记录临床复核结论
  能力   list_supported_views        切面与指标元数据
"""

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
from interpretation import compare_diagnosis_results, interpret_diagnosis_result
from metric_catalog import METRIC_META, VIEW_METRICS
from report_render import render_markdown_report


logger = logging.getLogger(__name__)

TASK_STATE_NAMES = {0: "queued", 1: "processing", 2: "completed", 3: "failed"}
SUPPORTED_MODALITIES = frozenset({"CARDIAC_ULTRASOUND", "ECG"})
REPORT_FORMATS = frozenset({"markdown", "json"})
REVIEW_DECISIONS = frozenset({"approved", "rejected"})


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
            "心衰辅助分析工具集。analyze_case_files 支持从本地文件一站式建病例、"
            "登记资产并提交分析；diagnose_heart_failure / get_diagnosis_result "
            "面向已登记病例；list_cases / get_case_detail / list_tasks 检索病例与任务；"
            "interpret_diagnosis 做规则解读，generate_report 生成报告草稿，"
            "compare_diagnoses 做纵向对比；get_review_status / submit_review "
            "对接临床复核。所有结果是辅助分析，必须经过临床人员复核，"
            "不得替代医生诊断。"
        ),
    )

    def _resolve_task(task_id: str) -> tuple[str, dict]:
        case_id = case_store.find_case_for_task(task_id, service_user)
        metadata = case_store.get_case_for_service(case_id, service_user)
        return case_id, metadata

    # ────────────────── 输入：一站式分析 ──────────────────

    @mcp.tool()
    def analyze_case_files(
        files: list[dict],
        request_id: str | None = None,
        submit: bool = True,
    ) -> dict[str, object]:
        """一站式分析：从本地文件创建病例、登记资产并提交诊断任务。

        files 每项需要 {"path": 本地文件路径, "modality": "CARDIAC_ULTRASOUND"|"ECG"}；
        心超必须另给 "dcm_type"（合法值见 list_supported_views），可选 "asset_id"。
        病例归属 MCP 服务账号；request_id 传稳定值可让建病例步骤幂等，
        缺省时每次调用都会创建新病例。
        """
        specs: list[dict] = []
        seen_asset_ids: set[str] = set()
        for index, item in enumerate(files):
            if not isinstance(item, dict):
                raise ValueError(f"files[{index}] 必须是对象")
            path = str(item.get("path") or "").strip()
            modality = str(item.get("modality") or "").strip().upper()
            dcm_type = item.get("dcm_type") or None
            asset_id = str(item.get("asset_id") or f"asset-{uuid4().hex}")
            if not path:
                raise ValueError(f"files[{index}].path 不能为空")
            if modality not in SUPPORTED_MODALITIES:
                raise ValueError(
                    f"files[{index}].modality 必须是 CARDIAC_ULTRASOUND 或 ECG"
                )
            if modality == "CARDIAC_ULTRASOUND":
                if dcm_type not in VIEW_METRICS:
                    raise ValueError(
                        f"files[{index}].dcm_type 必须是受支持切面: "
                        f"{', '.join(VIEW_METRICS)}"
                    )
            elif dcm_type:
                raise ValueError(f"files[{index}].dcm_type 仅心超资产可设置")
            if asset_id in seen_asset_ids:
                raise ValueError(f"asset_id 重复: {asset_id}")
            seen_asset_ids.add(asset_id)
            if not os.path.isfile(path):
                raise ValueError(f"文件不存在: {path}")
            specs.append({
                "path": path,
                "modality": modality,
                "dcm_type": dcm_type,
                "asset_id": asset_id,
            })
        if not specs:
            raise ValueError("files 不能为空")
        create_request_id = (request_id or f"mcp-batch-{uuid4().hex}").strip()
        try:
            metadata, case_created = case_store.create_case(
                service_user, create_request_id, [service_user]
            )
            case_id = metadata["caseId"]
            uploaded: list[dict[str, object]] = []
            for spec in specs:
                with open(spec["path"], "rb") as handle:
                    asset, asset_created = case_store.add_asset(
                        case_id,
                        service_user,
                        spec["asset_id"],
                        spec["modality"],
                        spec["dcm_type"],
                        handle,
                    )
                uploaded.append({
                    "asset_id": asset["assetId"],
                    "modality": asset["modality"],
                    "dcm_type": asset.get("dcmType"),
                    "sha256": asset["sha256"],
                    "size_bytes": asset["sizeBytes"],
                    "created": asset_created,
                })
        except Exception as exc:
            raise _safe_mcp_error(exc) from exc
        response: dict[str, object] = {
            "case_id": case_id,
            "case_created": case_created,
            "assets": uploaded,
        }
        if submit:
            try:
                submission = submit_case_diagnosis(
                    app,
                    case_store,
                    case_id,
                    f"mcp-diagnose-{uuid4().hex}",
                    service_user,
                    None,
                    task_id_prefix="mcp",
                )
            except Exception as exc:
                raise _safe_mcp_error(exc) from exc
            response.update(_submit_contract(submission))
        return response

    # ────────────────── 分析：提交与查询 ──────────────────

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

    # ────────────────── 解读：规则分析 / 报告 / 对比 ──────────────────

    @mcp.tool()
    def interpret_diagnosis(task_id: str) -> dict[str, object]:
        """对已完成任务做规则解读：异常指标标注、LVEF 分型、组合指标。"""
        try:
            case_id, metadata = _resolve_task(task_id)
            result = get_case_diagnosis_result(
                app, case_store, case_id, task_id, metadata["sysUserId"]
            )
        except Exception as exc:
            raise _safe_mcp_error(exc) from exc
        contract = _result_contract(result)
        if contract["status"] != "completed":
            payload: dict[str, object] = {
                "task_id": task_id,
                "case_id": case_id,
                "status": contract["status"],
            }
            if contract["status"] == "failed":
                payload["error"] = contract["error"]
            return payload
        return {
            "task_id": task_id,
            "case_id": case_id,
            "status": "completed",
            "hf_type": contract.get("hf_type"),
            "algorithm_version": contract.get("algorithm_version"),
            "requires_clinician_review": contract.get(
                "requires_clinician_review", True
            ),
            "review_status": contract.get("review_status", "pending"),
            **interpret_diagnosis_result(contract),
        }

    @mcp.tool()
    def generate_report(
        task_id: str,
        format: str = "markdown",
        save_to_case: bool = False,
    ) -> dict[str, object]:
        """把已完成任务渲染成报告草稿（markdown/json），可保存回病例工件。

        save_to_case=True 时写入病例 artifacts（artifactId 为
        report-{task_id}.md/.json），可通过 get_case_detail 查到。
        """
        fmt = str(format).strip().lower()
        if fmt not in REPORT_FORMATS:
            raise ValueError("format 必须是 markdown 或 json")
        try:
            case_id, metadata = _resolve_task(task_id)
            result = get_case_diagnosis_result(
                app, case_store, case_id, task_id, metadata["sysUserId"]
            )
        except Exception as exc:
            raise _safe_mcp_error(exc) from exc
        contract = _result_contract(result)
        if contract["status"] != "completed":
            return {
                "task_id": task_id,
                "case_id": case_id,
                "status": contract["status"],
                "content": None,
            }
        analysis = interpret_diagnosis_result(contract)
        if fmt == "markdown":
            content = render_markdown_report(contract, analysis)
        else:
            content = json.dumps(
                {
                    "report": {
                        key: contract.get(key)
                        for key in (
                            "case_id",
                            "task_id",
                            "status",
                            "hf_type",
                            "algorithm_version",
                            "requires_clinician_review",
                            "review_status",
                            "review",
                        )
                    },
                    "interpretation": analysis,
                    "inputs": contract.get("inputs", {}),
                },
                ensure_ascii=False,
                indent=2,
            )
        response: dict[str, object] = {
            "task_id": task_id,
            "case_id": case_id,
            "format": fmt,
            "content": content,
        }
        if save_to_case:
            artifact_id = f"report-{task_id}.{'md' if fmt == 'markdown' else 'json'}"
            try:
                artifact = case_store.save_case_artifact(
                    case_id, metadata["sysUserId"], artifact_id, content
                )
            except Exception as exc:
                raise _safe_mcp_error(exc) from exc
            response["artifact"] = {
                key: value for key, value in artifact.items() if key != "path"
            }
        return response

    @mcp.tool()
    def compare_diagnoses(
        case_id: str,
        task_id_a: str,
        task_id_b: str,
    ) -> dict[str, object]:
        """对比同病例两次已完成任务的测量变化（纵向随访）。"""
        try:
            metadata = case_store.get_case_for_service(case_id, service_user)
            owner_id = metadata["sysUserId"]
            for task_id in (task_id_a, task_id_b):
                if not case_store.diagnosis_belongs_to_case(
                    case_id, owner_id, task_id
                ):
                    raise CaseNotFoundError("诊断任务不存在")
            result_a = get_case_diagnosis_result(
                app, case_store, case_id, task_id_a, owner_id
            )
            result_b = get_case_diagnosis_result(
                app, case_store, case_id, task_id_b, owner_id
            )
        except Exception as exc:
            raise _safe_mcp_error(exc) from exc
        contract_a = _result_contract(result_a)
        contract_b = _result_contract(result_b)
        if contract_a["status"] != "completed" or contract_b["status"] != "completed":
            return {
                "case_id": case_id,
                "task_id_a": task_id_a,
                "task_id_b": task_id_b,
                "status_a": contract_a["status"],
                "status_b": contract_b["status"],
                "comparison": None,
            }
        return {
            "case_id": case_id,
            "task_id_a": task_id_a,
            "task_id_b": task_id_b,
            "comparison": compare_diagnosis_results(contract_a, contract_b),
        }

    # ────────────────── 检索：病例与任务 ──────────────────

    @mcp.tool()
    def list_cases() -> dict[str, object]:
        """列出 MCP 服务账号可见的病例摘要（资产/任务计数与复核决定）。"""
        cases = case_store.list_cases_for_service(service_user)
        return {"cases": cases, "count": len(cases)}

    @mcp.tool()
    def get_case_detail(case_id: str) -> dict[str, object]:
        """返回病例资产、诊断任务（含实时状态）、复核历史与报告工件。"""
        try:
            metadata = case_store.get_case_for_service(case_id, service_user)
        except Exception as exc:
            raise _safe_mcp_error(exc) from exc
        detail = FileCaseStore.public_case(metadata)
        store = app.state.store
        for diagnosis in detail["diagnoses"]:
            task = store.get_for_user(diagnosis["taskId"], metadata["sysUserId"])
            diagnosis["status"] = (
                TASK_STATE_NAMES.get(task["taskState"], "unknown")
                if task
                else "unknown"
            )
        return detail

    @mcp.tool()
    def list_tasks(case_id: str | None = None) -> dict[str, object]:
        """列出可见病例的诊断任务（可按 case_id 过滤），含实时状态。"""
        try:
            if case_id:
                metadatas = [case_store.get_case_for_service(case_id, service_user)]
            else:
                metadatas = [
                    case_store.get_case_for_service(item["caseId"], service_user)
                    for item in case_store.list_cases_for_service(service_user)
                ]
        except Exception as exc:
            raise _safe_mcp_error(exc) from exc
        store = app.state.store
        tasks: list[dict[str, object]] = []
        for metadata in metadatas:
            for diagnosis in metadata.get("diagnoses", []):
                task = store.get_for_user(
                    diagnosis["taskId"], metadata["sysUserId"]
                )
                tasks.append({
                    "case_id": metadata["caseId"],
                    "task_id": diagnosis["taskId"],
                    "created": diagnosis.get("createdAt"),
                    "submission_state": diagnosis.get("submissionState"),
                    "status": (
                        TASK_STATE_NAMES.get(task["taskState"], "unknown")
                        if task
                        else "unknown"
                    ),
                })
        return {"tasks": tasks, "count": len(tasks)}

    # ────────────────── 复核闭环 ──────────────────

    @mcp.tool()
    def get_review_status(task_id: str) -> dict[str, object]:
        """查询任务的临床复核状态与该任务的全部复核记录。"""
        try:
            case_id, metadata = _resolve_task(task_id)
        except Exception as exc:
            raise _safe_mcp_error(exc) from exc
        history = [
            review
            for review in metadata.get("reviewHistory", [])
            if review.get("taskId") == task_id
        ]
        latest = history[-1] if history else None
        return {
            "task_id": task_id,
            "case_id": case_id,
            "review_status": latest["decision"] if latest else "pending",
            "requires_clinician_review": (
                latest is None or latest["decision"] != "approved"
            ),
            "review": latest,
            "review_count": len(history),
        }

    @mcp.tool()
    def submit_review(
        task_id: str,
        decision: str,
        reviewer_id: str,
        comment: str = "",
    ) -> dict[str, object]:
        """为已完成任务记录临床复核结论（approved/rejected）。

        reviewer_id 必须是实际复核的临床人员，且不能与病例所有者相同；
        本工具不授予任何人跳过临床复核的权限。
        """
        if decision not in REVIEW_DECISIONS:
            raise ValueError("decision 必须是 approved 或 rejected")
        try:
            case_id, metadata = _resolve_task(task_id)
            owner_id = metadata["sysUserId"]
            task = app.state.store.get_for_user(task_id, owner_id)
            if task is None:
                raise CaseNotFoundError("诊断任务不存在")
            if task["taskState"] != 2:
                raise ValueError("只有已完成任务可以复核")
            if reviewer_id == owner_id:
                raise ValueError("病例所有者不能自我复核")
            review = case_store.record_review(
                case_id, owner_id, task_id, reviewer_id, decision, comment
            )
        except Exception as exc:
            raise _safe_mcp_error(exc) from exc
        return {"case_id": case_id, **review}

    # ────────────────── 能力发现 ──────────────────

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
            "优先调用 interpret_diagnosis 获取规则解读，再基于其输出组织回答。"
            "LVEF<40% 为 HFrEF，40–49% 为 HFmrEF，≥50% 为 HFpEF；"
            "LVEF 由 LVEDD/LVESD 经 Teichholz 公式估算，与金标准可能有偏差。"
            "ECG 是多标签预测，各概率相互独立、总和不要求等于 1。"
            "逐张检查心超的 error 和 skip_reason：任务 completed 不代表每张输入成功。"
            "先列异常指标及单位，再说明模型分型；算法结果仅供辅助，必须由临床人员复核。"
            "不得补造缺失测量、患者信息或治疗建议。"
        )

    return mcp
