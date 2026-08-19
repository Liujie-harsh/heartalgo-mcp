"""
心衰诊断 API (异步任务模式, 支持心超 + ECG 混合任务)。

遵循《图像算法分析接口协议 v3》:
  POST /heart-algo/task/start  → 启动分析任务（进程内队列异步）
  POST /heart-algo/task/result → 查询任务结果

v3 请求体按切面分组:
  cardiacUltrasound: [{dcmType, dcms:[{dcmId, dcmPath}]}]
  ecg: [{ecgId, ecgPath}]

v3 响应体心超与 ECG 分离:
  cardiacUltrasound: [{dcmId, dcmPath, reportId, rois:[{roiType, points}]}]
  ecg: [{ecgId, ecgPath, reportId}]
  reports: [{reportId, reportType, reportResult(JSON 字符串)}]
"""
from __future__ import annotations

import json
import logging
from typing import Literal, Optional, Protocol

from fastapi import FastAPI
from pydantic import BaseModel, Field

from rules import analyze
from algorithm_errors import to_public_error
from combined_runner import InProcessTaskQueue
from task_models import ImgItem
from task_outcome import build_success_outcome
from task_store import InMemoryTaskStore, TaskOwnershipError, TaskStore


logger = logging.getLogger(__name__)


# ────────────────── v3 请求体模型 ──────────────────

class DcmItem(BaseModel):
    """心超 dcm 影像图。"""

    dcmId: str
    dcmPath: str


class CardiacUltrasoundGroup(BaseModel):
    """心超数据分组, 按 dcmType 切面类型分组。

    dcmType 合法值 (SLICE_DIR_MAP key):
      分支1 B-Mode: PLAX / A4C / Subcostal / RVOT
      分支2 Doppler: MV_EA / AV_Vmax / TR_Vmax / MR_Vmax / LVOT_Vmax
      分支3 TDI: TDI_Medial / TDI_Lateral
      分支4 M-Mode: TAPSE
    """

    dcmType: str
    dcms: list[DcmItem]


class EcgItem(BaseModel):
    """心电图数据。"""

    ecgId: str
    ecgPath: str


class StartRequest(BaseModel):
    """v3 start 请求: 心超按切面分组 + ECG 列表。"""

    requestId: str
    sysUserId: str
    taskId: str
    cardiacUltrasound: list[CardiacUltrasoundGroup] = Field(default_factory=list)
    ecg: list[EcgItem] = Field(default_factory=list)


class StartResponse(BaseModel):
    responseId: str
    resultCode: int
    resultMsg: str
    taskId: str
    taskState: int


class ResultRequest(BaseModel):
    requestId: str
    sysUserId: str
    taskId: str


# ────────────────── v3 响应体模型 ──────────────────

class RoiPoint(BaseModel):
    """ROI 区域的坐标点。"""

    xPos: int
    yPos: int


class Roi(BaseModel):
    """心超测量区域。

    roiType 为指标名 (非几何类型), 合法值:
      PLAX: IVS / LVEDD / LVESD / LVPW / LA / Aorta / AorticRoot
      A4C:  RVBase
      Subcostal: IVC
      RVOT:  PA
      Doppler/TDI/M-Mode: 对应指标名或空 rois
    """

    roiType: str
    points: list[RoiPoint]


class Report(BaseModel):
    """单个模型分析报告, reportResult 为 JSON 字符串。

    reportType 遵循 v3 协议:
      CU-SUB     - 心超影像图对应的分析报告 (per-img)
      CU-SUMMARY - 心超综合分析报告 (顶层汇总, 不关联单图)
      ECG        - 心电图分析报告
    """

    reportId: str
    reportType: Literal["CU-SUB", "CU-SUMMARY", "ECG"]
    reportResult: str


class CardiacUltrasoundResult(BaseModel):
    """心超源文件与其报告的关联。"""

    dcmId: str
    dcmPath: str
    reportId: str
    rois: list[Roi] = Field(default_factory=list)


class ECGResult(BaseModel):
    """心电源文件与其报告的关联。"""

    ecgId: str
    ecgPath: str
    reportId: str


class ResultResponse(BaseModel):
    responseId: str
    resultCode: int
    resultMsg: str
    taskId: str
    taskState: int
    failedReason: Optional[str] = None
    reports: list[Report] = Field(default_factory=list)
    cardiacUltrasound: list[CardiacUltrasoundResult] = Field(default_factory=list)
    ecg: list[ECGResult] = Field(default_factory=list)


# ────────────────── runner 协议 ──────────────────

class InferenceRunner(Protocol):
    def run(self, imgs: list[ImgItem], task_id: str = "", work_root: str | None = None) -> dict: ...


class FakeRunner:
    """测试用假推理器, 返回固定心超指标, 无需 torch/GPU。"""

    def __init__(self, metrics: dict):
        self._metrics = metrics

    def run(self, imgs: list[ImgItem], task_id: str = "", work_root: str | None = None) -> dict:
        return dict(self._metrics)


# ────────────────── 请求展平 ──────────────────

def _flatten_request(req: StartRequest) -> list[ImgItem]:
    """将 v3 分组请求展平为 list[ImgItem], 供 runner 使用。

    imgType 用大写下划线常量 (与 DB input_type 枚举一致):
      心超: CARDIAC_ULTRASOUND
      ECG : ECG
    """
    imgs: list[ImgItem] = []
    for group in req.cardiacUltrasound:
        for dcm in group.dcms:
            imgs.append(ImgItem(
                imgId=dcm.dcmId, imgPath=dcm.dcmPath,
                imgType="CARDIAC_ULTRASOUND", dcmType=group.dcmType,
            ))
    for ecg in req.ecg:
        imgs.append(ImgItem(
            imgId=ecg.ecgId, imgPath=ecg.ecgPath, imgType="ECG",
        ))
    return imgs


# ────────────────── FastAPI app ──────────────────

def create_app(
    runner: InferenceRunner,
    sync: bool = False,
    work_root: str | None = None,
    store: TaskStore | None = None,
    task_queue: InProcessTaskQueue | None = None,
    queue_worker_count: int = 1,
    stale_running_seconds: int = 0,
    algorithm_version: str | None = None,
) -> FastAPI:
    """创建 FastAPI app。

    Args:
        runner: 推理器 (FakeRunner 测试 / CombinedRunner 生产)
        sync: True=start 同步执行（测试用）；False=进程内队列异步（生产用）
        work_root: 任务产物根目录 (TASK_WORK_ROOT), 传给 runner 做任务隔离
        store: 任务存储 (测试可注入; 默认内存 TaskStore)
        stale_running_seconds: 启动时判定遗留运行中任务的宽限秒数
    """
    if stale_running_seconds < 0:
        raise ValueError("stale_running_seconds 不能小于 0")
    app = FastAPI(title="心衰诊断算法服务")
    app.state.runner = runner
    app.state.store = store or InMemoryTaskStore()
    app.state.sync = sync
    app.state.work_root = work_root
    app.state.algorithm_version = algorithm_version
    app.state.stale_running_seconds = stale_running_seconds
    app.state.owns_task_queue = task_queue is None
    app.state.task_queue = task_queue or InProcessTaskQueue(worker_count=queue_worker_count)

    @app.on_event("startup")
    def _recover_pending_tasks() -> None:
        for pending in app.state.store.recover_pending_tasks(stale_running_seconds):
            app.state.task_queue.enqueue(
                _execute,
                app,
                pending.task_id,
                pending.images,
            )

    if app.state.owns_task_queue:
        @app.on_event("shutdown")
        def _close_task_queue() -> None:
            app.state.task_queue.close(wait=True)

    @app.post("/heart-algo/task/start", response_model=StartResponse, response_model_exclude_none=True)
    def start(req: StartRequest):
        store: TaskStore = app.state.store
        imgs = _flatten_request(req)
        try:
            task_id, task, created = store.create_or_get(
                req.taskId,
                imgs,
                request_id=req.requestId,
                sys_user_id=req.sysUserId,
            )
        except TaskOwnershipError:
            return StartResponse(
                responseId=req.requestId,
                resultCode=1,
                resultMsg="task id conflict",
                taskId=req.taskId,
                taskState=3,
            )
        if not created:
            return StartResponse(
                responseId=req.requestId,
                resultCode=0,
                resultMsg=f"task already exists, state={task['taskState']}",
                taskId=task_id,
                taskState=task["taskState"],
            )

        if app.state.sync:
            _execute(app, task_id, imgs)
        else:
            app.state.task_queue.enqueue(_execute, app, task_id, imgs)

        task = store.get(task_id)
        is_failed = task["taskState"] == 3
        return StartResponse(
            responseId=req.requestId,
            resultCode=1 if is_failed else 0,
            resultMsg=task.get("failedReason", "") if is_failed else "success",
            taskId=task_id,
            taskState=task["taskState"],
        )

    @app.post("/heart-algo/task/result", response_model=ResultResponse, response_model_exclude_none=True)
    def result(req: ResultRequest):
        store: TaskStore = app.state.store
        task = store.get_for_user(req.taskId, req.sysUserId)
        if task is None:
            return ResultResponse(
                responseId=req.requestId,
                resultCode=1,
                resultMsg=f"task not found: {req.taskId}",
                taskId=req.taskId,
                taskState=3,
                failedReason="task not found",
            )

        is_failed = task["taskState"] == 3
        response = ResultResponse(
            responseId=req.requestId,
            resultCode=1 if is_failed else 0,
            resultMsg=task["failedReason"] if is_failed else "success",
            taskId=req.taskId,
            taskState=task["taskState"],
            failedReason=task["failedReason"],
        )
        if task["taskState"] == 2 and task["result"] is not None:
            _fill_result_payload(response, task["result"])
        elif task["taskState"] == 3:
            _fill_failure_payload(response, req.taskId, task["imgs"], task["failedReason"])
        return response

    return app


# ────────────────── 响应组装 ──────────────────

def _json_report(payload: dict) -> str:
    """将结构化模型结果序列化为接口要求的 JSON 字符串。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _fill_result_payload(response: ResultResponse, outcome: dict) -> None:
    """将存储层的结构化任务输出转换为 v3 响应模型。"""
    for report in outcome.get("reports", []):
        response.reports.append(Report(
            reportId=report["reportId"],
            reportType=report["reportType"],
            reportResult=_json_report(report["reportResult"]),
        ))
    response.cardiacUltrasound.extend(
        CardiacUltrasoundResult(**item)
        for item in outcome.get("cardiacUltrasound", [])
    )
    response.ecg.extend(ECGResult(**item) for item in outcome.get("ecg", []))


def _fill_failure_payload(response: ResultResponse, task_id: str, images: list[ImgItem], reason: str | None) -> None:
    """失败任务仍按文件返回可关联的报告, 具体原因在 JSON 和 failedReason 中均可取得。"""
    failure_reason = reason or "analysis failed"
    for image in images:
        if image.imgType == "ECG":
            report_id = f"{task_id}:{image.imgId}:ecg"
            response.reports.append(Report(
                reportId=report_id,
                reportType="ECG",
                reportResult=_json_report({"ecgId": image.imgId, "error": failure_reason}),
            ))
            response.ecg.append(ECGResult(ecgId=image.imgId, ecgPath=image.imgPath, reportId=report_id))
        elif image.imgType == "CARDIAC_ULTRASOUND":
            report_id = f"{task_id}:{image.imgId}:measurement"
            response.reports.append(Report(
                reportId=report_id,
                reportType="CU-SUB",
                reportResult=_json_report({"dcmId": image.imgId, "error": failure_reason}),
            ))
            response.cardiacUltrasound.append(CardiacUltrasoundResult(
                dcmId=image.imgId,
                dcmPath=image.imgPath,
                reportId=report_id,
            ))


# ────────────────── 任务执行 ──────────────────

def _execute(app: FastAPI, task_id: str, imgs: list[ImgItem]) -> None:
    """执行推理并更新任务状态。ECG-only 任务不强求心超指标。"""
    store: TaskStore = app.state.store
    runner: InferenceRunner = app.state.runner
    work_root: str | None = getattr(app.state, "work_root", None)
    try:
        if not store.claim(task_id):
            return
        raw_result = runner.run(imgs, task_id=task_id, work_root=work_root)
        if "mv_ea" not in raw_result and "ea" in raw_result:
            raw_result["mv_ea"] = raw_result["ea"]
        echo_keys = ("lvef", "lvedd", "lvesd", "lad", "mv_ea")
        has_echo = any(key in raw_result for key in echo_keys)
        result: dict = {
            "has_echo": has_echo,
            "ecg_predictions": raw_result.get("ecg_predictions", {}),
            "ecg_measurements": raw_result.get("ecg_measurements", {}),
            "ecg_patient_info": raw_result.get("ecg_patient_info", {}),
            "echo_per_image": raw_result.get("echo_per_image", {}),
        }
        if has_echo:
            missing = [key for key in echo_keys if key not in raw_result]
            if missing:
                raise ValueError(f"Echo runner result is missing: {', '.join(missing)}")
            result.update(analyze(**{key: raw_result[key] for key in echo_keys}))
        store.complete(
            task_id,
            build_success_outcome(
                task_id,
                imgs,
                result,
                algorithm_version=getattr(app.state, "algorithm_version", None),
            ),
        )
    except Exception as exc:
        public_error = to_public_error(exc)
        logger.exception(
            "算法任务执行失败 task_id=%s error_code=%s",
            task_id,
            public_error.code,
        )
        store.fail(task_id, public_error.message)
