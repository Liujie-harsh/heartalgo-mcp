"""
心衰诊断 API (异步任务模式, 支持心超 + ECG 混合任务)。

遵循《图像算法分析接口协议 v3》:
  POST /heart-algo/task/start  → 启动分析任务 (BackgroundTasks 异步)
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
from typing import Callable, Literal, Optional, Protocol

from fastapi import BackgroundTasks, FastAPI
from pydantic import BaseModel, Field

from rules import analyze


# ────────────────── 指标元数据 (中文名/单位/参考范围) ──────────────────

METRIC_META: dict[str, dict[str, str]] = {
    # B-Mode 距离/厚度 (mm)
    "aorticroot":  {"name_cn": "主动脉根部内径",     "unit": "mm",   "reference": "20–37"},
    "aorta":       {"name_cn": "升主动脉内径",       "unit": "mm",   "reference": "20–37"},
    "lad":         {"name_cn": "左房内径",          "unit": "mm",   "reference": "19–40"},
    "lvedd":       {"name_cn": "左室舒末径",        "unit": "mm",   "reference": "35–55"},
    "lvesd":       {"name_cn": "左室缩末径",        "unit": "mm",   "reference": "25–35"},
    "ivs":         {"name_cn": "室间隔厚",          "unit": "mm",   "reference": "6–11"},
    "lvpw":        {"name_cn": "左室后壁厚",        "unit": "mm",   "reference": "6–11"},
    "rvbase":      {"name_cn": "右室内径",          "unit": "mm",   "reference": "0–20"},
    "ivc":         {"name_cn": "下腔静脉内径",      "unit": "mm",   "reference": "10–25"},
    "pa":          {"name_cn": "主肺动脉",          "unit": "mm",   "reference": "0–26"},
    "lvef":        {"name_cn": "左室射血分数(EF)",   "unit": "%",    "reference": "55–70"},
    # Doppler 流速 (cm/s)
    "mv_e":        {"name_cn": "二尖瓣E峰流速",     "unit": "cm/s", "reference": "60–130"},
    "mv_a":        {"name_cn": "二尖瓣A峰流速",     "unit": "cm/s", "reference": "40–100"},
    "mv_ea":       {"name_cn": "E/A",              "unit": "-",    "reference": "0.8–2.0"},
    "av_vmax":     {"name_cn": "主动脉瓣峰值流速",   "unit": "cm/s", "reference": "70–220"},
    "tr_vmax":     {"name_cn": "三尖瓣反流峰值流速", "unit": "cm/s", "reference": "≤280"},
    "mr_vmax":     {"name_cn": "二尖瓣反流峰值流速", "unit": "cm/s", "reference": "—"},
    "lvot_vmax":   {"name_cn": "左室流出道峰值流速", "unit": "cm/s", "reference": "70–120"},
    # TDI (cm/s)
    "tdi_lateral": {"name_cn": "二尖瓣环侧壁 e'",   "unit": "cm/s", "reference": "≥10"},
    "tdi_medial":  {"name_cn": "二尖瓣环间隔侧 e'", "unit": "cm/s", "reference": "≥7"},
    # M-Mode (mm)
    "tapse":       {"name_cn": "TAPSE",            "unit": "mm",   "reference": "≥17"},
}


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


# ────────────────── 内部展平模型 (runner 接口) ──────────────────

class ImgItem(BaseModel):
    """内部展平后的图像项, 由 start 请求体转换而来。

    dcmType 仅心超图有值 (从 CardiacUltrasoundGroup.dcmType 带下), ECG 为 None。
    runner 按 dcmType 查 DCM_TYPE_TASKS 表做切面分流。
    """

    imgId: str
    imgPath: str
    imgType: str  # "Cardiac Ultrasound" | "ECG"
    dcmType: Optional[str] = None


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
    """单个模型分析报告, reportResult 为 JSON 字符串。"""

    reportId: str
    reportType: Literal["ECGFM", "MEASUREMENT"]
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


# ────────────────── 任务存储 ──────────────────

class TaskStore:
    """内存任务存储。生产部署换 Redis/DB 支持多进程。

    幂等规则:
      - 同 taskId 已存在: 返回已有状态, 不重复启动 (taskId 幂等)
      - 同 requestId 重发: 视为重复请求, 返回已有结果 (requestId 幂等)
    """

    def __init__(self):
        self._tasks: dict[str, dict] = {}
        self._request_index: dict[str, str] = {}  # requestId → taskId

    def create(self, task_id: str, imgs: list[ImgItem], request_id: str = "") -> None:
        # 清理旧 requestId 映射 (taskId 被覆盖时)
        old = self._tasks.get(task_id)
        if old and old.get("requestId"):
            self._request_index.pop(old["requestId"], None)
        self._tasks[task_id] = {
            "taskState": 1,
            "result": None,
            "failedReason": None,
            "imgs": imgs,
            "requestId": request_id,
        }
        if request_id:
            self._request_index[request_id] = task_id

    def get(self, task_id: str) -> Optional[dict]:
        return self._tasks.get(task_id)

    def get_by_request_id(self, request_id: str) -> Optional[tuple[str, dict]]:
        """同 requestId 重发 → 返回 (taskId, task), 不重复创建。"""
        task_id = self._request_index.get(request_id)
        if task_id is None:
            return None
        return task_id, self._tasks.get(task_id)

    def complete(self, task_id: str, result: dict) -> None:
        self._tasks[task_id]["taskState"] = 2
        self._tasks[task_id]["result"] = result

    def fail(self, task_id: str, reason: str) -> None:
        self._tasks[task_id]["taskState"] = 3
        self._tasks[task_id]["failedReason"] = reason


# ────────────────── 请求展平 ──────────────────

def _flatten_request(req: StartRequest) -> list[ImgItem]:
    """将 v3 分组请求展平为 list[ImgItem], 供 runner 使用。

    心超: dcmId→imgId, dcmPath→imgPath, imgType="Cardiac Ultrasound", dcmType 从 group 带下
    ECG:  ecgId→imgId, ecgPath→imgPath, imgType="ECG", dcmType=None
    """
    imgs: list[ImgItem] = []
    for group in req.cardiacUltrasound:
        for dcm in group.dcms:
            imgs.append(ImgItem(
                imgId=dcm.dcmId, imgPath=dcm.dcmPath,
                imgType="Cardiac Ultrasound", dcmType=group.dcmType,
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
    ecgfm_health_check: Callable[[], dict] | None = None,
) -> FastAPI:
    """创建 FastAPI app。

    Args:
        runner: 推理器 (FakeRunner 测试 / CombinedRunner 生产)
        sync: True=start 同步执行 (测试用); False=BackgroundTasks 异步 (生产用)
        work_root: 任务产物根目录 (TASK_WORK_ROOT), 传给 runner 做任务隔离
        store: 任务存储 (测试可注入; 默认内存 TaskStore)
        ecgfm_health_check: ECG-FM 健康检查回调
    """
    app = FastAPI(title="心衰诊断算法服务")
    app.state.runner = runner
    app.state.store = store or TaskStore()
    app.state.sync = sync
    app.state.work_root = work_root
    app.state.ecgfm_health_check = ecgfm_health_check

    @app.get("/health")
    def health():
        """健康检查端点。"""
        if app.state.ecgfm_health_check is None:
            ecgfm = {"status": "unconfigured", "errors": ["ECG-FM health check is not configured"]}
        else:
            ecgfm = app.state.ecgfm_health_check()
        return {"status": "ok" if ecgfm.get("status") == "healthy" else "degraded", "ecgfm": ecgfm}

    @app.post("/heart-algo/task/start", response_model=StartResponse, response_model_exclude_none=True)
    def start(req: StartRequest, background_tasks: BackgroundTasks):
        store: TaskStore = app.state.store
        # 幂等性检查 1: 同 requestId 重发 → 直接返回已有任务
        if req.requestId:
            existing_by_req = store.get_by_request_id(req.requestId)
            if existing_by_req is not None:
                existing_task_id, existing_task = existing_by_req
                return StartResponse(
                    responseId=req.requestId,
                    resultCode=0,
                    resultMsg=f"duplicate request, task state={existing_task['taskState']}",
                    taskId=existing_task_id,
                    taskState=existing_task["taskState"],
                )
        # 幂等性检查 2: 同 taskId 已存在 → 返回已有状态
        existing = store.get(req.taskId)
        if existing is not None and existing["taskState"] in (1, 2, 3):
            return StartResponse(
                responseId=req.requestId,
                resultCode=0,
                resultMsg=f"task already exists, state={existing['taskState']}",
                taskId=req.taskId,
                taskState=existing["taskState"],
            )

        imgs = _flatten_request(req)
        store.create(req.taskId, imgs, request_id=req.requestId)
        if app.state.sync:
            _execute(app, req.taskId, imgs)
        else:
            background_tasks.add_task(_execute, app, req.taskId, imgs)

        task = store.get(req.taskId)
        is_failed = task["taskState"] == 3
        return StartResponse(
            responseId=req.requestId,
            resultCode=1 if is_failed else 0,
            resultMsg=task.get("failedReason", "") if is_failed else "success",
            taskId=req.taskId,
            taskState=task["taskState"],
        )

    @app.post("/heart-algo/task/result", response_model=ResultResponse, response_model_exclude_none=True)
    def result(req: ResultRequest):
        store: TaskStore = app.state.store
        task = store.get(req.taskId)
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
            _fill_result_payload(response, req.taskId, task["imgs"], task["result"])
        elif task["taskState"] == 3:
            _fill_failure_payload(response, req.taskId, task["imgs"], task["failedReason"])
        return response

    return app


# ────────────────── 响应组装 ──────────────────

def _json_report(payload: dict) -> str:
    """将结构化模型结果序列化为接口要求的 JSON 字符串。"""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _echo_rois(result: dict, img_id: str) -> list[Roi]:
    """将推理器的心超 ROI 转换为对外协议字段。"""
    segments = result.get("echo_per_image", {}).get(img_id, {}).get("rois", [])
    return [
        Roi(
            roiType=segment["type"],
            points=[RoiPoint(xPos=int(point[0]), yPos=int(point[1])) for point in segment["points"]],
        )
        for segment in segments
    ]


def _fill_result_payload(response: ResultResponse, task_id: str, images: list[ImgItem], result: dict) -> None:
    """按 ECG 与心超两类文件组装完成任务的响应。"""
    for image in images:
        if image.imgType == "ECG":
            report_id = f"{task_id}:{image.imgId}:ecg"
            payload = {
                "ecgId": image.imgId,
                "patientInfo": result.get("ecg_patient_info", {}).get(image.imgId, {}),
                "measurements": result.get("ecg_measurements", {}).get(image.imgId, {}),
                "predictions": result.get("ecg_predictions", {}).get(image.imgId, []),
            }
            response.reports.append(Report(reportId=report_id, reportType="ECGFM", reportResult=_json_report(payload)))
            response.ecg.append(ECGResult(ecgId=image.imgId, ecgPath=image.imgPath, reportId=report_id))
        elif image.imgType == "Cardiac Ultrasound":
            report_id = f"{task_id}:{image.imgId}:measurement"
            per_image = result.get("echo_per_image", {}).get(image.imgId, {})
            if not per_image:
                per_image = {
                    key: result[key]
                    for key in ("lvef", "lvedd", "lvesd", "lad", "mv_ea", "hf_type")
                    if key in result
                }
            # measurements 带中文名/单位/参考范围
            measurements: dict = {}
            for key, value in per_image.items():
                if key in {"rois", "error", "skipReason"}:
                    continue
                meta = METRIC_META.get(key)
                if meta:
                    measurements[key] = {"value": value, **meta}
                else:
                    measurements[key] = {"value": value}
            payload = {
                "dcmId": image.imgId,
                "measurements": measurements,
            }
            if per_image.get("skipReason"):
                payload["skipReason"] = per_image["skipReason"]
            if per_image.get("error"):
                payload["error"] = per_image["error"]
            response.reports.append(Report(reportId=report_id, reportType="MEASUREMENT", reportResult=_json_report(payload)))
            response.cardiacUltrasound.append(CardiacUltrasoundResult(
                dcmId=image.imgId,
                dcmPath=image.imgPath,
                reportId=report_id,
                rois=_echo_rois(result, image.imgId),
            ))


def _fill_failure_payload(response: ResultResponse, task_id: str, images: list[ImgItem], reason: str | None) -> None:
    """失败任务仍按文件返回可关联的报告, 具体原因在 JSON 和 failedReason 中均可取得。"""
    failure_reason = reason or "analysis failed"
    for image in images:
        if image.imgType == "ECG":
            report_id = f"{task_id}:{image.imgId}:ecg"
            response.reports.append(Report(
                reportId=report_id,
                reportType="ECGFM",
                reportResult=_json_report({"ecgId": image.imgId, "error": failure_reason}),
            ))
            response.ecg.append(ECGResult(ecgId=image.imgId, ecgPath=image.imgPath, reportId=report_id))
        elif image.imgType == "Cardiac Ultrasound":
            report_id = f"{task_id}:{image.imgId}:measurement"
            response.reports.append(Report(
                reportId=report_id,
                reportType="MEASUREMENT",
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
        raw_result = runner.run(imgs, task_id=task_id, work_root=work_root)
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
        store.complete(task_id, result)
    except Exception as exc:
        store.fail(task_id, str(exc))
