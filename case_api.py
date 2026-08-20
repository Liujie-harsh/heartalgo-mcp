"""非 Agent 病例上传、算法提交和结构化报告 API。"""

from __future__ import annotations

import hashlib
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from api import _execute
from case_store import (
    CaseConflictError,
    CaseNotFoundError,
    CaseStoreError,
    FileCaseStore,
)
from task_models import ImgItem
from task_store import TaskOwnershipError, TaskStore


class CreateCaseRequest(BaseModel):
    requestId: str = Field(min_length=1, max_length=128)
    sysUserId: str = Field(min_length=1, max_length=128)


class SubmitDiagnosisRequest(BaseModel):
    requestId: str = Field(min_length=1, max_length=128)
    sysUserId: str = Field(min_length=1, max_length=128)
    assetIds: list[str] | None = None


class ReviewDiagnosisRequest(BaseModel):
    sysUserId: str = Field(min_length=1, max_length=128)
    reviewerId: str = Field(min_length=1, max_length=128)
    decision: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=2000)


@dataclass(frozen=True)
class _AuthContext:
    user_id: str | None
    roles: frozenset[str]


def _raise_http(exc: CaseStoreError) -> None:
    if isinstance(exc, CaseNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, CaseConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=422, detail=str(exc)) from exc


def _effective_user(
    declared_user: str,
    authenticated_user: str | None,
    require_authenticated_user: bool,
) -> str:
    """把业务所有权绑定到网关注入的已验证主体，拒绝请求体覆盖身份。"""
    if require_authenticated_user and not authenticated_user:
        raise HTTPException(status_code=401, detail="缺少已验证用户身份")
    if authenticated_user and authenticated_user != declared_user:
        raise HTTPException(status_code=403, detail="请求用户与已验证身份不一致")
    return authenticated_user or declared_user


def _select_assets(metadata: dict, requested_ids: list[str] | None) -> list[dict]:
    assets = metadata["assets"]
    if not requested_ids:
        selected = assets
    else:
        if len(set(requested_ids)) != len(requested_ids):
            raise HTTPException(status_code=422, detail="assetIds 不能重复")
        by_id = {item["assetId"]: item for item in assets}
        missing = [asset_id for asset_id in requested_ids if asset_id not in by_id]
        if missing:
            raise HTTPException(status_code=422, detail=f"资产不存在: {', '.join(missing)}")
        selected = [by_id[asset_id] for asset_id in requested_ids]
    if not selected:
        raise HTTPException(status_code=422, detail="病例尚未上传可诊断资产")
    return selected


def _images_from_assets(assets: list[dict]) -> list[ImgItem]:
    return [
        ImgItem(
            imgId=asset["assetId"],
            imgPath=asset["path"],
            imgType=asset["modality"],
            dcmType=asset.get("dcmType"),
        )
        for asset in assets
    ]


def _structured_result(
    case_id: str,
    task_id: str,
    task: dict,
    case_metadata: dict,
) -> dict:
    state = task["taskState"]
    base = {"caseId": case_id, "taskId": task_id}
    if state in (0, 1):
        return {**base, "status": "processing"}
    if state == 3:
        return {**base, "status": "failed", "error": task["failedReason"]}

    outcome = task.get("result") or {}
    reports = {item["reportId"]: item["reportResult"] for item in outcome.get("reports", [])}
    echo = []
    for item in outcome.get("cardiacUltrasound", []):
        payload = reports.get(item["reportId"], {})
        echo.append({
            "dcmId": item["dcmId"],
            "measurements": payload.get("measurements", {}),
            "rois": item.get("rois", []),
            "error": payload.get("error"),
            "skipReason": payload.get("skipReason"),
        })
    ecg = []
    for item in outcome.get("ecg", []):
        payload = reports.get(item["reportId"], {})
        ecg.append({
            "ecgId": item["ecgId"],
            "patientInfo": payload.get("patientInfo", {}),
            "measurements": payload.get("measurements", {}),
            "predictions": payload.get("predictions", []),
            "error": payload.get("error"),
        })
    summary = next(
        (
            report["reportResult"].get("measurements", {})
            for report in outcome.get("reports", [])
            if report["reportType"] == "CU-SUMMARY"
        ),
        {},
    )
    diagnosis = next(
        (
            item
            for item in case_metadata.get("diagnoses", [])
            if item["taskId"] == task_id
        ),
        {"assetIds": []},
    )
    selected_asset_ids = set(diagnosis["assetIds"])
    inputs = {
        asset["assetId"]: {"sha256": asset["sha256"], "sizeBytes": asset["sizeBytes"]}
        for asset in case_metadata["assets"]
        if asset["assetId"] in selected_asset_ids
    }
    matching_reviews = [
        review
        for review in case_metadata.get("reviewHistory", [])
        if review["taskId"] == task_id
    ]
    review = matching_reviews[-1] if matching_reviews else None
    return {
        **base,
        "status": "completed",
        "hfType": summary.get("hf_type"),
        "cardiacUltrasound": echo,
        "ecg": ecg,
        "inputs": inputs,
        "algorithmVersion": next(
            (
                item.get("reportResult", {}).get("algorithmVersion")
                for item in outcome.get("reports", [])
                if item.get("reportResult", {}).get("algorithmVersion")
            ),
            "unknown",
        ),
        "reviewStatus": review["decision"] if review else "pending",
        "requiresClinicianReview": review is None or review["decision"] != "approved",
        "review": review,
    }


def submit_case_diagnosis(
    app: FastAPI,
    case_store: FileCaseStore,
    case_id: str,
    request_id: str,
    sys_user_id: str,
    asset_ids: list[str] | None = None,
    task_id_prefix: str = "diagnosis",
) -> dict:
    """通过共享任务队列提交病例；HTTP 与 MCP 共用这一入口。"""
    try:
        metadata = case_store.get_case(case_id, sys_user_id)
    except CaseStoreError as exc:
        _raise_http(exc)
    assets = _select_assets(metadata, asset_ids)
    images = _images_from_assets(assets)
    selected_asset_ids = [image.imgId for image in images]
    try:
        diagnosis, diagnosis_created = case_store.reserve_diagnosis(
            case_id,
            sys_user_id,
            f"{task_id_prefix}-{uuid4().hex}",
            request_id,
            selected_asset_ids,
        )
    except CaseStoreError as exc:
        _raise_http(exc)
    task_id = diagnosis["taskId"]
    scoped_request_id = "case-" + hashlib.sha256(
        f"{case_id}\0{request_id}".encode("utf-8")
    ).hexdigest()
    store: TaskStore = app.state.store
    try:
        actual_task_id, task, task_created = store.create_or_get(
            task_id,
            images,
            request_id=scoped_request_id,
            sys_user_id=sys_user_id,
        )
    except TaskOwnershipError as exc:
        raise HTTPException(status_code=409, detail="诊断任务标识冲突") from exc
    if [image.imgId for image in task["imgs"]] != selected_asset_ids:
        raise HTTPException(status_code=409, detail="诊断任务标识已用于不同输入")
    case_store.mark_diagnosis_submitted(case_id, sys_user_id, actual_task_id)
    if task_created:
        if app.state.sync:
            _execute(app, actual_task_id, images)
        else:
            app.state.task_queue.enqueue(_execute, app, actual_task_id, images)
    return {
        "caseId": case_id,
        "taskId": actual_task_id,
        "status": {0: "queued", 1: "processing", 2: "completed", 3: "failed"}[
            task["taskState"]
        ],
        "created": diagnosis_created,
    }


def get_case_diagnosis_result(
    app: FastAPI,
    case_store: FileCaseStore,
    case_id: str,
    task_id: str,
    sys_user_id: str,
) -> dict:
    """读取且展平一个属于指定病例和用户的诊断结果。"""
    try:
        metadata = case_store.get_case(case_id, sys_user_id)
        if not case_store.diagnosis_belongs_to_case(case_id, sys_user_id, task_id):
            raise CaseNotFoundError("诊断任务不存在")
    except CaseStoreError as exc:
        _raise_http(exc)
    store: TaskStore = app.state.store
    task = store.get_for_user(task_id, sys_user_id)
    if task is None:
        raise HTTPException(status_code=404, detail="诊断任务不存在")
    return _structured_result(case_id, task_id, task, metadata)


def install_case_routes(
    app: FastAPI,
    case_store: FileCaseStore,
    *,
    require_authenticated_user: bool = False,
    trusted_proxy_secret: str | None = None,
    service_user_id: str = "mcp-service",
) -> None:
    """把病例闭环 API 安装到现有算法 FastAPI 应用。"""
    app.state.case_store = case_store
    app.state.case_auth_required = require_authenticated_user
    if require_authenticated_user and not trusted_proxy_secret:
        raise ValueError("启用病例鉴权时必须配置 CASE_TRUSTED_PROXY_SECRET")

    def authenticated_context(
        authenticated_user: str | None = Header(None, alias="X-Authenticated-User"),
        authenticated_roles: str = Header("", alias="X-Authenticated-Roles"),
        proxy_secret: str | None = Header(None, alias="X-Auth-Proxy-Secret"),
    ) -> _AuthContext:
        if require_authenticated_user:
            if not proxy_secret or not secrets.compare_digest(
                proxy_secret, trusted_proxy_secret or ""
            ):
                raise HTTPException(status_code=401, detail="请求未经过受信身份网关")
            if not authenticated_user:
                raise HTTPException(status_code=401, detail="缺少已验证用户身份")
        roles = frozenset(
            item.strip() for item in authenticated_roles.split(",") if item.strip()
        )
        return _AuthContext(authenticated_user, roles)

    def case_access_user(
        case_id: str, declared_user_id: str, auth: _AuthContext
    ) -> str:
        if require_authenticated_user and "cardiology-reviewer" in auth.roles:
            try:
                metadata = case_store.get_case_by_id(case_id)
            except CaseStoreError as exc:
                _raise_http(exc)
            if metadata["sysUserId"] != declared_user_id:
                raise HTTPException(status_code=404, detail="病例不存在")
            return metadata["sysUserId"]
        return _effective_user(
            declared_user_id, auth.user_id, require_authenticated_user
        )

    def recover_reserved_case_submissions() -> None:
        """补建“病例已落盘、任务尚未创建”窗口里的任务。"""
        store: TaskStore = app.state.store
        for metadata, diagnosis in case_store.reserved_diagnoses():
            assets_by_id = {item["assetId"]: item for item in metadata["assets"]}
            try:
                assets = [assets_by_id[item] for item in diagnosis["assetIds"]]
            except KeyError:
                continue
            images = _images_from_assets(assets)
            scoped_request_id = "case-" + hashlib.sha256(
                f"{metadata['caseId']}\0{diagnosis['requestId']}".encode("utf-8")
            ).hexdigest()
            _, _, created = store.create_or_get(
                diagnosis["taskId"],
                images,
                request_id=scoped_request_id,
                sys_user_id=metadata["sysUserId"],
            )
            case_store.mark_diagnosis_submitted(
                metadata["caseId"], metadata["sysUserId"], diagnosis["taskId"]
            )
            if created:
                if app.state.sync:
                    _execute(app, diagnosis["taskId"], images)
                else:
                    app.state.task_queue.enqueue(
                        _execute, app, diagnosis["taskId"], images
                    )

    _base_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _case_lifespan(current_app):
        async with _base_lifespan(current_app):
            recover_reserved_case_submissions()
            yield

    app.router.lifespan_context = _case_lifespan

    @app.get("/heart-algo/portal", include_in_schema=False)
    def case_portal():
        return FileResponse(
            Path(__file__).resolve().parent / "web" / "heart_portal.html",
            media_type="text/html; charset=utf-8",
        )

    @app.post("/heart-algo/cases", status_code=status.HTTP_201_CREATED)
    def create_case(
        req: CreateCaseRequest,
        auth: _AuthContext = Depends(authenticated_context),
    ):
        user_id = _effective_user(
            req.sysUserId, auth.user_id, require_authenticated_user
        )
        try:
            metadata, created = case_store.create_case(
                user_id, req.requestId, [service_user_id]
            )
        except CaseStoreError as exc:
            _raise_http(exc)
        response = FileCaseStore.public_case(metadata)
        response["created"] = created
        return response

    @app.get("/heart-algo/cases/{case_id}")
    def get_case(
        case_id: str,
        sys_user_id: str = Query(...),
        auth: _AuthContext = Depends(authenticated_context),
    ):
        user_id = case_access_user(case_id, sys_user_id, auth)
        try:
            return FileCaseStore.public_case(case_store.get_case(case_id, user_id))
        except CaseStoreError as exc:
            _raise_http(exc)

    @app.post(
        "/heart-algo/cases/{case_id}/assets",
        status_code=status.HTTP_201_CREATED,
    )
    def upload_asset(
        case_id: str,
        sysUserId: str = Form(...),
        modality: str = Form(...),
        assetId: str | None = Form(None),
        dcmType: str | None = Form(None),
        file: UploadFile = File(...),
        auth: _AuthContext = Depends(authenticated_context),
    ):
        user_id = _effective_user(
            sysUserId, auth.user_id, require_authenticated_user
        )
        try:
            asset, created = case_store.add_asset(
                case_id,
                user_id,
                assetId or f"asset-{uuid4().hex}",
                modality,
                dcmType,
                file.file,
            )
        except CaseStoreError as exc:
            _raise_http(exc)
        finally:
            file.file.close()
        return {
            **{key: value for key, value in asset.items() if key != "path"},
            "created": created,
        }

    @app.post(
        "/heart-algo/cases/{case_id}/diagnoses",
        status_code=status.HTTP_202_ACCEPTED,
    )
    def submit_diagnosis(
        case_id: str,
        req: SubmitDiagnosisRequest,
        auth: _AuthContext = Depends(authenticated_context),
    ):
        user_id = _effective_user(
            req.sysUserId, auth.user_id, require_authenticated_user
        )
        return submit_case_diagnosis(
            app,
            case_store,
            case_id,
            req.requestId,
            user_id,
            req.assetIds,
        )

    @app.get("/heart-algo/cases/{case_id}/diagnoses/{task_id}")
    def get_diagnosis(
        case_id: str,
        task_id: str,
        sys_user_id: str = Query(...),
        auth: _AuthContext = Depends(authenticated_context),
    ):
        user_id = case_access_user(case_id, sys_user_id, auth)
        return get_case_diagnosis_result(
            app, case_store, case_id, task_id, user_id
        )

    @app.post(
        "/heart-algo/cases/{case_id}/diagnoses/{task_id}/review",
        status_code=status.HTTP_201_CREATED,
    )
    def review_diagnosis(
        case_id: str,
        task_id: str,
        req: ReviewDiagnosisRequest,
        auth: _AuthContext = Depends(authenticated_context),
    ):
        if require_authenticated_user:
            reviewer_id = _effective_user(
                req.reviewerId, auth.user_id, require_authenticated_user
            )
            if "cardiology-reviewer" not in auth.roles:
                raise HTTPException(status_code=403, detail="缺少临床复核角色")
            try:
                metadata = case_store.get_case_by_id(case_id)
            except CaseStoreError as exc:
                _raise_http(exc)
            user_id = metadata["sysUserId"]
            if req.sysUserId != user_id:
                raise HTTPException(status_code=404, detail="病例不存在")
            if reviewer_id == user_id:
                raise HTTPException(status_code=403, detail="病例所有者不能自我复核")
        else:
            user_id = req.sysUserId
            reviewer_id = req.reviewerId
        store: TaskStore = app.state.store
        task = store.get_for_user(task_id, user_id)
        if task is None:
            raise HTTPException(status_code=404, detail="诊断任务不存在")
        if task["taskState"] != 2:
            raise HTTPException(status_code=409, detail="只有已完成任务可以复核")
        try:
            return case_store.record_review(
                case_id,
                user_id,
                task_id,
                reviewer_id,
                req.decision,
                req.comment,
            )
        except CaseStoreError as exc:
            _raise_http(exc)
