"""独立于具体 Agent Harness 的心衰算法 MCP 适配层。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from mcp.server import MCPServer

from case_api import get_case_diagnosis_result, submit_case_diagnosis
from case_store import FileCaseStore, SUPPORTED_DCM_TYPES
from metric_catalog import METRIC_META


def _public_mcp_error(exc: HTTPException) -> ValueError:
    detail = exc.detail if isinstance(exc.detail, str) else "请求无法完成"
    return ValueError(detail)


def build_mcp(app: FastAPI) -> MCPServer:
    """构建只依赖共享病例/任务服务的 MCP Server。"""
    if not hasattr(app.state, "case_store"):
        raise RuntimeError("构建 MCP 前必须先安装病例服务")
    case_store: FileCaseStore = app.state.case_store
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
        sys_user_id: str,
        request_id: str,
        asset_ids: list[str] | None = None,
    ) -> dict[str, object]:
        """提交已上传病例的心衰分析任务，返回可继续查询的 taskId。"""
        try:
            return submit_case_diagnosis(
                app,
                case_store,
                case_id,
                request_id,
                sys_user_id,
                asset_ids,
            )
        except HTTPException as exc:
            raise _public_mcp_error(exc) from exc

    @mcp.tool()
    def get_diagnosis_result(
        case_id: str,
        task_id: str,
        sys_user_id: str,
    ) -> dict[str, object]:
        """查询诊断任务，返回 processing、completed 或 failed 结构。"""
        try:
            return get_case_diagnosis_result(
                app, case_store, case_id, task_id, sys_user_id
            )
        except HTTPException as exc:
            raise _public_mcp_error(exc) from exc

    @mcp.tool()
    def list_supported_views() -> dict[str, object]:
        """返回支持的心超切面和测量指标元数据。"""
        return {
            "views": sorted(SUPPORTED_DCM_TYPES),
            "metrics": METRIC_META,
        }

    return mcp
