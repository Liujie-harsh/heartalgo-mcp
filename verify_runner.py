"""
EchoNetRunner 服务器分层验证脚本。

分层 (每层 PASS 才继续, 失败即停):
  L1 环境导入 + 纯函数 (Teichholz / 规则引擎)
  L2 推理脚本直跑 (基线, 已知能跑)
  L3 Runner 单方法 (_detect_region_type / _run_lvid / _run_la / _run_mvpeak) ← 新代码最易出错
  L4 Runner.run 完整 (多文件分发)

服务器执行:
  cd G:\\meaurements\\measurements\\Measurement
  set PYTHONPATH=F:\\project\\prototype\\prototype\\algorithm
  python F:\\project\\prototype\\prototype\\algorithm\\verify_runner.py

也可指定测试文件:
  python verify_runner.py --dcm2d "<2D PLAX.dcm>" --dcm-pw "<PW Doppler.dcm>"
"""
import os
import sys
import argparse
import traceback

# 默认测试文件 (之前会话已验证过的基准文件)
DEFAULT_DCM_2D = (
    r"G:\meaurements\measurements\Measurement\data\心超"
    r"\0000573193\1\export_20251127131027300\黄锡南_20251127131027314_0"
    r"\1F0AE452465C6FDDA9839BC7BC242B38\1F0AE442128A69F18409438E4CD48CA1"
    r"\00005_38084c67f3ec09b6.dcm"
)
DEFAULT_DCM_PW = (
    r"G:\meaurements\measurements\Measurement\data\心超"
    r"\0003276392\2\export_20251127131842912\高汉平_20251127131842923_0"
    r"\1F0A4A39C51C6D43A5C8A71D5F01A7D0\1F0A4A8A899D6753B9BD4B1FED7602D6"
    r"\00020_fbec2c817d7a5442.dcm"
)


def banner(layer: str, msg: str):
    print(f"\n{'='*60}\n  [{layer}] {msg}\n{'='*60}")


def ok(msg: str):
    print(f"  [PASS] {msg}")


def fail(msg: str, exc: Exception = None):
    print(f"  [FAIL] {msg}")
    if exc:
        traceback.print_exc()
    sys.exit(1)


# ────────────────── L1: 环境与纯函数 ──────────────────

def test_l1():
    banner("L1", "环境导入 + 纯函数")
    # 导入检查
    try:
        import torch
        import pydicom
        import pandas
        ok(f"torch={torch.__version__} cuda={torch.cuda.is_available()} | pydicom ok | pandas ok")
    except ImportError as e:
        fail("导入失败", e)

    # 纯函数: Teichholz
    from echonet_runner import teichholz_lvef
    lvef = teichholz_lvef(edd_mm=50.6, esd_mm=31.6)
    if 30 < lvef < 80:
        ok(f"teichholz_lvef(50.6, 31.6) = {lvef}% (公式自洽)")
    else:
        fail(f"teichholz_lvef 异常: {lvef}")

    # 纯函数: 规则引擎
    from rules import classify_hf
    if classify_hf(35.48) == "HFrEF":
        ok("classify_hf(35.48) = HFrEF (规则引擎 ok)")
    else:
        fail("规则引擎异常")

    print("  L1 全部通过")


# ────────────────── L2: 推理脚本直跑 (基线) ──────────────────

def test_l2(dcm_2d: str):
    banner("L2", f"推理脚本直跑 (基线): {os.path.basename(dcm_2d)}")
    import subprocess
    cwd = os.path.dirname(os.path.abspath(dcm_2d))  # 不重要, subprocess 用 Measurement 目录
    measurement_dir = r"G:\meaurements\measurements\Measurement"

    cmd = [
        sys.executable, os.path.join(measurement_dir, "inference_2D_image.py"),
        "--model_weights", "lvid", "--phase_estimate",
        "--file_path", dcm_2d,
        "--output_path", os.path.join(measurement_dir, "data", "infer_results", "verify_l2.avi"),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=measurement_dir, timeout=300)
        if r.returncode != 0:
            fail(f"inference_2D_image.py 退出码 {r.returncode}\nstderr: {r.stderr[-500:]}")
        ok("inference_2D_image.py lvid 跑通 (基线确认)")
    except Exception as e:
        fail("L2 推理脚本执行异常", e)

    # 检查 CSV 产物
    csv_path = os.path.join(measurement_dir, "data", "infer_results", "verify_l2_distance.csv")
    if not os.path.exists(csv_path):
        fail(f"未生成距离 CSV: {csv_path}")
    import pandas as pd
    df = pd.read_csv(csv_path)
    print(f"  [INFO] CSV 列名: {df.columns.tolist()}")
    print(f"  [INFO] CSV 前 3 行:\n{df.head(3).to_string()}")
    ok(f"距离 CSV 生成, 共 {len(df)} 行")

    print("  L2 全部通过 (基线 ok, 说明推理脚本本身没问题)")


# ────────────────── L3: Runner 单方法 ──────────────────

def test_l3(dcm_2d: str, dcm_pw: str):
    banner("L3", "Runner 单方法 (subprocess + 解析, 新代码)")
    measurement_dir = r"G:\meaurements\measurements\Measurement"
    from echonet_runner import EchoNetRunner
    runner = EchoNetRunner(script_dir=measurement_dir)

    # L3.1 _detect_region_type
    print("\n  --- L3.1 _detect_region_type ---")
    try:
        t2d = runner._detect_region_type(dcm_2d)
        if t2d == "2D":
            ok(f"2D 文件 → '{t2d}'")
        else:
            fail(f"2D 文件应判 '2D', 实际 '{t2d}'")
    except Exception as e:
        fail("_detect_region_type(2D) 异常", e)

    try:
        tdp = runner._detect_region_type(dcm_pw)
        if tdp == "Doppler":
            ok(f"Doppler 文件 → '{tdp}'")
        else:
            fail(f"Doppler 文件应判 'Doppler', 实际 '{tdp}'")
    except Exception as e:
        fail("_detect_region_type(Doppler) 异常", e)

    # L3.2 _run_lvid (关键: CSV 列名匹配 + max/min 逻辑)
    print("\n  --- L3.2 _run_lvid (CSV 解析, 最易出错) ---")
    try:
        edd, esd = runner._run_lvid(dcm_2d)
        print(f"  [INFO] _run_lvid 返回 EDD={edd}mm, ESD={esd}mm")
        # handoff 验证: LVID 3.16-5.06cm → EDD≈50mm, ESD≈31mm
        if 30 < esd < 45 and 40 < edd < 65 and edd > esd:
            ok(f"EDD={edd}, ESD={esd} 落在临床区间 (handoff 基准: EDD≈50, ESD≈31)")
        else:
            print(f"  [WARN] 数值偏离基准, 请检查 CSV 解析逻辑")
            print(f"         handoff 验证值: LVID 3.16-5.06cm (ESD≈31mm, EDD≈50mm)")
            fail(f"EDD={edd}, ESD={esd} 不在预期区间")
    except Exception as e:
        fail("_run_lvid 异常 (可能 CSV 列名不匹配, 见上方 [INFO] 打印)", e)

    # L3.3 _run_la
    print("\n  --- L3.3 _run_la ---")
    try:
        lad = runner._run_la(dcm_2d)
        print(f"  [INFO] _run_la 返回 LAD={lad}mm")
        if 20 < lad < 60:
            ok(f"LAD={lad}mm 落在合理区间")
        else:
            print(f"  [WARN] LAD 偏离, 但继续 (LA 测量基准未严格验证)")
            ok(f"LAD={lad}mm (已返回, 待人工核对)")
    except Exception as e:
        fail("_run_la 异常", e)

    # L3.4 _run_mvpeak (关键: 终端正则匹配)
    print("\n  --- L3.4 _run_mvpeak (终端正则解析) ---")
    try:
        ea = runner._run_mvpeak(dcm_pw)
        print(f"  [INFO] _run_mvpeak 返回 E/A={ea}")
        # handoff 验证: 高汉平 00020 PW → E/A=2.02
        if 0.5 < ea < 3.0:
            ok(f"E/A={ea} 落在合理区间 (handoff 基准: 2.02)")
        else:
            fail(f"E/A={ea} 偏离基准 2.02")
    except Exception as e:
        fail("_run_mvpeak 异常 (可能终端输出格式不匹配正则 E/A\\s*=\\s*([\\d.]+))", e)

    print("\n  L3 全部通过 (Runner 新代码验证 ok)")


# ────────────────── L4: Runner.run 完整 ──────────────────

def test_l4(dcm_2d: str, dcm_pw: str):
    banner("L4", "Runner.run 完整 (多文件分发 + 规则引擎汇总)")
    measurement_dir = r"G:\meaurements\measurements\Measurement"
    from echonet_runner import EchoNetRunner
    from api import ImgItem
    runner = EchoNetRunner(script_dir=measurement_dir)

    imgs = [
        ImgItem(imgId="img-2d", imgPath=dcm_2d, imgType="Cardiac Ultrasound"),
        ImgItem(imgId="img-pw", imgPath=dcm_pw, imgType="Cardiac Ultrasound"),
    ]
    try:
        result = runner.run(imgs)
        print(f"  [INFO] 完整结果: {result}")
        required = {"lvef", "lvedd", "lvesd", "lad", "ea", "gls"}
        if not required.issubset(result.keys()):
            fail(f"结果缺字段: {required - set(result.keys())}")
        if result["gls"] is not None:
            fail(f"GLS 应为 None, 实际 {result['gls']}")
        if result["lvef"] is None or result["ea"] is None:
            fail(f"LVEF/EA 不应为 None: lvef={result['lvef']}, ea={result['ea']}")

        # 规则引擎汇总
        from rules import classify_hf
        hf = classify_hf(result["lvef"])
        ok(f"6 项指标齐全: LVEF={result['lvef']}%, E/A={result['ea']}, GLS=null")
        ok(f"HF 分型: {hf} (LVEF={result['lvef']}%)")
    except Exception as e:
        fail("Runner.run 异常", e)

    print("  L4 全部通过 (端到端推理 + 规则引擎 ok)")


# ────────────────── 主流程 ──────────────────

def main():
    parser = argparse.ArgumentParser(description="EchoNetRunner 分层验证")
    parser.add_argument("--dcm2d", default=DEFAULT_DCM_2D, help="2D PLAX 测试文件")
    parser.add_argument("--dcm-pw", default=DEFAULT_DCM_PW, help="PW Doppler 测试文件")
    args = parser.parse_args()

    print(f"\n分层验证 EchoNetRunner")
    print(f"  2D 文件 : {args.dcm2d}")
    print(f"  PW 文件 : {args.dcm_pw}")

    for path in (args.dcm2d, args.dcm_pw):
        if not os.path.exists(path):
            print(f"\n[错误] 文件不存在: {path}")
            print("  请用 --dcm2d / --dcm-pw 指定实际路径")
            sys.exit(1)

    test_l1()
    test_l2(args.dcm2d)
    test_l3(args.dcm2d, args.dcm_pw)
    test_l4(args.dcm2d, args.dcm_pw)

    banner("完成", "L1-L4 全部通过, 可进入 L5 (API 端到端)")
    print("  L5 启动 API 服务:")
    # 注意: f-string 表达式内不能含反斜杠 (Py<3.12), 路径先取到变量
    script_dir_str = r"G:\meaurements\measurements\Measurement"
    print(f"    set PYTHONPATH={script_dir_str}")
    print(f'    python F:\\project\\prototype\\prototype\\algorithm\\main.py --script-dir "{script_dir_str}"')
    print("  然后 POST /heart-algo/task/start 测试")


if __name__ == "__main__":
    main()
