# P0/P1 发布与验证手册

本文覆盖 ECG XML、算法版本、PhysicalDelta 临床校准、模型覆盖和 CPU
连续任务基准。工具生成的报告不包含输入路径、模型结果或患者字段。

## 1. 安装运行依赖

```powershell
conda activate heart
cd D:\project\heart
python -m pip install -r requirements.txt
```

保持服务器既有约束：`numpy<2`、`transformers<5`。不要通过重新解析全部依赖
升级这两个包。

## 2. 补齐并审计模型覆盖

EchoNet-Measurements 的权重存放在 Git LFS 中。普通 clone 可能只有指针文件：

```powershell
cd D:\project\Measurement\Measurement
git lfs install
git lfs pull
```

回到算法服务执行覆盖门禁：

```powershell
cd D:\project\heart
python -m quality.audit_model_coverage `
  --measurement-dir "D:\project\Measurement\Measurement" `
  --samples-root "D:\heart-data\cases\test\dcm" `
  --output "D:\heart-data\validation\model-coverage.json"
```

退出码 `0` 且 `summary.allAssetsReady=true` 表示每个切面同时具备脚本、结构
可识别且不是 Git LFS 指针的权重，以及至少一个具有 Part 10 文件头的测试
DICOM。缺少 AV_Vmax DICOM、任一 2D 权重或脚本都会返回退出码 `1`。
这只是资产门禁，不证明模型推理成功；逐切面真实推理仍必须通过第 5 节基准。

## 3. 生成并设置算法版本

版本生成器对实际模型文件做流式 SHA-256，并把完整哈希写入发布清单。下面的
artifact 列表应包含本次部署实际可能调用的全部权重；服务版本使用当前提交：

```powershell
cd D:\project\heart
$serviceVersion = git rev-parse --short=12 HEAD
$artifactArgs = @(
  "--artifact", "measurement-lvid=D:\project\Measurement\Measurement\weights\2D_models\lvid_weights.ckpt",
  "--artifact", "measurement-ivs=D:\project\Measurement\Measurement\weights\2D_models\ivs_weights.ckpt",
  "--artifact", "measurement-lvpw=D:\project\Measurement\Measurement\weights\2D_models\lvpw_weights.ckpt",
  "--artifact", "measurement-la=D:\project\Measurement\Measurement\weights\2D_models\la_weights.ckpt",
  "--artifact", "measurement-aorta=D:\project\Measurement\Measurement\weights\2D_models\aorta_weights.ckpt",
  "--artifact", "measurement-aortic-root=D:\project\Measurement\Measurement\weights\2D_models\aortic_root_weights.ckpt",
  "--artifact", "measurement-rv-base=D:\project\Measurement\Measurement\weights\2D_models\rv_base_weights.ckpt",
  "--artifact", "measurement-ivc=D:\project\Measurement\Measurement\weights\2D_models\ivc_weights.ckpt",
  "--artifact", "measurement-pa=D:\project\Measurement\Measurement\weights\2D_models\pa_weights.ckpt",
  "--artifact", "measurement-avvmax=D:\project\Measurement\Measurement\weights\Doppler_models\avvmax_weights.ckpt",
  "--artifact", "measurement-trvmax=D:\project\Measurement\Measurement\weights\Doppler_models\trvmax_weights.ckpt",
  "--artifact", "measurement-mrvmax=D:\project\Measurement\Measurement\weights\Doppler_models\mrvmax_weights.ckpt",
  "--artifact", "measurement-lvotvmax=D:\project\Measurement\Measurement\weights\Doppler_models\lvotvmax_weights.ckpt",
  "--artifact", "measurement-medevel=D:\project\Measurement\Measurement\weights\Doppler_models\medevel_weights.ckpt",
  "--artifact", "measurement-latevel=D:\project\Measurement\Measurement\weights\Doppler_models\latevel_weights.ckpt",
  "--artifact", "measurement-mvpeak=D:\project\Measurement\Measurement\weights\Doppler_models\mvpeak_2c_weights.ckpt",
  "--artifact", "measurement-tapse=D:\project\Measurement\Measurement\weights\Doppler_models\tapse_2c_weights.ckpt",
  "--artifact", "ecgfm-mimic17=D:\project\ecg-fm\ecg-fm\weights\mimic_iv_ecg_finetuned.pt"
)
python -m quality.release_identity --service-version $serviceVersion @artifactArgs `
  --output "D:\heart-data\validation\release-manifest.json"
$env:ALGORITHM_VERSION = (
  Get-Content -Raw "D:\heart-data\validation\release-manifest.json" |
  ConvertFrom-Json
).algorithmVersion
```

真实模式未配置 `ALGORITHM_VERSION`、值为 `unknown` 或包含非法字符时会拒绝
启动。版本在应用构建时固定，任务运行期间修改环境变量不会改写该进程的版本。

## 4. PhysicalDelta 临床校准

复制并修改 `docs/examples/physical-delta-manifest.json`。`dcmType` 会进入报告，
用于追溯 LA 等指标采用的切面。每项必须使用模型真实输出坐标和人工复核金
标准；容差由临床负责人确认，工具不提供默认临床阈值。

```powershell
python -m quality.calibrate_physical_delta `
  --dicom "D:\heart-data\cases\test\dcm\plax\sample.dcm" `
  --manifest "D:\heart-data\validation\plax-calibration-input.json" `
  --output "D:\heart-data\validation\plax-calibration-report.json"
```

报告包含 DICOM SHA-256、原始 `(0018,602C)/(0018,602E)` 标尺、物理单位、
模型坐标、换算结果、绝对/相对误差和容差结论。DICOM 读取仅加载元数据，
不加载像素，也不把患者字段或输入路径写入报告。

`axis=x/y/euclidean` 表示两点间距离；Doppler/TDI 只有单个峰值坐标时使用
`absolute_y`，工具会按 Reference Pixel Y、Reference Pixel Physical Value Y
和 PhysicalDeltaY 计算绝对物理值。存在多个重叠 Ultrasound Region 时必须显式
设置 `regionIndex`，工具不会猜测区域。

至少对 LVID、LA、TR_Vmax、TDI 和 TAPSE 建立经医生签字的校准报告。
`summary.failed=0` 只是技术门禁，不能替代临床签字。

## 5. CPU 连续任务稳定性

复制 `docs/examples/cpu-stability-manifest.json`，替换版本和服务器真实输入路径。
建议初次设 `iterations=3`，正式容量基线提高到业务认可的连续任务数。

```powershell
python -m quality.run_cpu_stability `
  --manifest "D:\heart-data\validation\cpu-stability-input.json" `
  --measurement-dir "D:\project\Measurement\Measurement" `
  --measurement-python "C:\Users\lj\miniconda3\envs\heart\python.exe" `
  --measurement-timeout-seconds 900 `
  --ecg-project-dir "D:\project\ecg-fm\ecg-fm" `
  --ecg-checkpoint "D:\project\ecg-fm\ecg-fm\weights\mimic_iv_ecg_finetuned.pt" `
  --ecg-python "C:\Users\lj\miniconda3\envs\heart\python.exe" `
  --ecg-timeout-seconds 900 `
  --work-root "D:\heart-data\runtime\benchmark" `
  --output "D:\heart-data\validation\cpu-stability-report.json"
```

工具强制 `CUDA_VISIBLE_DEVICES` 为空、串行执行场景，并采样主进程及所有子进程
的峰值 RSS。退出码 `0` 表示全部连续任务成功；报告提供整体及逐场景成功率、
P50/P95/最大时延和峰值内存。清单必须同时包含 ECG、心超和混合场景，并设置
`maxRunSeconds`；单图 `error/skipReason`、ECG 无预测或超过资源门槛都会使该次
运行失败。模型覆盖验收时还需加入每个 `dcmType` 的单切面场景并至少运行一轮。

## 6. 发布门禁

- 原始 UTF-8 BOM ECG XML 上传和真实推理成功。
- 新任务 `algorithmVersion` 与发布清单一致，旧任务版本不变。
- 模型覆盖报告 `allAssetsReady=true`，且每个 `dcmType` 的真实 CPU 场景成功。
- 经临床确认的校准报告没有超容差项目，并明确 LA 采用的切面。
- CPU 基准无失败；超时、P95 和峰值内存满足部署容量约束。
- 以上全部满足后，才进入生产鉴权/服务托管及 MCP/Agent 联调。
