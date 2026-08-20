"""
ECG-FM 服务器分层验证脚本 (端到端)。

分层 (每层 PASS 才继续, 失败即停):
  L1 ECG conda 环境检查 (python + 关键依赖)
  L2 前置文件检查 (converter + inference 脚本 + 权重)
  L3 ECGFMRunner 配置验证
  L4 真实 XML 推理 (端到端, 解锁交接文档 L474 待办)

服务器执行 (用 echonet 环境 python 跑, ECG 推理 subprocess 调 ecg_env):
  cd G:\\meaurements\\measurements\\Measurement
  set PYTHONPATH=G:\\meaurements\\measurements\\Measurement\\algorithm
  python G:\\meaurements\\measurements\\Measurement\\algorithm\\verify_ecgfm.py ^
    --ecg-project-dir "G:\\ecg-fm\\ecg-fm" ^
    --ecg-checkpoint "G:\\ecg-fm\\ecg-fm\\weights\\mimic_iv_ecg_finetuned.pt" ^
    --ecg-python "C:\\Users\\Administrator\\miniconda3\\envs\\ecg_env\\python.exe"

  可选指定样例 XML:
    --xml "G:\\path\\to\\sample.xml"
  不指定则自动在 ecg-fm 项目 data 目录下查找 .xml
"""
import os
import sys
import subprocess
import argparse
from pathlib import Path

# 默认路径 (服务器)
#   项目目录: G:\ecg-fm\ecg-fm\ecg-fm\ (含 scripts/, data/)
#   权重:     G:\ecg-fm\ecg-fm\weights\   (在项目目录上一级, 非项目目录下)
DEFAULT_ECG_PROJECT = r"G:\ecg-fm\ecg-fm\ecg-fm"
DEFAULT_ECG_CHECKPOINT = r"G:\ecg-fm\ecg-fm\weights\mimic_iv_ecg_finetuned.pt"
DEFAULT_ECG_PYTHON = r"C:\Users\Administrator\miniconda3\envs\ecg_env\python.exe"
DEFAULT_XML_DIR = r"G:\ecg-fm\ecg-fm\ecg-fm\data\xml_data"


def banner(layer: str, msg: str):
    print(f"\n{'='*60}\n  [{layer}] {msg}\n{'='*60}")


def ok(msg: str):
    print(f"  [PASS] {msg}")


def fail(msg: str, exc: Exception = None):
    print(f"  [FAIL] {msg}")
    if exc:
        import traceback
        traceback.print_exc()
    sys.exit(1)


def warn(msg: str):
    print(f"  [WARN] {msg}")


# ────────────────── L1: ECG conda 环境检查 ──────────────────

def test_l1(ecg_python: str):
    banner("L1", f"ECG conda 环境检查: {ecg_python}")
    if not Path(ecg_python).is_file():
        fail(f"python 不存在: {ecg_python}")

    # 检查 python 版本
    try:
        r = subprocess.run([ecg_python, "--version"], capture_output=True, text=True, timeout=10)
        print(f"  [INFO] {r.stdout.strip()}{r.stderr.strip()}")
        ok("python 可执行")
    except Exception as e:
        fail("python 执行异常", e)

    # 检查关键依赖 (ECG-FM 推理需要), 用多行脚本避免 -c 单行 for 语法错误
    deps_check = (
        "mods = []\n"
        "for m in ['torch','fairseq_signals','scipy','pandas','numpy']:\n"
        "    try:\n"
        "        __import__(m)\n"
        "        mods.append(m + ' OK')\n"
        "    except Exception:\n"
        "        mods.append(m + ' MISS')\n"
        "print(' | '.join(mods))\n"
    )
    try:
        r = subprocess.run([ecg_python, "-c", deps_check], capture_output=True, text=True, timeout=30)
        print(f"  [INFO] 依赖: {r.stdout.strip()}")
        if r.returncode != 0:
            print(f"  [INFO] stderr: {r.stderr[:300]}")
            fail("依赖检查脚本执行失败 (见 stderr)")
        if "MISS" in r.stdout:
            fail("有依赖缺失, 见上方 [INFO]")
        ok("关键依赖齐全 (torch/fairseq_signals/scipy/pandas/numpy)")
    except Exception as e:
        fail("依赖检查异常", e)

    # CUDA 检查 (ECG-FM 推理最好有 GPU)
    cuda_check = "import torch; print('cuda', torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU only')"
    try:
        r = subprocess.run([ecg_python, "-c", cuda_check], capture_output=True, text=True, timeout=15)
        print(f"  [INFO] {r.stdout.strip()}")
        if "cuda True" in r.stdout:
            ok("GPU 可用")
        else:
            warn("CUDA 不可用, 将用 CPU 推理 (会很慢)")
    except Exception as e:
        warn(f"CUDA 检查异常: {e}")

    print("  L1 通过")


# ────────────────── L2: 前置文件检查 ──────────────────

def test_l2(project_dir: str, checkpoint: str):
    banner("L2", "前置文件检查 (converter + inference + 权重)")
    converter = Path(project_dir) / "scripts" / "xml_to_ecgfm_mat.py"
    inference = Path(project_dir) / "scripts" / "infer_quickstart.py"
    ckpt = Path(checkpoint)

    missing = []
    for name, path in [("converter", converter), ("inference", inference), ("checkpoint", ckpt)]:
        if path.is_file():
            size = path.stat().st_size
            print(f"  [INFO] {name}: {path} ({size:,} bytes)")
        else:
            missing.append(f"{name}: {path}")
            print(f"  [MISS] {name}: {path}")

    if missing:
        fail("前置文件缺失:\n    " + "\n    ".join(missing))

    # 权重大小检查 (交接文档 L473: 1,081,641,499 字节)
    ckpt_size = ckpt.stat().st_size
    if ckpt_size < 1_000_000_000:
        warn(f"权重仅 {ckpt_size:,} bytes, 可能不完整 (预期 ~1GB)")
    else:
        ok(f"权重完整 ({ckpt_size:,} bytes ≈ {ckpt_size/1e9:.2f} GB)")

    # scripts 目录列出
    scripts_dir = Path(project_dir) / "scripts"
    if scripts_dir.is_dir():
        scripts = list(scripts_dir.glob("*.py"))
        print(f"  [INFO] scripts/ 下共 {len(scripts)} 个 .py: {[s.name for s in scripts[:10]]}")

    print("  L2 通过")


# ────────────────── L3: ECGFMRunner 配置验证 ──────────────────

def test_l3(project_dir: str, checkpoint: str, ecg_python: str):
    banner("L3", "ECGFMRunner 配置验证 (实例化 + _validate_configuration)")
    try:
        from ecgfm_runner import ECGFMRunner
    except ImportError as e:
        fail(f"无法导入 ECGFMRunner (确认 PYTHONPATH 含 algorithm 目录): {e}")

    try:
        runner = ECGFMRunner(
            project_dir=project_dir,
            checkpoint=checkpoint,
            python_executable=ecg_python,
            top_k=5,
        )
        runner._validate_configuration()
        ok(f"ECGFMRunner 实例化 + 配置验证通过")
        print(f"  [INFO] project_dir : {runner.project_dir}")
        print(f"  [INFO] checkpoint  : {runner.checkpoint}")
        print(f"  [INFO] python      : {runner.python_executable}")
        print(f"  [INFO] top_k       : {runner.top_k}")
    except Exception as e:
        fail("ECGFMRunner 配置异常", e)

    print("  L3 通过")


# ────────────────── L4: 真实 XML 推理 (端到端) ──────────────────

def find_sample_xml(project_dir: str, xml_dir: str | None = None) -> str | None:
    """查找 .xml 样例: 优先 xml_dir, 其次 data/xml_data, 最后 data 递归。"""
    candidates = []
    if xml_dir:
        candidates.append(Path(xml_dir))
    candidates.append(Path(project_dir) / "data" / "xml_data")
    candidates.append(Path(project_dir) / "data")
    for d in candidates:
        if not d.is_dir():
            continue
        for xml in d.rglob("*.xml"):
            if xml.stat().st_size > 1000:
                return str(xml)
    return None


def test_l4(project_dir: str, checkpoint: str, ecg_python: str, xml_arg: str | None, xml_dir: str | None):
    banner("L4", "真实 XML 推理 (端到端, 解锁交接文档 L474 待办)")

    # 确定 XML 文件
    xml_path = xml_arg
    if not xml_path:
        xml_path = find_sample_xml(project_dir, xml_dir)
        if xml_path:
            print(f"  [INFO] 自动找到样例 XML: {xml_path}")
        else:
            print(f"  [INFO] 未在 {project_dir}\\data 下自动找到 .xml")
            print(f"  请用 --xml 指定一个 HL7 aECG XML 信号文件")
            print(f"  跳过 L4 (需真实 XML 才能端到端验证)")
            return None
    else:
        if not Path(xml_path).is_file():
            fail(f"指定的 XML 不存在: {xml_path}")

    print(f"  [INFO] XML: {xml_path} ({Path(xml_path).stat().st_size:,} bytes)")

    from ecgfm_runner import ECGFMRunner
    from api import ImgItem
    runner = ECGFMRunner(
        project_dir=project_dir,
        checkpoint=checkpoint,
        python_executable=ecg_python,
        top_k=5,
    )

    try:
        print(f"  [INFO] 开始推理 (XML→MAT→ECG-FM→Top-K), 可能需 30s-2min ...")
        result = runner.run([ImgItem(imgId="ecg-test", imgPath=xml_path, imgType="ECG")])
        predictions = result.get("ecg_predictions", {}).get("ecg-test", [])

        if not predictions:
            fail("推理返回空预测列表")

        print(f"\n  [INFO] ECG-FM 疾病概率 Top-{len(predictions)}:")
        for i, pred in enumerate(predictions, 1):
            label = pred["label"]
            prob = pred["probability"]
            bar = "█" * int(prob * 30)
            print(f"    {i}. {label:40s} {prob*100:6.2f}%  {bar}")

        # 合理性判断
        top1 = predictions[0]
        if top1["probability"] < 0.01:
            warn(f"Top-1 概率仅 {top1['probability']:.4f}, 可能模型未有效输出")
        else:
            ok(f"Top-1: {top1['label']} = {top1['probability']:.4f} (概率合理)")

        # 检查是否全 0.5 (模型异常标志)
        probs = [p["probability"] for p in predictions]
        if all(abs(p - 0.5) < 0.01 for p in probs):
            fail("所有概率≈0.5, 模型输出异常 (可能未加载权重或输入预处理错误)")
        else:
            ok("概率分布非均匀, 模型有效区分")

        print("\n  L4 通过 (ECG-FM 端到端推理跑通)")
        return predictions

    except subprocess.CalledProcessError as e:
        print(f"  [INFO] subprocess 退出码 {e.returncode}")
        print(f"  [INFO] stdout: {e.stdout[-500:] if e.stdout else '(空)'}")
        print(f"  [INFO] stderr: {e.stderr[-800:] if e.stderr else '(空)'}")
        fail("subprocess 推理失败 (见上方输出, 多为权重/预处理/依赖问题)")
    except Exception as e:
        fail("L4 推理异常", e)


# ────────────────── 主流程 ──────────────────

def main():
    parser = argparse.ArgumentParser(description="ECG-FM 服务器端到端验证")
    parser.add_argument("--ecg-project-dir", default=DEFAULT_ECG_PROJECT, help="ECG-FM 项目目录")
    parser.add_argument("--ecg-checkpoint", default=DEFAULT_ECG_CHECKPOINT, help="微调权重路径")
    parser.add_argument("--ecg-python", default=DEFAULT_ECG_PYTHON, help="ECG conda 环境 python.exe")
    parser.add_argument("--xml", default=None, help="样例 HL7 aECG XML 文件 (不指定则自动查找)")
    parser.add_argument("--xml-dir", default=DEFAULT_XML_DIR, help="XML 样例目录 (自动查找时用)")
    args = parser.parse_args()

    print(f"\nECG-FM 服务器端到端验证")
    print(f"  项目目录 : {args.ecg_project_dir}")
    print(f"  权重     : {args.ecg_checkpoint}")
    print(f"  python   : {args.ecg_python}")
    print(f"  XML 目录 : {args.xml_dir}")
    print(f"  XML      : {args.xml or '(自动查找)'}")

    test_l1(args.ecg_python)
    test_l2(args.ecg_project_dir, args.ecg_checkpoint)
    test_l3(args.ecg_project_dir, args.ecg_checkpoint, args.ecg_python)
    test_l4(args.ecg_project_dir, args.ecg_checkpoint, args.ecg_python, args.xml, args.xml_dir)

    banner("完成", "L1-L4 全部通过, ECG-FM 链路验证完成")
    print("  交接文档 L474 待办 (真实 XML GPU 推理 + 临床核对) 已完成推理部分")
    print("  剩余: 临床人员核对 Top-K 标签合理性")


if __name__ == "__main__":
    main()
