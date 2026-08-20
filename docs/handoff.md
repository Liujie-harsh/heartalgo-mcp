# 心衰诊断系统 · 算法侧集成 Handoff 文档

- **更新日期**：2026-08-20
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
| 当前阶段 | **非 Agent 可靠闭环主路径已通过；PLAX 修复已部署，新的文件传输发布清单已生成，但运行中的服务仍加载旧算法版本**。原始 BOM ECG 功能冒烟通过；正常 PLAX 六模型最终完成但约耗时 45 分钟，AorticRoot 为当前性能瓶颈；资产审计仍为 9/12 条件通过 |
| 当前运行模式 | 代码通过文件传输部署到 `D:\project\heart`；真实算法、MySQL 任务存储、单 worker、仅监听 `127.0.0.1`、联调阶段暂时 `--no-mcp` |
| 尚未完成 | 从新清单正确注入版本并重启服务、验证新任务记录新版本、失败样本 `00005...dcm` 回归、PLAX 性能治理、DTD/实体及伪 XML 拒绝、PhysicalDelta 临床校准、9 切面条件 CPU 稳定性门禁、3 个豁免切面样本补齐、生产鉴权/网关、MCP 与 Agent Harness 运行时联调 |

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

**说明**：`ecg-transform 0.1.3` 的元数据声明 `numpy>=2.1.3`，因此 `pip check` 当前仍返回依赖冲突；但服务器已在 numpy 1.26.4 下用真实权重完成 ECG 直接推理。联调期间不要为消除元数据提示而直接升级 numpy；生产前仍需通过兼容版本、移除未使用依赖或拆分环境正式消除冲突。

服务器数据库状态：应用专用 MySQL 账号已通过 SQLAlchemy 实连验证，当前数据库为 `heart_failure_analytics_dev`。连接串只通过当前 PowerShell 进程的 `DATABASE_URL` 注入；本文不记录口令。此前口令曾出现在联调记录/会话中，正式部署前必须轮换并清理可访问日志。

当前病例联调为受控的本机回环模式：`CASE_AUTH_REQUIRED=false`、`ALLOW_INSECURE_CASE_API=true`、`127.0.0.1:8000`。这只适合服务器本机 smoke test，不是生产安全配置。

---

## 3. 目录与关键路径

| 用途 | 路径 |
|---|---|
| 算法 API / MCP 代码 | `D:\project\heart` |
| Measurement 代码/权重/脚本 | `D:\project\Measurement\Measurement` |
| Measurement 2D 权重 | `D:\project\Measurement\Measurement\weights\2D_models\`，9 个权重（aorta、aortic_root、ivc、ivs、la、lvid、lvpw、pa、rv_base）均已存在并通过结构审计 |
| Measurement Doppler 权重 | `...\weights\Doppler_models\{avvmax,latevel,lvotvmax,medevel,mrvmax,mvpeak_2c,tapse_2c,trvmax}_weights.ckpt` |
| ecg-fm 推理脚本（自定义） | `D:\project\ecg-fm\ecg-fm\scripts\infer_quickstart.py` |
| ecg-fm XML→MAT 转换 | `D:\project\ecg-fm\ecg-fm\scripts\xml_to_ecgfm_mat.py` |
| fairseq-signals（源码，非 pip 包） | `D:\project\ecg-fm\ecg-fm\fairseq-signals` |
| ecg 权重（17 分类微调） | `D:\project\ecg-fm\ecg-fm\weights\mimic_iv_ecg_finetuned.pt`（1.08 GB，另有 physionet 预训练版） |
| 标签定义（脚本依赖） | `D:\project\ecg-fm\ecg-fm\ecg-fm\data\mimic_iv_ecg\labels\label_def.csv` |
| 测试数据 | `D:\heart-data\cases\test`（`dcm\{plax,a4c,MV_EA,TR_Vmax,MR_Vmax,LVOT_Vmax,TDI_Medial,TDI_Lateral,TAPSE}\*.dcm` + `ecg\2.xml`） |
| 当前可上传 ECG smoke 文件 | `D:\heart-data\cases\test\ecg\2.xml`（原始 UTF-8 BOM，服务器功能冒烟已通过）；`2-no-bom.xml` 保留为历史对照 |
| 任务工作目录 | `D:\heart-data\runtime` |
| 病例文件存储 | `D:\heart-data\cases` |
| P0/P1 验证报告 | `D:\heart-data\validation`（含模型覆盖报告与 `release-manifest.json`） |
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
- `8f81cc4 feat: add P0 P1 reliability validation gates`
- `f738d3c fix: harden PLAX CPU inference diagnostics`
- `a170004 fix: address PLAX acceptance review`

PLAX 修复本地完整基线为 `193 passed, 7 skipped`；服务器验收清单已将完整部署、修复标识和回归测试标记完成，原始摘要应保留在 `D:\heart-data\validation\pytest-summary.txt`。详细端点、幂等、鉴权、锁和恢复边界见 `docs\phase1-reliable-loop.md`；P0/P1 发布与验证命令见 `docs\p0-p1-validation.md`。

### 4.5 P0/P1 可靠性门禁（工程实现已完成并部署）

- ECG XML 上传已实现 UTF-8 BOM 兼容、完整流式 XML 解析及 DTD/实体拒绝。
- 真实模式启动时冻结 `ALGORITHM_VERSION`，任务结果记录固定版本。
- 新增模型 artifact SHA-256 发布清单、模型覆盖审计、PhysicalDelta 临床标定和串行 CPU 稳定性工具。
- Measurement 子任务增加显式超时；CPU 基准采集进程树峰值 RSS，并校验单图模型结果。
- 服务器代码不是通过 Git 部署，而是文件传输；历史代码指纹为 `bundle-2066fea65c13`，本次部署后对 30 个运行/发布文件重新计算得到 `bundle-f06f5f050c98`。
- 18 个模型 artifact 已重新读取并完成内容哈希；新清单候选版本为 `heart@bundle-f06f5f050c98+models@4bf76612484dd393`。旧版本 `heart@bundle-2066fea65c13+models@feb35a11fa0e0bcb` 只应保留给历史任务。
- 发布清单位于 `D:\heart-data\validation\release-manifest.json`；每次新开 PowerShell 或启动服务都需从该文件重新注入 `ALGORITHM_VERSION`。
- 当前存在关键证据冲突：生成清单的 PowerShell 已显示新版本，但实际服务启动日志仍打印旧版本，说明环境变量没有进入启动服务的那个进程。新版本身份门禁尚未通过。

### 4.6 2026-08-20 PLAX 慢任务与错误诊断（修复已部署，性能待治理）

- 最新附件中的 `return_code=3221225786 (0xC000013A)` 和 `KeyboardInterrupt` 是服务被 Ctrl+C 停止后中断仍在逐帧推理的 PyTorch 子进程，不是模型自行崩溃。
- 纯 CPU PLAX 会顺序运行 LVID、IVS、LVPW、LA、Aorta、AorticRoot 六个模型；历史整例约 379 秒，页面长期显示 `processing` 属于当前容量边界，验收时不得中途停止服务。
- 另一个 `systolic_i[0]` 的 `IndexError` 已用真实失败样本距离曲线复现：固定 `distance=25` 后首尾极值清理会使收缩峰数组为空。
- 算法服务修复已部署：LVID 不再传入服务未消费的 `--phase_estimate`；Windows Ctrl+C 返回码映射为明确的“服务停止导致中断”；每个 Measurement 子任务记录 `duration_seconds`；门户显示已等待秒数和 PLAX CPU 提示。
- 正常 PLAX 样本的 LVID、IVS、LVPW、LA、Aorta 均正常完成；AorticRoot 阶段长时间无进展，任务最终约 45 分钟完成。该结果约为历史 379 秒基线的 7 倍，不能作为正常容量基线。
- 性能观察时误采集了 Uvicorn 主服务进程而非 `inference_2D_image.py --model_weights aortic_root` 子进程，因此已有 `CPU/RSS` 数值不能用于判断 AorticRoot 是否满负载或卡死；后续必须采集实际子进程 PID、逐模型耗时和进程树峰值 RSS。
- 下午逐项验收使用 `docs\server-validation-checklist.md`，未取得服务器、临床或真实样本证据的项目不得标记通过。

### 4.7 DeepSeek Harness 插件进度

- `deepseek-harness-plugin` 已形成可独立安装的 `dsh.bundle.patch`，通过 `@deepseek-ai/dsh-mcp-client` 将 `/mcp` Streamable HTTP 端点注册为原生工具；插件不复制算法逻辑，也不直接接收服务器文件路径。
- 已实现环境变量注入的 MCP URL/Bearer Token、60 秒短调用超时、异步提交/查询契约、最小权限 `caseId/assetId` 边界、安装/卸载及排错文档。相关提交为 `6171890`、`65fd38a`、`c3ad655`。
- 本地验证：`npm run check` 通过；`python -m pytest -q test/test_deepseek_harness_plugin.py` 为 `4 passed`；使用临时 npm cache 的 `npm pack --dry-run` 成功，包内仅 4 个声明文件，无凭据写入。
- 当前运行时尚未联调：本机 Node.js 为 `20.11.1`，低于插件声明的 `>=20.12.0`；尚未在真实 DeepSeek Harness profile 执行 `dsh plugin add` / `dsh --dump-config`；算法服务仍以 `--no-mcp` 启动。生产安全和临床门禁通过前不得提前启用真实 MCP。

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
- `PLAX` 历史运行部分通过：曾产出 `LVEF=27.42`、`LVEDD=66.54`、`LVESD=57.81`，当时随后因缺少 `ivs_weights.ckpt` 失败，CPU 单次约 379 秒。相关权重现已补齐。
- 2026-08-20 第一次 PLAX 门户任务在逐帧 LVID 推理期间被人工停止，附件中的 `0xC000013A/KeyboardInterrupt` 因此不能记为模型自行失败。
- 修复部署后的正常 PLAX 样本已依次完成 LVID、IVS、LVPW、LA、Aorta、AorticRoot，最终总耗时约 45 分钟；AorticRoot 是现场观察到的长尾阶段。功能路径完成，但性能门禁未通过，且仍需保存六个精确 `duration_seconds` 和临床校准证据。
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
| 历史任务版本不可变 | ✅ 保持不变 | 重启前后同一已完成且批准的旧任务均为 `heart@bundle-2066fea65c13+models@feb35a11fa0e0bcb` |
| 原始 BOM ECG 功能冒烟 | ✅ completed | 原始 `2.xml` 上传和真实推理完成；但运行服务仍标记旧算法版本，不能据此勾选“新版本任务”门禁 |

原始任务 ID、资产 ID、哈希和患者字段保留在服务器验证记录中；本文按交接最小化原则不复制。ECG-only 的 `hfType=null`，心超/混合任务的 `hfType=未知`，说明编排成功，但心衰分型规则尚未得到临床验收。

### 5.5 P0/P1 服务器门禁进度（🟡 条件通过，仍有阻塞项）

- 依赖安装完成；服务器验收清单已将本次完整部署、修复标识和回归测试标记完成；`psutil 7.0.0` 与 `pydicom 2.4.4` 已安装。
- ECG-FM 使用真实 MAT 和微调权重在 CPU 上成功处理 1 个文件/1 个五秒片段，并产出分段及聚合预测 CSV。
- Measurement 所有脚本存在；9 个 2D 权重和 8 个 Doppler/TAPSE 权重均存在，无 Git LFS 指针、无结构无效权重。
- 模型资产审计当前为 `assetReadyViews=9/12`、`allAssetsReady=false`、`missingWeights=[]`。唯一缺口是 `Subcostal`、`RVOT`、`AV_Vmax` 没有有效测试 DICOM。
- 本轮决定对上述 3 个切面做临时联调豁免；只能形成“9/12 条件验收”，不得描述为全切面生产通过。当前严格门禁仍会返回非零，后续需部署显式 waiver 能力或补齐真实样本。
- 新文件传输部署的源代码指纹生成成功：`serviceVersion=bundle-f06f5f050c98`；30 个运行/发布文件清单应保存在 `D:\heart-data\validation\service-bundle-manifest.json`。
- 18 个模型 artifact 参数校验和内容哈希完成，新发布清单生成 `heart@bundle-f06f5f050c98+models@4bf76612484dd393`。
- 实际服务启动日志仍显示旧版本 `heart@bundle-2066fea65c13+models@feb35a11fa0e0bcb`。根因是 PowerShell 环境变量只在当前进程生效；生成清单和启动服务不在同一个已正确重载环境变量的进程中。必须重启并以启动日志为最终证据。
- 原始 BOM ECG 功能冒烟已完成；DTD/实体和非 XML 的 422 拒绝尚未现场执行。经医生确认的 PhysicalDelta 报告、9 切面 CPU 连续任务报告仍未完成。

### 5.6 PLAX 正常样本性能实测

- 正常 PLAX 单图最终完成六模型推理，总耗时约 45 分钟；现场曾在 `processing` 状态观察到已等待 1399 秒。
- 运行顺序为 LVID → IVS → LVPW → LA → Aorta → AorticRoot；前五项正常完成，最后的 AorticRoot 出现明显长尾。
- 当时活动推理子进程命令明确包含 `--model_weights aortic_root`；但性能采样使用的是 Uvicorn 主进程 PID，所得约 65 MB RSS 和累计 CPU 不能代表模型子进程。
- 当前结论：PLAX 功能回归可记为“正常样本 completed”，性能只能记为 `FAIL/待治理`；不能把 45 分钟当作正常纯 CPU 基线。失败样本 `00005_851deaabbf3089ae.dcm` 尚未回归。
- 下一轮需直接采集活动 `inference_2D_image.py` 子进程，保存六个逐模型耗时、任务总耗时、进程树峰值 RSS、DICOM 帧数/分辨率，并对 AorticRoot 做单模型 CPU 基准；随后再决定 GPU 迁移。GPU 迁移不能替代当前长尾定位。

### 5.7 当前阶段结论

第一阶段已完成“上传—排队—真实 CPU 推理—结构化报告—医生复核—重启后读取”的非 Agent 闭环；PLAX 修复代码和新发布清单已落到服务器，但运行进程仍使用旧版本标签。下一步必须先用新清单重启并验证新任务版本，再回归 `00005...dcm`、定位 AorticRoot 长尾和完成 PhysicalDelta/9 切面 CPU 门禁。DeepSeek Harness 插件工程包已自检通过，但只有生产安全、临床门禁和 MCP 运行时配置满足后才能进入真实联调。

---

## 6. 推理命令（复现用）

```powershell
# ── 0. 环境（每次新开终端）──
conda activate heart

# ── 1. Measurement 2D ──
cd D:\project\Measurement\Measurement
python inference_2D_image.py --model_weights la  --file_path "D:\heart-data\cases\test\dcm\a4c\00007_f740c695836ad433.dcm"  --output_path "D:\heart-data\cases\test\output\la_a4c_00007.avi"
python inference_2D_image.py --model_weights lvid --file_path "D:\heart-data\cases\test\dcm\plax\00003_2dbbf05f7a19120f.dcm" --output_path "D:\heart-data\cases\test\output\lvid_plax_00003.avi"

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

$encodedPassword = & "C:\Users\lj\miniconda3\envs\heart\python.exe" -c "from getpass import getpass; from urllib.parse import quote_plus; print(quote_plus(getpass('DB password: ')))"

$env:DATABASE_URL = "mysql+pymysql://heart_algo_app:${encodedPassword}@127.0.0.1:3306/heart_failure_analytics_dev?charset=utf8mb4"

$env:CASE_AUTH_REQUIRED = "false"
$env:ALLOW_INSECURE_CASE_API = "true"
$env:PYTHON_QUEUE_WORKERS = "1"
$env:PYTHON_GPU_IDS = "0"
$env:ALGORITHM_VERSION = (Get-Content -Raw "D:\heart-data\validation\release-manifest.json" | ConvertFrom-Json).algorithmVersion

if ($env:ALGORITHM_VERSION -ne "heart@bundle-f06f5f050c98+models@4bf76612484dd393") {
    throw "启动版本与本次发布清单不一致：$env:ALGORITHM_VERSION"
}

& "C:\Users\lj\miniconda3\envs\heart\python.exe" ".\main.py" `
  --measurement-script-dir "D:\project\Measurement\Measurement" `
  --measurement-python "C:\Users\lj\miniconda3\envs\heart\python.exe" `
  --measurement-timeout-seconds 900 `
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
| PLAX (B-Mode) | LVEDD、LVESD、LVEF、IVS、LVPW、LA、Aorta、AorticRoot | `inference_2D_image.py` | ✅ lvid、ivs、lvpw、la、aorta、aortic_root |
| A4C (B-Mode) | RVBase | 同上 | ✅ rv_base |
| Subcostal (B-Mode) | IVC | 同上 | ✅ ivc；无测试 DICOM，临时豁免 |
| RVOT (B-Mode) | PA | 同上 | ✅ pa；无测试 DICOM，临时豁免 |
| MV_EA (Doppler) | MV E峰、A峰、E/A | `inference_MV_EperA.py` | ✅ mvpeak_2c |
| AV_Vmax (Doppler) | 主动脉瓣峰值流速 | `inference_Doppler_image.py --model_weights avvmax` | ✅；无测试 DICOM，临时豁免 |
| TR_Vmax (Doppler) | 三尖瓣反流峰值流速 | 同上 `trvmax` | ✅ |
| MR_Vmax (Doppler) | 二尖瓣反流峰值流速 | 同上 `mrvmax` | ✅ |
| LVOT_Vmax (Doppler) | 左室流出道峰值流速 | 同上 `lvotvmax` | ✅ |
| TDI_Medial (TDI) | 二尖瓣环间隔侧 e' | 同上 `medevel` | ✅ |
| TDI_Lateral (TDI) | 二尖瓣环侧壁 e' | 同上 `latevel` | ✅ |
| TAPSE (M-Mode) | TAPSE | `inference_TAPSE.py` | ✅ tapse_2c |

**注意**：
- 2D 模型输入要求 3:4（480×640）；DICOM 输入脚本自动 resize 并校验比例。
- Doppler/TDI/M-Mode 均要求 DICOM（依赖 PhysicalDelta 标尺 tag 换算 cm/s）。
- 当前测试数据覆盖 9 个切面；缺 `Subcostal`、`RVOT`、`AV_Vmax` 三类真实 DICOM。

---

## 8. 已知问题与坑

1. **numpy <2 与 transformers <5 是硬约束**（见第 2 节），装任何新包前先 `pip check`，别让 pip 自动升级这两个。
2. **PhysicalDelta 临床校准未完成**：工具已实现对 `(0018,602C)/(0018,602E)`、参考像素/物理值、重叠 Region、单位和临床容差的审计，但服务器尚未生成医生确认报告。**推理跑通 ≠ 数值可信**。
3. **LA 切面待定**：临床 LA 前后径标准在 PLAX；测试数据 `dcm\a4c` 曾用于 la 也能跑通。建议两个切面各跑一例，按换算后数值合理性（LA 2.7–3.8 cm）定夺。
4. **三个切面缺样本**：全部 2D/Doppler 权重已补齐，但 `Subcostal`、`RVOT`、`AV_Vmax` 缺真实去标识化 DICOM。本轮仅临时豁免，不能形成 12/12 全切面验收。
5. **LVEF**：模型不输出，需 Teichholz 公式：EDV=7D³/(2.4+D)，LVEF=(EDV−ESV)/EDV×100（D 取 LVEDD/LVESD）。
6. **服务器纯 CPU / AorticRoot 长尾**：PLAX 依次运行 6 个二维模型，正常样本本轮约 45 分钟才完成，明显慢于历史约 379 秒；AorticRoot 是当前观察到的长尾阶段。`900` 秒是每个子模型而非整任务超时，六模型串行会让 `processing` 持续很久。已有 CPU/RSS 采样误用了主服务 PID，必须重测实际推理子进程后再判断 GPU 迁移。
7. **ECG XML 安全负例待补**：原始 UTF-8 BOM `2.xml` 已完成服务器功能冒烟；DTD/实体 XML 和非 XML 内容的 422 拒绝仍未现场执行。当前运行服务仍使用旧算法版本标签，因此还需在新版本进程上重跑一次快速 ECG 以完成发布门禁。
8. **新清单已生成但服务仍加载旧版本**：新候选为 `heart@bundle-f06f5f050c98+models@4bf76612484dd393`，实际启动日志仍为历史版本 `heart@bundle-2066fea65c13+models@feb35a11fa0e0bcb`。必须在启动服务的同一个 PowerShell 中重新读取清单；以启动日志为准，不能以另一个窗口的 `$env:ALGORITHM_VERSION` 为准。
9. **当前鉴权仅供 smoke test**：`CASE_AUTH_REQUIRED=false` 和 `ALLOW_INSECURE_CASE_API=true` 不能用于生产，也不能把端口直接暴露到外网。生产需受信网关剥离/重写身份头、注入代理密钥，并恢复临床复核角色约束。
10. **PowerShell 中文显示**：`Invoke-RestMethod` 的控制台表格曾把“未知”显示为乱码，但浏览器 JSON 正常。这更像控制台编码/格式化问题；需要区分显示乱码与服务端 JSON 编码错误。
11. **患者数据与日志**：结构化 ECG 结果含年龄、性别和患者标识。后续日志、截图、Agent 上下文和问题单必须默认脱敏；算法输入/输出不能直接发送给通用 LLM。
12. **ecg 输入**：需 HL7 aECG XML（12 导联完整，长节律 ≥5000 点）经 `xml_to_ecgfm_mat.py` 转 MAT；`feats` 形状 (12, N)、`org_sample_rate` 字段。
13. **DeepSeek Harness 尚未运行时验收**：插件包、自检、Python 契约测试和 dry-run 打包均已通过，但当前 Node.js `20.11.1` 低于声明的 `>=20.12.0`，未执行真实 profile 安装和 `dsh --dump-config`，算法服务也仍以 `--no-mcp` 启动。不得把“插件代码完成”写成“MCP/Agent 联调完成”。

---

## 9. 待办事项

- [x] 验证应用专用 MySQL 账号和 `heart_failure_analytics_dev` 连接。
- [x] 完成真实服务 ECG 单模态闭环。
- [x] 完成真实服务 TR_Vmax 心超单模态闭环。
- [x] 完成真实服务 TR_Vmax + ECG 混合闭环。
- [x] 完成临床批准记录和服务重启后的持久化读取验证。
- [x] 补齐全部 Measurement 2D/Doppler/TAPSE 权重并确认无 LFS 指针、无无效权重。
- [x] 生成文件传输部署代码指纹、18 个模型 artifact 清单和真实 `ALGORITHM_VERSION`。
- [x] 定位 PLAX 最新“报错”为服务停止导致的子进程中断，并复现 LVID 空收缩峰数组缺陷。
- [x] 在本地算法服务绕开未消费的 `--phase_estimate`、补充中断错误语义、逐模型耗时日志和门户等待提示。
- [x] 生成覆盖全部 handoff 待办的服务器验收清单 `docs\server-validation-checklist.md`。
- [x] 将 PLAX 修复完整部署到服务器，并生成新代码/模型发布身份 `heart@bundle-f06f5f050c98+models@4bf76612484dd393`。
- [x] 重启前后查询同一历史任务，确认旧任务版本和 approved 复核状态保持不变。
- [x] 使用原始 UTF-8 BOM ECG XML 完成服务器功能冒烟。
- [x] 完成正常 PLAX 样本六模型功能回归；最终约 45 分钟完成，性能门禁未通过。
- [x] 完成 DeepSeek Harness 插件工程包、自检、契约单测和 dry-run 打包。
- [ ] 在启动服务的同一 PowerShell 中注入新清单并重启，确认启动日志及新 ECG 任务均显示 `heart@bundle-f06f5f050c98+models@4bf76612484dd393`。
- [ ] 回归空收缩峰样本 `00005_851deaabbf3089ae.dcm`，确认 completed、六项结果且无 `IndexError`。
- [ ] 对 AorticRoot 活动子进程做单模型/整任务性能采样，保存六项 `duration_seconds`、总耗时和进程树峰值 RSS，再决定 GPU 迁移方案。
- [ ] 使用去标识化 DTD/实体和伪 XML 验证上传仍被 422 拒绝。
- [ ] 核对 PhysicalDelta 标尺问题，修正并以人工测量金标准验证 cm/cm·s⁻¹ 换算（临床上线阻塞项）。
- [ ] 获取 Subcostal、RVOT、AV_Vmax 测试 DICOM，取消临时豁免并完成 12/12 门禁。
- [ ] 在样本补齐前完成明确标注 waiver 的 9 切面条件 CPU 基准；不得把结果记为全切面通过。
- [ ] 确定 LA 切面口径（PLAX vs A4C）。
- [ ] 确认切面路由实现：用 DICOM `ViewName`/`ProtocolName` 标签自动识别 dcmType。
- [ ] 对 ECG/心超/混合推理做 CPU 耗时、峰值内存、超时和连续任务稳定性测试。
- [ ] 设计生产网关、TLS、`CASE_TRUSTED_PROXY_SECRET`、临床 reviewer 角色与最小权限 MCP 服务账号；轮换已暴露数据库口令。
- [ ] 把 MySQL、`D:\heart-data\cases` 和 `D:\heart-data\runtime` 纳入一致的备份、保留期、访问审计和恢复演练。
- [ ] 完成生产服务托管（Windows Service/NSSM 或等价方案）、健康检查、日志轮转和启动失败告警。
- [ ] 在以上生产边界满足后启用独立 MCP，再与 Agent Harness 做最小权限联调；Agent 只传 `caseId/assetId`，不直接访问算法服务器磁盘路径。
- [ ] 将 Node.js 升级到 `>=20.12.0`，安装插件到实际 Harness profile，执行 `dsh --dump-config` 并完成三个 MCP 工具的异步提交/查询联调。
- [ ] 心衰语料库/批量病例（`D:\project\心衰语料库`）接入批量推理。

---

## 10. 建议下一会话使用的技能

- `diagnosing-bugs`：定位新版本服务、PowerShell 中文显示或真实推理异常；先建立最小复现和证据链。
- `tdd`：若服务器冒烟暴露回归，先把真实失败压缩为自动化测试，再修改实现并回跑本地 `193 passed, 7 skipped` 基线。
- `implement`：在明确发布标识、鉴权网关或服务托管需求后实施变更，并保持现有可靠闭环测试通过。
- `review`：生产部署前从规格与工程标准两条线复核鉴权、路径脱敏、单实例约束、持久化和临床复核权限。

---

## 11. 参考链接

- EchoNet-Measurements 官方仓库：<https://github.com/echonet/measurements>
- 论文：Sahashi Y, et al. *Artificial Intelligence Automation of Echocardiographic Measurements.* JACC. 2025;86(13):964-978. <https://www.jacc.org/doi/10.1016/j.jacc.2025.07.053>
- ECG-FM 官方仓库：<https://github.com/bowang-lab/ECG-FM>（权重托管于 HuggingFace `wanglab/ecg-fm`）
- fairseq-signals：<https://github.com/Jwoo5/fairseq-signals>
- 旧交接文档：`D:\project\Measurement\Measurement\handoff.md`
