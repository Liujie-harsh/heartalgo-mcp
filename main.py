"""
心衰诊断算法服务启动入口 (心超 + ECG 混合任务)。

本机测试 (FakeRunner, 无需 torch):
  cd F:\\project\\prototype\\prototype\\algorithm
  python main.py --fake

服务器 (真实推理, 心超 + ECG, 路径已硬编码为默认值, 直接 python main.py 即可):
  cd G:\\meaurements\\measurements\\Measurement
  set PYTHONPATH=G:\\meaurements\\measurements\\Measurement\\algorithm
  python G:\\meaurements\\measurements\\Measurement\\algorithm\\main.py --host 0.0.0.0 --port 8000

ECG-FM 路径优先级: CLI 参数 > 环境变量 (ECGFM_PROJECT_DIR / ECGFM_CHECKPOINT / ECGFM_PYTHON) > 默认值
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import uvicorn
from fastapi.middleware.cors import CORSMiddleware

from api import FakeRunner, create_app
from config import ECGFMConfig


ALGORITHM_DIR = Path(__file__).resolve().parent
WORK_DIR = ALGORITHM_DIR.parent
# ECG-FM 默认路径 (可被 --ecg-project-dir / --ecg-checkpoint 覆盖)
# 项目目录: G:\ecg-fm\ecg-fm\ecg-fm (含 scripts/, data/)
# 权重:     G:\ecg-fm\ecg-fm\weights\mimic_iv_ecg_finetuned.pt
DEFAULT_ECG_PROJECT = "G:\\ecg-fm\\ecg-fm\\ecg-fm"
DEFAULT_ECG_CHECKPOINT = "G:\\ecg-fm\\ecg-fm\\weights\\mimic_iv_ecg_finetuned.pt"
DEFAULT_ECG_PYTHON = "C:\\Users\\Administrator\\miniconda3\\envs\\ecg_env\\python.exe"


def build_app(
    use_fake: bool = False,
    script_dir: str | None = None,
    ecg_project_dir: str | None = None,
    ecg_checkpoint: str | None = None,
    ecg_python: str | None = None,
    ecg_top_k: int = 5,
    ecg_timeout_seconds: int = 300,
    task_work_root: str | None = None,
):
    """构建 app, 注入 runner + CORS + 健康检查回调。"""
    # 任务产物根目录: 参数 > 环境变量 > 默认 G:\heart-algo\runtime\
    # 开发机用 FakeRunner 时可不传, 真实推理时必须配置
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

        # ECG-FM 配置优先级: CLI 参数 > 环境变量 > 默认值 (Q3 决策, config 作桥梁)
        selected_project = ecg_project_dir or os.environ.get("ECGFM_PROJECT_DIR") or DEFAULT_ECG_PROJECT
        selected_checkpoint = ecg_checkpoint or os.environ.get("ECGFM_CHECKPOINT") or DEFAULT_ECG_CHECKPOINT
        selected_python = ecg_python or os.environ.get("ECGFM_PYTHON") or DEFAULT_ECG_PYTHON
        if not (ecg_python or os.environ.get("ECGFM_PYTHON")):
            print(f"[WARN] 未指定 ECG-FM python 环境 (--ecg-python 或 ECGFM_PYTHON), "
                  f"使用默认: {DEFAULT_ECG_PYTHON}")

        # 构造 ECGFMConfig (不校验存在性, 路径有效性交给 /health 检测)
        ecg_config = ECGFMConfig.from_cli(
            project_dir=selected_project,
            checkpoint=selected_checkpoint,
            python_executable=selected_python,
            top_k=ecg_top_k,
            timeout_seconds=ecg_timeout_seconds,
        )
        ecg_runner = ECGFMRunner(config=ecg_config, work_root=work_root)

        runner = CombinedRunner(
            echo_runner=EchoNetRunner(script_dir=script_dir),
            ecg_runner=ecg_runner,
        )
        # Q1 决策: 传实例级 health_check (读 self.config), 而非静态 health_check_from_env
        ecgfm_health_check = ecg_runner.health_check

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
    parser.add_argument("--script-dir", type=str, default="G:\\meaurements\\measurements\\Measurement",
                        help="EchoNet 推理脚本目录 (服务器路径)")
    parser.add_argument("--ecg-project-dir", type=str, default=str(DEFAULT_ECG_PROJECT),
                        help="ECG-FM 项目目录 (含 scripts/ 子目录)")
    parser.add_argument("--ecg-checkpoint", type=str, default=str(DEFAULT_ECG_CHECKPOINT),
                        help="ECG-FM 微调权重路径")
    parser.add_argument("--ecg-python", type=str,
                        default="C:\\Users\\Administrator\\miniconda3\\envs\\ecg_env\\python.exe",
                        help="ECG-FM conda 环境的 python.exe (也可用 ECGFM_PYTHON 环境变量)")
    parser.add_argument("--ecg-top-k", type=int, default=5, help="ECG 疾病概率返回 Top-K")
    parser.add_argument("--ecg-timeout-seconds", type=int,
                        default=int(os.environ.get("ECGFM_TIMEOUT_SECONDS", "300")),
                        help="ECG 单次推理子进程超时 (秒, 默认 300, 也可用 ECGFM_TIMEOUT_SECONDS 环境变量)")
    parser.add_argument("--task-work-root", type=str, default="G:\\heart-algo\\runtime",
                        help="任务产物根目录 (默认 G:\\heart-algo\\runtime\\, 也可用 TASK_WORK_ROOT 环境变量)")
    parser.add_argument("--host", type=str, default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    app = build_app(
        use_fake=args.fake,
        script_dir=args.script_dir,
        ecg_project_dir=args.ecg_project_dir,
        ecg_checkpoint=args.ecg_checkpoint,
        ecg_python=args.ecg_python,
        ecg_top_k=args.ecg_top_k,
        ecg_timeout_seconds=args.ecg_timeout_seconds,
        task_work_root=args.task_work_root,
    )
    work_root_display = args.task_work_root or os.environ.get("TASK_WORK_ROOT", "未配置(FakeRunner 不需要)")
    print(f"[启动] fake={args.fake} | script_dir={args.script_dir or '默认(同目录)'}")
    print(f"  work_root  : {work_root_display}")
    if not args.fake:
        print(f"  ECG project : {args.ecg_project_dir}")
        print(f"  ECG ckpt    : {args.ecg_checkpoint}")
        print(f"  ECG python  : {args.ecg_python or os.environ.get('ECGFM_PYTHON', 'sys.executable (默认)')}")
        print(f"  ECG timeout : {args.ecg_timeout_seconds}s")
    uvicorn.run(app, host=args.host, port=args.port)
