"""
心衰诊断算法服务启动入口 (心超 + ECG 混合任务)。

本机测试 (FakeRunner, 无需 torch):
  cd F:\\project\\prototype\\prototype\\algorithm
  python main.py --fake

服务器 (真实推理, 心超 + ECG, 路径已硬编码为默认值, 直接 python main.py 即可):
  cd G:\\meaurements\\measurements\\Measurement
  set PYTHONPATH=G:\\meaurements\\measurements\\Measurement\\algorithm
  python G:\\meaurements\\measurements\\Measurement\\algorithm\\main.py --host 0.0.0.0 --port 8000

配置优先级: CLI 参数 > config.py 内置默认值。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn
from fastapi.middleware.cors import CORSMiddleware

from api import FakeRunner, create_app
from config import ECGFMConfig, MeasurementConfig


ALGORITHM_DIR = Path(__file__).resolve().parent
WORK_DIR = ALGORITHM_DIR.parent


def build_app(
    use_fake: bool = False,
    measurement_script_dir: str | None = None,
    measurement_python: str | None = None,
    ecg_project_dir: str | None = None,
    ecg_checkpoint: str | None = None,
    ecg_python: str | None = None,
    ecg_top_k: int | None = None,
    ecg_timeout_seconds: int | None = None,
    task_work_root: str | None = None,
):
    """构建 app, 注入 runner + CORS + 健康检查回调。"""
    work_root = task_work_root or os.environ.get("TASK_WORK_ROOT")
    if not work_root and not use_fake:
        print("[WARN] 未指定 TASK_WORK_ROOT (--task-work-root 或 TASK_WORK_ROOT 环境变量), "
              "推理产物将写到系统 temp, 不做任务隔离。")
    if use_fake and not work_root:
        work_root = None  # FakeRunner 不需要 work_root

    ecgfm_health_check = None  # FakeRunner 模式下无 ECG-FM 健康检查

    if use_fake:
        runner = FakeRunner(metrics={
            "lvef": 35.48, "lvedd": 55.0, "lvesd": 40.0,
            "lad": 35.0, "ea": 2.02, "gls": None,
        })
    else:
        from combined_runner import CombinedRunner
        from echonet_runner import EchoNetRunner
        from ecgfm_runner import ECGFMRunner

        measurement_config = MeasurementConfig.resolve(
            script_dir=measurement_script_dir,
            python_executable=measurement_python,
        )
        ecg_config = ECGFMConfig.from_cli(
            project_dir=ecg_project_dir or os.environ.get("ECGFM_PROJECT_DIR")
                        or str(ECGFMConfig.DEFAULT_PROJECT_DIR),
            checkpoint=ecg_checkpoint or os.environ.get("ECGFM_CHECKPOINT")
                        or str(ECGFMConfig.DEFAULT_CHECKPOINT),
            python_executable=ecg_python or os.environ.get("ECGFM_PYTHON")
                        or str(ECGFMConfig.DEFAULT_PYTHON_EXECUTABLE),
            top_k=ecg_top_k or ECGFMConfig.DEFAULT_TOP_K,
            timeout_seconds=ecg_timeout_seconds or ECGFMConfig.DEFAULT_TIMEOUT_SECONDS,
        )
        ecg_runner = ECGFMRunner(config=ecg_config, work_root=work_root)
        echo_runner = EchoNetRunner(config=measurement_config)
        runner = CombinedRunner(echo_runner=echo_runner, ecg_runner=ecg_runner)

    app = create_app(
        runner=runner,
        sync=False,  # 生产用异步
        work_root=work_root,
        ecgfm_health_check=ecgfm_health_check,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],   # MVP; 生产改为前端域名
        allow_methods=["POST"],
        allow_headers=["*"],
    )
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="心衰诊断算法服务 (心超 + ECG)")
    parser.add_argument("--fake", action="store_true", help="用 FakeRunner (本机无 torch 时测试)")
    parser.add_argument("--measurement-script-dir", type=str, default=None,
                        help="Measurement 推理脚本目录 (支持 MEASUREMENT_SCRIPT_DIR)")
    parser.add_argument("--measurement-python", type=str, default=None,
                        help="Measurement 运行 Python (支持 MEASUREMENT_PYTHON)")
    parser.add_argument("--ecg-project-dir", type=str, default=None,
                        help="ECG-FM 项目目录 (含 scripts/ 子目录, 支持 ECGFM_PROJECT_DIR)")
    parser.add_argument("--ecg-checkpoint", type=str, default=None,
                        help="ECG-FM 微调权重路径 (支持 ECGFM_CHECKPOINT)")
    parser.add_argument("--ecg-python", type=str, default=None,
                        help="ECG-FM conda 环境的 python.exe (支持 ECGFM_PYTHON)")
    parser.add_argument("--ecg-top-k", type=int, default=None, help="ECG 疾病概率返回 Top-K")
    parser.add_argument("--ecg-timeout-seconds", type=int, default=None,
                        help="ECG 单次推理子进程超时 (秒, 默认 300)")
    parser.add_argument("--task-work-root", type=str, default="G:\\heart-algo\\runtime",
                        help="任务产物根目录 (默认 G:\\heart-algo\\runtime\\)")
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    app = build_app(
        use_fake=args.fake,
        measurement_script_dir=args.measurement_script_dir or os.environ.get("MEASUREMENT_SCRIPT_DIR"),
        measurement_python=args.measurement_python or os.environ.get("MEASUREMENT_PYTHON"),
        ecg_project_dir=args.ecg_project_dir or os.environ.get("ECGFM_PROJECT_DIR"),
        ecg_checkpoint=args.ecg_checkpoint or os.environ.get("ECGFM_CHECKPOINT"),
        ecg_python=args.ecg_python or os.environ.get("ECGFM_PYTHON"),
        ecg_top_k=args.ecg_top_k,
        ecg_timeout_seconds=args.ecg_timeout_seconds,
        task_work_root=args.task_work_root,
    )
    work_root_display = args.task_work_root or os.environ.get("TASK_WORK_ROOT", "未配置(FakeRunner 不需要)")
    measurement_dir = args.measurement_script_dir or os.environ.get("MEASUREMENT_SCRIPT_DIR") or MeasurementConfig.DEFAULT_SCRIPT_DIR
    print(f"[启动] fake={args.fake} | measurement_script_dir={measurement_dir}")
    print(f"  work_root  : {work_root_display}")
    if not args.fake:
        print(f"  ECG project : {args.ecg_project_dir or os.environ.get('ECGFM_PROJECT_DIR') or ECGFMConfig.DEFAULT_PROJECT_DIR}")
        print(f"  ECG ckpt    : {args.ecg_checkpoint or os.environ.get('ECGFM_CHECKPOINT')}")
        print(f"  ECG python  : {args.ecg_python or os.environ.get('ECGFM_PYTHON')}")
    uvicorn.run(app, host=args.host, port=args.port)
