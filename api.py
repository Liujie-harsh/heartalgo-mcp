"""
心衰诊断 API (异步任务模式, 支持心超 + ECG 混合任务)。

遵循《图像算法分析接口协议》:
  POST /heart-algo/task/start  → 启动分析任务 (BackgroundTasks 异步)
  POST /heart-algo/task/result → 查询任务结果

任务可含心超图 (Cardiac Ultrasound) 和/或心电信号 (ECG XML):
  - 心超: EchoNetRunner → 6 项指标 + HF 分型
  - ECG:  ECGFMRunner  → 疾病概率 Top-K
  - 混合: CombinedRunner 分流后合并
"""
from __future__ import annotations

from typing import Callable, Optional, Protocol

from fastapi import BackgroundTasks, FastAPI
from pydantic import BaseModel, Field

from rules import analyze


class ImgItem(BaseModel):
    imgId: str
    imgPath: str
    imgType: str  # "Cardiac Ultrasound" | "ECG"


class StartRequest(BaseModel):
    requestId: str
    sysUserId: str
    taskId: str
    imgs: list[ImgItem]


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


class ECGPrediction(BaseModel):
    """One ECG-FM disease label and its sigmoid probability."""

    label: str
    probability: float


class ECGMeasurements(BaseModel):
    """ECG 测量值 (改进 #8, 同事贡献: parse_ecg_xml 解析 HL7 aECG XML 提取)。

    所有字段 Optional, 缺失时返回 null (exclude_none=True 会剔除)。
    """

    ventRate: Optional[float] = None
    prInterval: Optional[float] = None
    qrsDuration: Optional[float] = None
    qt: Optional[float] = None
    qtc: Optional[float] = None
    pAxis: Optional[float] = None
    qrsAxis: Optional[float] = None
    tAxis: Optional[float] = None


class ECGPatientInfo(BaseModel):
    """ECG 患者信息 (改进 #8, 同事贡献: parse_ecg_xml 提取)。"""

    name: Optional[str] = None
    age: Optional[int] = None
    sex: Optional[str] = None


class RoiPoint(BaseModel):
    """ROI 线段端点坐标。"""

    xPos: int
    yPos: int


class RoiSegment(BaseModel):
    """结构化 ROI 线段 (D 方案): 类型 + 帧号 + 两点坐标。

    type: "LVEDD" | "LVESD" | "LAD" (左室舒张末径 / 收缩末径 / 左房径)
    frameIndex: 线段取自多帧 DICOM 的第几帧 (0-based)
    points: 线段两端点 [point1, point2]
    """

    type: str
    frameIndex: int
    points: list[RoiPoint]


class Report(BaseModel):
    """协议 reports[].report: 单个分析报告。"""

    reportId: str
    reportResult: list[str]


class ImgResult(BaseModel):
    """协议 imgs[].img: 单个图像信息。

    方案 B (mentor 要求, 扩展协议): 每个 img 带独立 reportResult 字段。
    ecgPredictions / ecgMeasurements / ecgPatientInfo 不在原协议中, ECG-FM 结构化
    返回暂挂此处 (改进 #8), 待与后端对齐后调整。
    """

    imgId: str
    imgType: str
    reportId: str
    rois: list[RoiSegment] = Field(default_factory=list)
    reportResult: list[str] = Field(default_factory=list)
    ecgPredictions: list[ECGPrediction] = Field(default_factory=list)
    ecgMeasurements: Optional[ECGMeasurements] = None
    ecgPatientInfo: Optional[ECGPatientInfo] = None


class ReportItem(BaseModel):
    """协议 reports[]: 包裹层 {report: {...}}。"""

    report: Report


class ImgItemResult(BaseModel):
    """协议 imgs[]: 包裹层 {img: {...}}。"""

    img: ImgResult


class ResultResponse(BaseModel):
    responseId: str
    resultCode: int
    resultMsg: str
    taskId: str
    taskState: int
    failedReason: Optional[str] = None
    reports: list[ReportItem] = Field(default_factory=list)
    imgs: list[ImgItemResult] = Field(default_factory=list)


class InferenceRunner(Protocol):
    def run(self, imgs: list[ImgItem], task_id: str = "", work_root: str | None = None) -> dict: ...


class FakeRunner:
    """测试用假推理器, 返回固定心超指标, 无需 torch/GPU。"""

    def __init__(self, metrics: dict):
        self._metrics = metrics

    def run(self, imgs: list[ImgItem], task_id: str = "", work_root: str | None = None) -> dict:
        return dict(self._metrics)


class TaskStore:
    """内存任务存储。生产部署换 Redis/DB 支持多进程。

    幂等规则 (同事建议 #8):
      - 同 taskId 已存在: 返回已有状态, 不重复启动 (taskId 幂等)
      - 同 requestId 重发: 视为重复请求, 返回已有结果 (requestId 幂等)
      - 同 taskId 不同 requestId: 视为覆盖, 重新创建任务
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


def create_app(
    runner: InferenceRunner,
    sync: bool = False,
    work_root: str | None = None,
    ecgfm_health_check: Callable[[], dict] | None = None,
) -> FastAPI:
    """
    创建 FastAPI app。

    Args:
        runner: 推理器 (FakeRunner 测试 / CombinedRunner 生产)
        sync: True=start 同步执行 (测试用); False=BackgroundTasks 异步 (生产用)
        work_root: 任务产物根目录 (TASK_WORK_ROOT), 传给 runner 做任务隔离
        ecgfm_health_check: ECG-FM 健康检查回调 (返回 status dict), 由 main.py 注入
                           runner.health_check (实例级) 或 ECGFMRunner.health_check_from_env (静态)
    """
    app = FastAPI(title="心衰诊断算法服务")
    app.state.runner = runner
    app.state.store = TaskStore()
    app.state.sync = sync
    app.state.work_root = work_root
    app.state.ecgfm_health_check = ecgfm_health_check

    @app.get("/health")
    def health():
        """健康检查端点 (阶段 1.3, 改进 #12)。

        返回 ECG-FM 配置/文件/解释器状态, 不加载模型。
        未注入 ecgfm_health_check 时返回 unconfigured。
        """
        if app.state.ecgfm_health_check is None:
            ecgfm = {
                "status": "unconfigured",
                "errors": ["ECG-FM health check is not configured"],
            }
        else:
            ecgfm = app.state.ecgfm_health_check()
        return {
            "status": "ok" if ecgfm.get("status") == "healthy" else "degraded",
            "ecgfm": ecgfm,
        }

    @app.post("/heart-algo/task/start", response_model=StartResponse, response_model_exclude_none=True)
    def start(req: StartRequest, background_tasks: BackgroundTasks):
        store: TaskStore = app.state.store
        # 幂等性检查 1: 同 requestId 重发 → 直接返回已有任务 (避免重复推理)
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
        # 幂等性检查 2: 同 taskId 已存在 → 返回已有状态, 不重复启动
        # - 已完成 (state=2) / 已失败 (state=3): 不重跑
        # - 分析中 (state=1): 不重复启动 BackgroundTasks
        existing = store.get(req.taskId)
        if existing is not None and existing["taskState"] in (1, 2, 3):
            return StartResponse(
                responseId=req.requestId,
                resultCode=0,
                resultMsg=f"task already exists, state={existing['taskState']}",
                taskId=req.taskId,
                taskState=existing["taskState"],
            )

        store.create(req.taskId, req.imgs, request_id=req.requestId)
        if app.state.sync:
            _execute(app, req.taskId, req.imgs)
        else:
            background_tasks.add_task(_execute, app, req.taskId, req.imgs)

        task = store.get(req.taskId)
        return StartResponse(
            responseId=req.requestId,
            resultCode=0,
            resultMsg="success" if task["taskState"] != 3 else task.get("failedReason", ""),
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

        response = ResultResponse(
            responseId=req.requestId,
            resultCode=0,
            resultMsg="success",
            taskId=req.taskId,
            taskState=task["taskState"],
            failedReason=task["failedReason"],
        )
        if task["taskState"] == 2 and task["result"] is not None:
            result_data = task["result"]
            response.reports = [ReportItem(
                report=Report(
                    reportId=req.taskId,
                    reportResult=_report_lines(result_data),
                )
            )]
            predictions = result_data.get("ecg_predictions", {})
            measures = result_data.get("ecg_measurements", {})
            patients = result_data.get("ecg_patient_info", {})
            echo_per_image = result_data.get("echo_per_image", {})
            response.imgs = [
                ImgItemResult(
                    img=ImgResult(
                        imgId=image.imgId,
                        imgType=image.imgType,
                        reportId=req.taskId,
                        rois=[
                            RoiSegment(
                                type=seg["type"],
                                frameIndex=seg["frameIndex"],
                                points=[RoiPoint(xPos=int(p[0]), yPos=int(p[1])) for p in seg["points"]],
                            )
                            for seg in echo_per_image.get(image.imgId, {}).get("rois", [])
                        ],
                        reportResult=_img_report_lines(image.imgId, image.imgType, result_data),
                        ecgPredictions=[ECGPrediction(**item) for item in predictions.get(image.imgId, [])],
                        ecgMeasurements=ECGMeasurements(**measures[image.imgId]) if image.imgId in measures else None,
                        ecgPatientInfo=ECGPatientInfo(**patients[image.imgId]) if image.imgId in patients else None,
                    )
                )
                for image in task["imgs"]
            ]
        elif task["taskState"] == 3:
            # 失败时: 心超图 reportResult 显示 "未分析", ECG 图返回空模型
            reason = task["failedReason"] or "analysis failed"
            response.reports = [ReportItem(
                report=Report(reportId=req.taskId, reportResult=[f"分析失败: {reason}"])
            )]
            response.imgs = [
                ImgItemResult(
                    img=ImgResult(
                        imgId=image.imgId,
                        imgType=image.imgType,
                        reportId=req.taskId,
                        reportResult=[] if image.imgType == "ECG" else ["未分析"],
                        ecgMeasurements=ECGMeasurements() if image.imgType == "ECG" else None,
                        ecgPatientInfo=ECGPatientInfo() if image.imgType == "ECG" else None,
                    )
                )
                for image in task["imgs"]
            ]
        return response

    return app


def _img_report_lines(img_id: str, img_type: str, result: dict) -> list[str]:
    """方案 B (mentor 要求): 单个图像的独立推理报告行。

    心超 (Cardiac Ultrasound): 从 echo_per_image[imgId] 取指标, 生成 1 行指标文本
    ECG: 从 ecg_measurements/ecg_patient_info/ecg_predictions[imgId] 生成 3 行
        - ECG patient: 姓名/性别/年龄
        - ECG measurements: HR/PR/QRS/QT/QTc/电轴
        - ECG disease probability Top-K
    """
    lines: list[str] = []
    if img_type == "Cardiac Ultrasound":
        per_img = result.get("echo_per_image", {}).get(img_id, {})
        # 阶段 2: 单图推理失败 → 返回错误信息
        if per_img.get("error"):
            return [f"分析失败: {per_img['error']}"]
        parts = []
        if per_img.get("lvef") is not None:
            parts.append(f"LVEF={per_img['lvef']}%")
        if per_img.get("lvedd") is not None:
            parts.append(f"LVEDD={per_img['lvedd']}mm")
        if per_img.get("lvesd") is not None:
            parts.append(f"LVESD={per_img['lvesd']}mm")
        if per_img.get("lad") is not None:
            parts.append(f"LAD={per_img['lad']}mm")
        if per_img.get("ea") is not None:
            parts.append(f"E/A={per_img['ea']}")
        if per_img.get("gls") is not None:
            parts.append(f"GLS={per_img['gls']}")
        if parts:
            lines.append(", ".join(parts))
        else:
            # 无任何指标 (单帧 skip 图 / 未跑推理) → 标记未分析
            lines.append("未分析")
    elif img_type == "ECG":
        # 患者信息行
        patient = result.get("ecg_patient_info", {}).get(img_id, {})
        if patient:
            name = patient.get("name") or "未知"
            age = patient.get("age")
            sex = patient.get("sex") or "未知"
            age_str = f"{age}岁" if age is not None else "年龄未知"
            sex_zh = {"M": "男", "F": "女"}.get(sex, sex)
            lines.append(f"ECG patient ({img_id}): {name}, {sex_zh}, {age_str}")
        # 测量值行
        m = result.get("ecg_measurements", {}).get(img_id, {})
        if m:
            parts = []
            if "ventRate" in m:
                parts.append(f"HR={m['ventRate']}bpm")
            if "prInterval" in m:
                parts.append(f"PR={m['prInterval']}ms")
            if "qrsDuration" in m:
                parts.append(f"QRS={m['qrsDuration']}ms")
            if "qt" in m:
                parts.append(f"QT={m['qt']}ms")
            if "qtc" in m:
                parts.append(f"QTc={m['qtc']}ms")
            if "pAxis" in m:
                parts.append(f"P轴={m['pAxis']}")
            if "qrsAxis" in m:
                parts.append(f"QRS轴={m['qrsAxis']}")
            if "tAxis" in m:
                parts.append(f"T轴={m['tAxis']}")
            if parts:
                lines.append(f"ECG measurements ({img_id}): {', '.join(parts)}")
        # Top-K 行
        predictions = result.get("ecg_predictions", {}).get(img_id, [])
        if predictions:
            top_k = ", ".join(
                f"{item['label']}={item['probability']:.2%}" for item in predictions
            )
            lines.append(f"ECG disease probability Top-K ({img_id}): {top_k}")
    return lines


def _report_lines(result: dict) -> list[str]:
    """生成报告文本行: 心超分型 + 指标 + ECG 患者信息 + 测量值 + Top-K。"""
    lines: list[str] = []
    if result.get("has_echo"):
        lines.extend([
            f"Heart failure type: {result.get('hf_type', 'unknown')}",
            "LVEF={lvef}%, LVEDD={lvedd}mm, LVESD={lvesd}mm, LAD={lad}mm, E/A={ea}, GLS={gls}".format(**result),
        ])
    # ECG 患者信息 + 测量值 (同事贡献: parse_ecg_xml 提取)
    for image_id, patient in result.get("ecg_patient_info", {}).items():
        if not patient:
            continue
        name = patient.get("name") or "未知"
        age = patient.get("age")
        sex = patient.get("sex") or "未知"
        age_str = f"{age}岁" if age is not None else "年龄未知"
        sex_zh = {"M": "男", "F": "女"}.get(sex, sex)
        lines.append(f"ECG patient ({image_id}): {name}, {sex_zh}, {age_str}")
    for image_id, m in result.get("ecg_measurements", {}).items():
        if not m:
            continue
        # ventRate/prInterval/qrsDuration/qt/qtc/pAxis/qrsAxis/tAxis
        parts = []
        if "ventRate" in m:
            parts.append(f"HR={m['ventRate']}bpm")
        if "prInterval" in m:
            parts.append(f"PR={m['prInterval']}ms")
        if "qrsDuration" in m:
            parts.append(f"QRS={m['qrsDuration']}ms")
        if "qt" in m:
            parts.append(f"QT={m['qt']}ms")
        if "qtc" in m:
            parts.append(f"QTc={m['qtc']}ms")
        if "pAxis" in m:
            parts.append(f"P轴={m['pAxis']}")
        if "qrsAxis" in m:
            parts.append(f"QRS轴={m['qrsAxis']}")
        if "tAxis" in m:
            parts.append(f"T轴={m['tAxis']}")
        if parts:
            lines.append(f"ECG measurements ({image_id}): {', '.join(parts)}")
    for image_id, predictions in result.get("ecg_predictions", {}).items():
        if not predictions:
            continue
        top_k = ", ".join(
            f"{item['label']}={item['probability']:.2%}" for item in predictions
        )
        lines.append(f"ECG disease probability Top-K ({image_id}): {top_k}")
    return lines or ["No analyzable cardiac-ultrasound or ECG result was produced."]


def _execute(app: FastAPI, task_id: str, imgs: list[ImgItem]) -> None:
    """执行推理并更新任务状态。ECG-only 任务不强求心超指标。"""
    store: TaskStore = app.state.store
    runner: InferenceRunner = app.state.runner
    work_root: str | None = getattr(app.state, "work_root", None)
    try:
        raw_result = runner.run(imgs, task_id=task_id, work_root=work_root)
        echo_keys = ("lvef", "lvedd", "lvesd", "lad", "ea", "gls")
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
