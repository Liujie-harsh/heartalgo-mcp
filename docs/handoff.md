# 心衰诊断系统 · 算法侧集成 Handoff 文档

- **更新日期**：2026-08-19
- **上一版交接**：`D:\project\Measurement\Measurement\handoff.md`（本文档为其延续与更新）
- **服务器原始验证记录**：`E:\MyFiles\Desktop\服务器验证记录.txt`（包含运行标识与患者字段，不在本文重复）
- **项目目标**：保持算法服务/MCP 独立，先完成不依赖 Agent Harness 的病例创建、心超/ECG 上传、算法推理、结构化结果、临床复核和持久化可靠闭环，再接入垂域 Agent。
- **实现说明**：第一阶段接口、部署边界和可靠性设计见 `F:\project\prototype\prototype\algorithm_mcp\docs\phase1-reliable-loop.md`，本文只记录服务器交接状态，不重复完整接口契约。

---

## 1. 项目概述与现状

| 项 | 说明 |
|---|---|
| 超声测量模型 | **EchoNet-Measurements**（deeplabv3_resnet50 骨干，2D 线性测量 + Doppler 峰值流速） |
| 心电图模型 | **ECG-FM**（fairseq-signals 框架，wav2vec2 风格 ECG transformer，17 分类微调模型） |
| 前端指标 | LVEF、LVEDD、LVESD、LAD、E/A、GLS 等（详见旧 handoff.md；LVEF 需 Teichholz 公式从 LVID 估算，模型不直接输出） |
| 当前阶段 | **CPU 服务器上的非 Agent 可靠闭环已通过主路径验证**：MySQL、ECG 单模态、TR_Vmax 心超单模态、混合推理、临床批准、服务重启后结果读取均成功 |
| 当前运行模式 | `D:\project\heart`，真实算法、MySQL 任务存储、单 worker、仅监听 `127.0.0.1`、暂时 `--no-mcp` |
| 尚未完成 | XML BOM 服务端兼容、算法版本标识、PhysicalDelta 临床校准、缺失 2D 权重、生产鉴权/网关、MCP 与 Agent Harness 联调 |

---

## 2. 当前服务器环境状态（纯 CPU，重要）

**唯一可用环境：heart（conda）**

```
Python   : C:\Users\lj\miniconda3\envs\heart\python.exe  （Python 3.10.20）
numpy    : 1.26.4   ★ 关键版本（见下方约束）
torch    : 2.4.0+cu121（无 GPU，torch.cuda.is_available() = False，一律 CPU 推理）
torchvision : 0.19.0+cu121
opencv   : 4.8.0.76
pydicom  : 2.4.4
scipy    : 1.15.3
pandas   : 2.3.0
transformers : 4.46.3   ★ 必须 <5（见下方约束）
ecg-transform : 0.1.3
hydra-core / omegaconf / wfdb / scikit-learn / matplotlib / tqdm：已装
```

**两条铁律（踩过的坑，勿动）**：

1. **numpy 必须 <2**（当前 1.26.4）。torch 2.4.0 / opencv 4.8.0.76 / torchvision 0.19.0 均按 numpy 1.x 编译，配 numpy 2.x 会报 `_ARRAY_API not found` / `Numpy is not available`，三个库全部不可用。
2. **transformers 必须 <5**（当前 4.46.3）。5.x（如 5.15.0）要求 torch≥2.5，在 torch 2.4.0 下会禁用 PyTorch 并导致 fairseq_signals 导入 m3ae/bert 时 `NameError: name 'torch' is not defined`。

**说明**：`ecg-transform 0.1.3` 的元数据声明 `numpy>=2.1.3` 是**错误的元数据约束**（其源码未使用任何 numpy 2.x API，已实测在 numpy 1.26.4 下完整管线运行正常）。`pip check` 会提示此冲突，可忽略；若重装需加 `--no-deps`。**不要因此升级 numpy**，否则 torch/opencv 全崩。

服务器数据库状态：应用专用 MySQL 账号已通过 SQLAlchemy 实连验证，当前数据库为 `heart_failure_analytics_dev`。连接串只通过当前 PowerShell 进程的 `DATABASE_URL` 注入；本文不记录用户名或口令。此前口令曾出现在联调记录/会话中，正式部署前必须轮换并清理可访问日志。

当前病例联调为受控的本机回环模式：`CASE_AUTH_REQUIRED=false`、`ALLOW_INSECURE_CASE_API=true`、`127.0.0.1:8000`。这只适合服务器本机 smoke test，不是生产安全配置。

---

## 3. 目录与关键路径

| 用途 | 路径 |
|---|---|
| 算法 API / MCP 代码 | `D:\project\heart` |
| Measurement 代码/权重/脚本 | `D:\project\Measurement\Measurement` |
| Measurement 2D 权重 | `D:\project\Measurement\Measurement\weights\2D_models\{la,lvid}_weights.ckpt` |
| Measurement Doppler 权重 | `...\weights\Doppler_models\{avvmax,latevel,lvotvmax,medevel,mrvmax,mvpeak_2c,tapse_2c,trvmax}_weights.ckpt` |
| ecg-fm 推理脚本（自定义） | `D:\project\ecg-fm\ecg-fm\scripts\infer_quickstart.py` |
| ecg-fm XML→MAT 转换 | `D:\project\ecg-fm\ecg-fm\scripts\xml_to_ecgfm_mat.py` |
| fairseq-signals（源码，非 pip 包） | `D:\project\ecg-fm\ecg-fm\fairseq-signals` |
| ecg 权重（17 分类微调） | `D:\project\ecg-fm\ecg-fm\weights\mimic_iv_ecg_finetuned.pt`（1.08 GB，另有 physionet 预训练版） |
| 标签定义（脚本依赖） | `D:\project\ecg-fm\ecg-fm\ecg-fm\data\mimic_iv_ecg\labels\label_def.csv` |
| 测试数据 | `D:\heart-data\cases\test`（`dcm\{plax,a4c,MV_EA,TR_Vmax,MR_Vmax,LVOT_Vmax,TDI_Medial,TDI_Lateral,TAPSE}\*.dcm` + `ecg\2.xml`） |
| 当前可上传 ECG smoke 文件 | `D:\heart-data\cases\test\ecg\2-no-bom.xml` |
| 任务工作目录 | `D:\heart-data\runtime` |
| 病例文件存储 | `D:\heart-data\cases` |
| 旧交接文档 | `D:\project\Measurement\Measurement\handoff.md` |

**统一路径参数（供 wrapper 使用）**：

```
--measurement-python  C:\Users\lj\miniconda3\envs\heart\python.exe
--ecg-python          C:\Users\lj\miniconda3\envs\heart\python.exe
--ecg-checkpoint      D:\project\ecg-fm\ecg-fm\weights\mimic_iv_ecg_finetuned.pt
```

---

## 4. 本会话已完成的工作

### 4.1 环境诊断与修复
- 定位并修复 torch/opencv/torchvision 与 numpy 2.x 的 ABI 不兼容（降 numpy 至 1.26.4）。
- 定位 transformers 5.15 与 torch 2.4 冲突，降级至 4.46.3，fairseq_signals 导入链恢复。

### 4.2 Measurement：无 GPU 的 `cuda:0` 硬编码补丁（已完成，4 个文件）
把固定 `device = "cuda:0"` 改为 `device = "cuda:0" if torch.cuda.is_available() else "cpu"`：

| 文件 | 修改行 |
|---|---|
| `inference_2D_image.py` | L135 |
| `inference_Doppler_image.py` | L106 |
| `inference_MV_EperA.py` | L91 |
| `inference_TAPSE.py` | L90 |

（`gradio_demo.py` 未改，非 CLI 推理路径。）

### 4.3 ecg-fm：`infer_quickstart.py` 路径硬编码修复（已完成，3 处）
原脚本假设目录布局与实际不符（仓库嵌套在 `ecg-fm\ecg-fm\ecg-fm` 下），导致 3 处路径错误：

| 位置 | 原问题 | 修复 |
|---|---|---|
| fairseq-signals 路径 | 指向 `WORK_ROOT/fairseq-signals`（不存在），需手动 PYTHONPATH | 探测 `PROJECT_ROOT/fairseq-signals` 与 `WORK_ROOT/fairseq-signals`，取存在者；**无需再设 PYTHONPATH** |
| `--checkpoint` 默认值 | 指向 `WORK_ROOT/weights/...`（不存在） | 探测 3 个候选路径，取首个存在的 |
| `label_def.csv` | 指向 `PROJECT_ROOT/data/...`（不存在）→ FileNotFoundError | 探测 `PROJECT_ROOT/ecg-fm/data/...` 与 `PROJECT_ROOT/data/...`；找不到时报清晰错误 |

修改文件：`D:\project\ecg-fm\ecg-fm\scripts\infer_quickstart.py`

### 4.4 第一阶段非 Agent 可靠闭环（已完成实现）

`D:\project\heart` 已提供病例创建、DICOM/ECG XML 上传、异步算法任务、结构化结果查询、临床复核、MySQL 任务持久化和文件病例存储。HTTP 病例接口与独立 MCP 复用同一任务队列和 runner；当前服务器联调刻意使用 `--no-mcp`，先验证算法闭环。

本地仓库对应提交：

- `925a7d6 feat: add reliable heart diagnosis case workflow`
- `3c1464f fix: harden reliable diagnosis workflow`

实现测试基线为 `164 passed, 7 skipped`。详细端点、幂等、鉴权、锁和恢复边界见 `docs\phase1-reliable-loop.md`。

---

## 5. 验证结果

### 5.1 Measurement 2D（✅ 已通，用户确认）
- LA 模型 + `D:\heart-data\cases\test\dcm\a4c\00007_f740c695836ad433.dcm` 正常推理（输出标注视频 + 坐标 CSV）。
- 判据：出现 `[Model] <All keys matched successfully>`（权重与 deeplabv3_resnet50(num_classes=2) 完全匹配）。

### 5.2 ecg-fm（✅ 已通，本会话端到端实测）
```
Loading model: D:\project\ecg-fm\ecg-fm\weights\mimic_iv_ecg_finetuned.pt
Processed 1 MAT file(s), 1 five-second segment(s).
→ predictions_5s_segments.csv / predictions_aggregated.csv（17 标签概率，窦性心律 0.86 等）
```
- 输入 `2.xml`（HL7 aECG）→ `xml_to_ecgfm_mat.py` 转 MAT → `infer_quickstart.py` 推理。

### 5.3 Measurement/Doppler wrapper（✅ 主路径已通，仍有模型域问题）

- 已通过：`TR_Vmax=64.12 cm/s`、`TAPSE=18.7`、`TDI_Medial=77.99`、`TDI_Lateral=8.48`、`MR_Vmax=81.13`、`LVOT_Vmax=25.2`。
- `MV_EA` 未通过：测试图像 `y0=227`，而当前模型只支持约 `340–350` 的输入区域；wrapper 正确返回“心超图像超出模型支持范围”。这属于样本/模型支持域问题，不是服务编排故障。
- `PLAX` 部分通过：已产出 `LVEF=27.42`、`LVEDD=66.54`、`LVESD=57.81`，随后因缺少 `ivs_weights.ckpt` 失败；CPU 单次约 379 秒。补齐权重和校准前不可视为完整 PLAX 验证。
- `AV_Vmax` 权重存在，但仍缺合适测试 DICOM。

### 5.4 真实算法服务闭环（✅ 已通）

服务器日志确认以下请求均走真实 runner、MySQL 任务存储和病例文件存储，未使用 fake runner：

| 场景 | 结果 | 关键证据 |
|---|---|---|
| ECG 单模态 | ✅ completed | `2-no-bom.xml` 上传 201、诊断 202；返回测量值、前 5 个分类概率，`error=null` |
| TR_Vmax 心超单模态 | ✅ completed | 返回 `tr_vmax=64.12 cm/s`，`error=null`，ECG 列表为空 |
| TR_Vmax + ECG 混合任务 | ✅ completed | 同一报告同时包含一项心超和一项 ECG；两项输入均有 SHA-256 与大小追溯信息 |
| 临床复核 | ✅ approved | 批准后 `reviewStatus=approved`、`requiresClinicianReview=false` |
| 服务重启后查询 | ✅ completed | 以原病例用户查询旧 `caseId/taskId`，仍返回完整混合结果和批准记录 |

原始任务 ID、资产 ID、哈希和患者字段保留在服务器验证记录中；本文按交接最小化原则不复制。ECG-only 的 `hfType=null`，心超/混合任务的 `hfType=未知`，说明编排成功，但心衰分型规则尚未得到临床验收。

### 5.5 当前阶段结论

第一阶段的主路径验收已经从“两个算法分别能运行”推进到“上传—排队—真实 CPU 推理—结构化报告—医生复核—重启后读取”的完整非 Agent 闭环。下一阶段应先处理临床可信度和生产边界，再开启 MCP/Agent 接入。

---

## 6. 推理命令（复现用）

```powershell
# ── 0. 环境（每次新开终端）──
conda activate heart

# ── 1. Measurement 2D ──
cd D:\project\Measurement\Measurement
python inference_2D_image.py --model_weights la  --file_path "D:\heart-data\cases\test\dcm\a4c\00007_f740c695836ad433.dcm"  --output_path "D:\heart-data\cases\test\output\la_a4c_00007.avi"
python inference_2D_image.py --model_weights lvid --file_path "D:\heart-data\cases\test\dcm\plax\00003_2dbbf05f7a19120f.dcm" --output_path "D:\heart-data\cases\test\output\lvid_plax_00003.avi" --phase_estimate

# ── 2. Measurement Doppler ──
cd D:\project\Measurement\Measurement
python inference_Doppler_image.py --model_weights trvmax   --file_path "D:\heart-data\cases\test\dcm\TR_Vmax\00006_4ca03022ca3e030f.dcm"   --output_path "D:\heart-data\cases\test\output\trvmax.jpg"
python inference_Doppler_image.py --model_weights mrvmax   --file_path "D:\heart-data\cases\test\dcm\MR_Vmax\00016_8b3fd32111ce8df6.dcm"   --output_path "D:\heart-data\cases\test\output\mrvmax.jpg"
python inference_Doppler_image.py --model_weights lvotvmax --file_path "D:\heart-data\cases\test\dcm\LVOT_Vmax\00021_921554d88b862669.dcm" --output_path "D:\heart-data\cases\test\output\lvotvmax.jpg"
python inference_Doppler_image.py --model_weights latevel  --file_path "D:\heart-data\cases\test\dcm\TDI_Lateral\00012_d1e1c586cd483e7e.dcm" --output_path "D:\heart-data\cases\test\output\latevel.jpg"
python inference_Doppler_image.py --model_weights medevel  --file_path "D:\heart-data\cases\test\dcm\TDI_Medial\00005_3e640d9a052f89a8.dcm"  --output_path "D:\heart-data\cases\test\output\medevel.jpg"
python inference_MV_EperA.py --file_path "D:\heart-data\cases\test\dcm\MV_EA\00008_e666e00ba5bb3eb2.dcm" --output_path "D:\heart-data\cases\test\output\mvea.jpg"
python inference_TAPSE.py   --file_path "D:\heart-data\cases\test\dcm\TAPSE\00013_7cebcdae2ebaf95a.dcm"   --output_path "D:\heart-data\cases\test\output\tapse.jpg"

# ── 3. ecg-fm（修复后无需 PYTHONPATH）──
cd D:\project\ecg-fm\ecg-fm
python scripts\xml_to_ecgfm_mat.py "D:\heart-data\cases\test\ecg\2.xml" "D:\heart-data\cases\test\ecg\2.mat"
python scripts\infer_quickstart.py "D:\heart-data\cases\test\ecg\2.mat" `
  --device cpu `
  --checkpoint "D:\project\ecg-fm\ecg-fm\weights\mimic_iv_ecg_finetuned.pt" `
  --output-dir "D:\heart-data\cases\test\ecg\out"
```

### 6.4 当前真实服务启动基线

每个新 PowerShell 都需重新激活环境和注入 `DATABASE_URL`。数据库口令必须从安全配置获取，不要写入脚本或本文。

```powershell
conda activate heart
cd D:\project\heart

$env:CASE_AUTH_REQUIRED = "false"
$env:ALLOW_INSECURE_CASE_API = "true"
$env:PYTHON_QUEUE_WORKERS = "1"
$env:PYTHON_GPU_IDS = "0"

& "C:\Users\lj\miniconda3\envs\heart\python.exe" ".\main.py" `
  --measurement-script-dir "D:\project\Measurement\Measurement" `
  --measurement-python "C:\Users\lj\miniconda3\envs\heart\python.exe" `
  --ecg-project-dir "D:\project\ecg-fm\ecg-fm" `
  --ecg-checkpoint "D:\project\ecg-fm\ecg-fm\weights\mimic_iv_ecg_finetuned.pt" `
  --ecg-python "C:\Users\lj\miniconda3\envs\heart\python.exe" `
  --ecg-timeout-seconds 900 `
  --task-work-root "D:\heart-data\runtime" `
  --task-store mysql `
  --case-storage-root "D:\heart-data\cases" `
  --no-mcp `
  --host 127.0.0.1 `
  --port 8000
```

虽然环境变量名为 `PYTHON_GPU_IDS`，当前值 `0` 只充当单资源槽/串行队列标识；两个 wrapper 会因 `torch.cuda.is_available()=False` 实际选择 CPU。不要配置多个 worker；当前文件病例存储只支持单实例、单 Uvicorn worker，共享目录由实例锁保护。

---

## 7. 临床映射（dcmType → 指标 → 模型/权重）

按 dcmType 路由推理是既定方案。本地权重覆盖情况：

| dcmType | 指标 | 脚本 / 权重 | 本地权重 |
|---|---|---|---|
| PLAX (B-Mode) | LVEDD、LVESD、LVEF、IVS、LVPW、LA、Aorta、AorticRoot | `inference_2D_image.py` | **仅 lvid**（→LVEDD/LVESD；LVEF 用 Teichholz 公式） |
| A4C (B-Mode) | RVBase | 同上 | ❌ 缺 rv_base |
| Subcostal (B-Mode) | IVC | 同上 | ❌ 缺 ivc |
| RVOT (B-Mode) | PA | 同上 | ❌ 缺 pa |
| MV_EA (Doppler) | MV E峰、A峰、E/A | `inference_MV_EperA.py` | ✅ mvpeak_2c |
| AV_Vmax (Doppler) | 主动脉瓣峰值流速 | `inference_Doppler_image.py --model_weights avvmax` | ✅（无测试 DICOM） |
| TR_Vmax (Doppler) | 三尖瓣反流峰值流速 | 同上 `trvmax` | ✅ |
| MR_Vmax (Doppler) | 二尖瓣反流峰值流速 | 同上 `mrvmax` | ✅ |
| LVOT_Vmax (Doppler) | 左室流出道峰值流速 | 同上 `lvotvmax` | ✅ |
| TDI_Medial (TDI) | 二尖瓣环间隔侧 e' | 同上 `medevel` | ✅ |
| TDI_Lateral (TDI) | 二尖瓣环侧壁 e' | 同上 `latevel` | ✅ |
| TAPSE (M-Mode) | TAPSE | `inference_TAPSE.py` | ✅ tapse_2c |

**注意**：
- 2D 模型输入要求 3:4（480×640）；DICOM 输入脚本自动 resize 并校验比例。
- Doppler/TDI/M-Mode 均要求 DICOM（依赖 PhysicalDelta 标尺 tag 换算 cm/s）。
- 测试数据目录与映射一一对应（plax→lvid、MV_EA→mvpeak_2c、TR/MR/LVOT_Vmax、TDI_Medial/Lateral、TAPSE），缺 `AV_Vmax` 目录。

---

## 8. 已知问题与坑

1. **numpy <2 与 transformers <5 是硬约束**（见第 2 节），装任何新包前先 `pip check`，别让 pip 自动升级这两个。
2. **PhysicalDelta 标尺问题**（旧 handoff 记录，未解决）：`0x0018,0x602C/602E` 标签值错误曾导致 LVID≈0.39cm、LAD≈0.10cm（应 3.5–5.8 / 2.7–3.8 cm），偏差约 4 倍且 Y 向为 0。**推理跑通 ≠ 数值可信**，上报告前必须核对标尺换算。
3. **LA 切面待定**：临床 LA 前后径标准在 PLAX；测试数据 `dcm\a4c` 曾用于 la 也能跑通。建议两个切面各跑一例，按换算后数值合理性（LA 2.7–3.8 cm）定夺。
4. **缺失的 2D 权重**（ivs/lvpw/la/aorta/aortic_root/rv_base/pa/ivc）：本地 `weights\2D_models` 仅有 la、lvid，其余需从官方仓库 `echonet/measurements` release 下载。
5. **LVEF**：模型不输出，需 Teichholz 公式：EDV=7D³/(2.4+D)，LVEF=(EDV−ESV)/EDV×100（D 取 LVEDD/LVESD）。
6. **服务器纯 CPU**：没有可用 GPU，2D/混合推理慢是预期行为；`PLAX` 实测约 379 秒。保持 `PYTHON_QUEUE_WORKERS=1`，容量/超时评估必须以 CPU 数据为准。
7. **ECG XML BOM**：原始 `2.xml` 以 UTF-8 BOM（`EF BB BF`）开头，当前上传校验误判为“不是 XML”。联调通过生成无 BOM 的 `2-no-bom.xml` 绕过；长期修复应让服务端安全接受 UTF-8 BOM，并用测试覆盖，不能要求临床来源修改原文件。
8. **算法版本缺失**：已完成结果均显示 `algorithmVersion=unknown`。下次启动前应把 `ALGORITHM_VERSION` 设置为真实发布 tag/commit/build ID，再创建新任务验证；旧结果按设计不会被新版本覆盖。
9. **当前鉴权仅供 smoke test**：`CASE_AUTH_REQUIRED=false` 和 `ALLOW_INSECURE_CASE_API=true` 不能用于生产，也不能把端口直接暴露到外网。生产需受信网关剥离/重写身份头、注入代理密钥，并恢复临床复核角色约束。
10. **PowerShell 中文显示**：`Invoke-RestMethod` 的控制台表格曾把“未知”显示为乱码，但浏览器 JSON 正常。这更像控制台编码/格式化问题；需要区分显示乱码与服务端 JSON 编码错误。
11. **患者数据与日志**：结构化 ECG 结果含年龄、性别和患者标识。后续日志、截图、Agent 上下文和问题单必须默认脱敏；算法输入/输出不能直接发送给通用 LLM。
12. **ecg 输入**：需 HL7 aECG XML（12 导联完整，长节律 ≥5000 点）经 `xml_to_ecgfm_mat.py` 转 MAT；`feats` 形状 (12, N)、`org_sample_rate` 字段。

---

## 9. 待办事项

- [x] 验证应用专用 MySQL 账号和 `heart_failure_analytics_dev` 连接。
- [x] 完成真实服务 ECG 单模态闭环。
- [x] 完成真实服务 TR_Vmax 心超单模态闭环。
- [x] 完成真实服务 TR_Vmax + ECG 混合闭环。
- [x] 完成临床批准记录和服务重启后的持久化读取验证。
- [ ] 设置真实 `ALGORITHM_VERSION`，新建任务并确认结果不再为 `unknown`。
- [ ] 服务端兼容 UTF-8 BOM ECG XML，并补上传回归测试；保留内容类型与 XML 安全校验。
- [ ] 核对 PhysicalDelta 标尺问题，修正并以人工测量金标准验证 cm/cm·s⁻¹ 换算（临床上线阻塞项）。
- [ ] 获取 AV_Vmax 测试 DICOM，验证 `avvmax` 权重。
- [ ] 下载缺失 2D 权重（ivs/lvpw/la/aorta/aortic_root/rv_base/pa/ivc）并纳入验证。
- [ ] 确定 LA 切面口径（PLAX vs A4C）。
- [ ] 确认切面路由实现：用 DICOM `ViewName`/`ProtocolName` 标签自动识别 dcmType。
- [ ] 对 ECG/心超/混合推理做 CPU 耗时、峰值内存、超时和连续任务稳定性测试。
- [ ] 设计生产网关、TLS、`CASE_TRUSTED_PROXY_SECRET`、临床 reviewer 角色与最小权限 MCP 服务账号；轮换已暴露数据库口令。
- [ ] 把 MySQL、`D:\heart-data\cases` 和 `D:\heart-data\runtime` 纳入一致的备份、保留期、访问审计和恢复演练。
- [ ] 完成生产服务托管（Windows Service/NSSM 或等价方案）、健康检查、日志轮转和启动失败告警。
- [ ] 在以上生产边界满足后启用独立 MCP，再与 Agent Harness 做最小权限联调；Agent 只传 `caseId/assetId`，不直接访问算法服务器磁盘路径。
- [ ] 心衰语料库/批量病例（`D:\project\心衰语料库`）接入批量推理。

---

## 10. 建议下一会话使用的技能

- `diagnosing-bugs`：定位 XML BOM 校验、PowerShell 中文显示或真实服务异常；先建立最小复现和证据链。
- `tdd`：修复 BOM 上传兼容时先增加失败测试，再修改 `case_store.py`，覆盖 BOM、空白、伪 XML 与 DICOM 头校验。
- `implement`：在明确发布标识、鉴权网关或服务托管需求后实施变更，并保持现有可靠闭环测试通过。
- `review`：生产部署前从规格与工程标准两条线复核鉴权、路径脱敏、单实例约束、持久化和临床复核权限。

---

## 11. 参考链接

- EchoNet-Measurements 官方仓库：<https://github.com/echonet/measurements>
- 论文：Sahashi Y, et al. *Artificial Intelligence Automation of Echocardiographic Measurements.* JACC. 2025;86(13):964-978. <https://www.jacc.org/doi/10.1016/j.jacc.2025.07.053>
- ECG-FM 官方仓库：<https://github.com/bowang-lab/ECG-FM>（权重托管于 HuggingFace `wanglab/ecg-fm`）
- fairseq-signals：<https://github.com/Jwoo5/fairseq-signals>
- 旧交接文档：`D:\project\Measurement\Measurement\handoff.md`
