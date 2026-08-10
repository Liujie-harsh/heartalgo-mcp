"""心衰诊断算法服务启动入口（心超 + ECG 混合任务）。

配置优先级：CLI 参数 >config.py 内置默认值。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn
from fastapi.middleware.cors import CORSMiddleware

from api import FakeRunner, create_app
from config import ECGFMConfig, MeasurementConfig
from combined_runner import CombinedRunner, GPUResourcePool


ALGORITHM_DIR = Path(__file__).resolve().parent
WORK_DIR = ALGORITHM_DIR.parent


def build_app(
    use_fake: bool = False,
    script_dir: str | None = None,
    measurement_script_dir: str | None = None,
    measurement_python: str | None = None,
    ecg_project_dir: str | None = None,
    ecg_checkpoint: str | None = None,
    ecg_python: str | None = None,
    ecg_top_k: int | None = None,
    ecg_timeout_seconds: int | None = None,
    task_work_root: str | None = None,
):
    """构建 app，注入实际 runner、任务目录与两个模型的健康检查回调。"""
    work_root = task_work_root or os.environ.get("TASK_WORK_ROOT")
    if not work_root and not use_fake:
        print("[WARN] 未指定 TASK_WORK_ROOT (--task-work-root 或 TASK_WORK_ROOT 环境变量)，"
              "推理产物将写到系统 temp，不做任务隔离。")
    if use_fake and not work_root:
        work_root = None


    if use_fake:
        runner = FakeRunner(metrics={
            "lvef": 35.48, "lvedd": 55.0, "lvesd": 40.0,
            "lad": 35.0, "ea": 2.02, "gls": None,
        })
    else:
        from combined_runner import CombinedRunner
        from echonet_runner import EchoNetRunner
        from ecgfm_runner import ECGFMRunner

        ecg_config = ECGFMConfig.resolve(
            project_dir=ecg_project_dir,
            checkpoint=ecg_checkpoint,
            python_executable=ecg_python,
            top_k=ecg_top_k,
            timeout_seconds=ecg_timeout_seconds,
        )
        measurement_config = MeasurementConfig.resolve(
            script_dir=measurement_script_dir or script_dir,
            python_executable=measurement_python,
        )
        ecg_runner = ECGFMRunner(config=ecg_config, work_root=work_root)
        echo_runner = EchoNetRunner(config=measurement_config)
        gpu_ids = [item.strip() for item in os.environ.get("PYTHON_GPU_IDS", "0").split(",") if item.strip()]
        runner = CombinedRunner(echo_runner=echo_runner, ecg_runner=ecg_runner, gpu_pool=GPUResourcePool(gpu_ids))

    queue_workers = int(os.environ.get("PYTHON_QUEUE_WORKERS", "1"))
    app = create_app(
        runner=runner,
        sync=False,
        work_root=work_root,
        queue_worker_count=queue_workers,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["POST"],
        allow_headers=["*"],
    )
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="心衰诊断算法服务（心超 + ECG）")
    parser.add_argument("--fake", action="store_true", help="使用 FakeRunner（本机无 torch 时测试）")
    parser.add_argument("--script-dir", type=str, default=None,
                        help="旧兼容参数：Measurement 推理脚本目录")
    parser.add_argument("--measurement-script-dir", type=str, default=None,
                        help="Measurement 推理脚本目录（优先于 --script-dir；支持 MEASUREMENT_SCRIPT_DIR）")
    parser.add_argument("--measurement-python", type=str, default=None,
                        help="Measurement 运行 Python（支持 MEASUREMENT_PYTHON）")
    parser.add_argument("--ecg-project-dir", type=str, default=None,
                        help="ECG-FM 项目目录（支持 ECGFM_PROJECT_DIR）")
    parser.add_argument("--ecg-checkpoint", type=str, default=None,
                        help="ECG-FM 微调权重（支持 ECGFM_CHECKPOINT）")
    parser.add_argument("--ecg-python", type=str, default=None,
                        help="ECG-FM Python（支持 ECGFM_PYTHON）")
    parser.add_argument("--ecg-top-k", type=int, default=None,
                        help="ECG 疾病概率 Top-K（支持 ECGFM_TOP_K）")
    parser.add_argument("--ecg-timeout-seconds", type=int, default=None,
                        help="ECG 单阶段超时秒数（支持 ECGFM_TIMEOUT_SECONDS）")
    parser.add_argument("--task-work-root", type=str, default="G:\\heart-algo\\runtime",
                        help="任务产物根目录")
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    app = build_app(
        use_fake=args.fake,
        script_dir=args.script_dir,
        measurement_script_dir=args.measurement_script_dir,
        measurement_python=args.measurement_python,
        ecg_project_dir=args.ecg_project_dir,
        ecg_checkpoint=args.ecg_checkpoint,
        ecg_python=args.ecg_python,
        ecg_top_k=args.ecg_top_k,
        ecg_timeout_seconds=args.ecg_timeout_seconds,
        task_work_root=args.task_work_root,
    )
    measurement_dir = (
        args.measurement_script_dir or args.script_dir
        or os.environ.get("MEASUREMENT_SCRIPT_DIR")
        or MeasurementConfig.DEFAULT_SCRIPT_DIR
    )
    measurement_python = (
        args.measurement_python or os.environ.get("MEASUREMENT_PYTHON")
        or MeasurementConfig.DEFAULT_PYTHON_EXECUTABLE
    )
    print(f"[启动] fake={args.fake} | measurement_script_dir={measurement_dir}")
    print(f"  work_root          : {args.task_work_root or os.environ.get('TASK_WORK_ROOT')}")
    if not args.fake:
        print(f"  Measurement python : {measurement_python}")
        print(f"  ECG project        : {args.ecg_project_dir or os.environ.get('ECGFM_PROJECT_DIR') or ECGFMConfig.DEFAULT_PROJECT_DIR}")
        print(f"  ECG checkpoint     : {args.ecg_checkpoint or os.environ.get('ECGFM_CHECKPOINT') or ECGFMConfig.DEFAULT_CHECKPOINT}")
        print(f"  ECG python         : {args.ecg_python or os.environ.get('ECGFM_PYTHON') or ECGFMConfig.DEFAULT_PYTHON_EXECUTABLE}")
    uvicorn.run(app, host=args.host, port=args.port)