# 心衰算法服务服务器验收清单

- 验收日期：2026-08-20
- 服务器代码：`D:\project\heart`
- Measurement：`D:\project\Measurement\Measurement`
- 验证输出：`D:\heart-data\validation`
- 原则：只有真实服务器输出可勾选通过；缺样本、临床签字或生产基础设施时必须记为 `BLOCKED`，不能以代码单测代替。

## 状态定义

| 状态 | 含义 |
|---|---|
| PASS | 服务器实测满足通过条件，证据文件已保存 |
| CONDITIONAL | 有明确 waiver 的条件验收，不代表生产通过 |
| BLOCKED | 缺临床决策、真实样本、账号或基础设施 |
| FAIL | 已执行且结果不满足门禁 |

## 0. 本次 PLAX 故障结论

附件中的 `return_code=3221225786 (0xC000013A)` 与 `KeyboardInterrupt` 表示服务在模型逐帧推理期间被 Ctrl+C 停止，并非模型自行崩溃。纯 CPU 的 PLAX 会串行运行 LVID、IVS、LVPW、LA、Aorta、AorticRoot 六个模型；任务为 `processing` 时不得停止服务。

另一个已复现缺陷是 Measurement `--phase_estimate` 在收缩峰数组为空时访问索引 0。本次算法服务修复不再调用这个未消费的相位覆盖层，ED/ES 仍按 distance CSV 全局极值计算。该计算路径仍须经过 PhysicalDelta 和临床金标准验收。

## 1. 部署与代码回归

- [ ] 将本次提交完整部署到 `D:\project\heart`，不要只复制单个文件。
- [ ] 确认 `echonet_runner.py` 包含以下三个修复标识：

```powershell
Set-Location D:\project\heart
Select-String -Path .\echonet_runner.py -Pattern `
  "服务停止操作中断", `
  "duration_seconds", `
  "相位覆盖层"
```

- [ ] 回归测试通过且没有新增失败：

```powershell
conda activate heart
Set-Location D:\project\heart
python -m pytest -q
```

通过条件：`193 passed, 7 skipped`。若最终提交后的数量变化，以“零失败且跳过项仍有明确原因”为准，并记录完整摘要。

证据：`D:\heart-data\validation\pytest-summary.txt`

## 2. 重新发布算法身份

本次代码发生变化，旧版本 `heart@bundle-2066fea65c13+models@feb35a11fa0e0bcb` 只能作为历史任务版本，不能继续标记新任务。

- [ ] 按 `docs\p0-p1-validation.md` 第 3 节重新生成 `release-manifest.json`。
- [ ] 服务版本必须对应本次提交或本次部署包的不可变指纹。
- [ ] 18 个模型 artifact 必须重新读取并保持完整哈希。
- [ ] 启动前从新清单注入环境变量：

```powershell
$manifestPath = "D:\heart-data\validation\release-manifest.json"
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$env:ALGORITHM_VERSION = [string]$manifest.algorithmVersion

if ([string]::IsNullOrWhiteSpace($env:ALGORITHM_VERSION) -or
    $env:ALGORITHM_VERSION -eq "unknown") {
    throw "ALGORITHM_VERSION 无效"
}

$env:ALGORITHM_VERSION
```

通过条件：新版本不是 `unknown`，且服务启动日志中的 `algorithm_version` 与清单完全一致。

证据：`release-manifest.json`、启动日志片段。

## 3. 新旧任务版本不可变性

在重启前先记录一个已完成旧任务的 `caseId/taskId/sysUserId` 和版本，重启后再查询同一任务。

```powershell
$baseUrl = "http://127.0.0.1:8000"
$oldCaseId = "<旧 caseId>"
$oldTaskId = "<旧 taskId>"
$oldUserId = "<旧 sysUserId>"
$oldUri = "$baseUrl/heart-algo/cases/$oldCaseId/diagnoses/$oldTaskId" +
          "?sys_user_id=$([Uri]::EscapeDataString($oldUserId))"

$oldAfter = Invoke-RestMethod -Uri $oldUri
$oldAfter | Select-Object caseId,taskId,status,algorithmVersion
```

- [ ] 原始 BOM ECG 新任务完成后，`algorithmVersion` 等于新清单。
- [ ] 旧任务重启前后 `algorithmVersion` 完全一致。
- [ ] 旧任务报告、临床复核记录和输入哈希未被重写。

证据：只保存去标识化的任务 ID、状态、版本和哈希比较，不复制患者字段。

## 4. ECG XML 安全冒烟

- [ ] 原始 UTF-8 BOM 文件 `D:\heart-data\cases\test\ecg\2.xml` 上传 201，真实推理 completed。
- [ ] DTD/实体 XML 上传被 422 拒绝。
- [ ] 非 XML 内容上传被 422 拒绝。
- [ ] 报告版本等于新发布清单，ECG `error=null` 且 predictions 非空。

通过门户执行正常 BOM 文件；恶意输入使用去标识化的最小测试文件，不得使用患者内容。

证据：HTTP 状态、任务状态、版本、预测数量；不保存 patientInfo。

## 5. PLAX 单图回归与耗时

### 5.1 已知可运行样本

- 输入：`D:\heart-data\cases\test\dcm\plax\00003_2dbbf05f7a19120f.dcm`
- [ ] 门户任务最终为 completed；等待期间不停止服务。
- [ ] 日志按顺序出现 6 条 `心超子任务完成 metric=... duration_seconds=...`。
- [ ] 日志不出现 `KeyboardInterrupt`、`IndexError` 或 `--phase_estimate`。
- [ ] 报告包含 LVID、IVS、LVPW、LA、Aorta、AorticRoot；单图 `error=null`。
- [ ] 记录总耗时、六个子模型耗时和峰值 RSS，不以主观“快/慢”代替数字。

### 5.2 空收缩峰回归样本

- 输入：`00005_851deaabbf3089ae.dcm` 对应的去标识化服务器副本。
- [ ] 任务 `status=completed`，且 `cardiacUltrasound[0].error=null`。
- [ ] 报告包含 LVID、IVS、LVPW、LA、Aorta、AorticRoot 六项输出。
- [ ] 日志不再出现 `systolic_i[0]` 的 `IndexError`，并记录六个子模型耗时与任务总耗时。
- [ ] 若 distance CSV 可用，结果仍必须经过第 7 节临床校准；技术完成不代表数值可信。

### 5.3 中断语义

- [ ] 仅在专用测试任务中停止服务，重启后查询结果不得显示普通“模型推理失败”。
- [ ] 可见错误应为“心超推理被服务停止操作中断，请重新提交任务”。

证据：去标识化日志、报告摘要、CPU 基准报告。

## 6. 模型覆盖与三项 waiver

```powershell
Set-Location D:\project\heart
python -m quality.audit_model_coverage `
  --measurement-dir "D:\project\Measurement\Measurement" `
  --samples-root "D:\heart-data\cases\test\dcm" `
  --output "D:\heart-data\validation\model-coverage.json"
```

- [ ] 9 个 2D、8 个 Doppler/TAPSE 和 1 个 ECG artifact 均有效，无 LFS 指针。
- [ ] 当前缺 Subcostal、RVOT、AV_Vmax 时，只能记录 `CONDITIONAL 9/12`。
- [ ] 获取三类真实去标识化 DICOM 后重新执行，只有 `allAssetsReady=true` 才是 `PASS 12/12`。

## 7. PhysicalDelta 临床校准（上线阻塞）

- [ ] LVID、LA、TR_Vmax、TDI、TAPSE 均生成校准报告。
- [ ] 每项包含真实模型坐标、DICOM Region、PhysicalDelta、单位、金标准、误差和临床容差。
- [ ] 重叠 Region 显式指定 `regionIndex`。
- [ ] `summary.failed=0`。
- [ ] 心超医生签字确认；工具通过不能替代签字。

命令见 `docs\p0-p1-validation.md` 第 4 节。

证据：`D:\heart-data\validation\*-calibration-report.json` 和独立签字记录。

## 8. LA 切面与自动路由

- [ ] 临床负责人明确选择 LA 前后径口径：PLAX 或 A4C。
- [ ] PLAX/A4C 各至少一例与人工测量对照。
- [ ] 自动切面路由必须在真实设备 DICOM 上验证 `ViewName/ProtocolName/SeriesDescription` 的可用性。
- [ ] 元数据缺失或冲突时必须要求人工选择，不能静默猜测。

状态：缺临床决策或真实设备元数据时记为 `BLOCKED`。

## 9. CPU 连续任务稳定性

- [ ] 清单包含 ECG、心超、混合和当前 9 个有样本切面。
- [ ] 明确记录三项 waiver，不能写成全切面通过。
- [ ] 初验 `iterations>=3`；正式容量值由业务确认。
- [ ] 报告无失败，包含 P50/P95/max、进程树峰值 RSS、每场景错误。

```powershell
Set-Location D:\project\heart
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

## 10. 生产安全与运维边界

以下项目需要网关、运维或安全负责人，不能在本机 smoke 配置下勾选：

- [ ] TLS 和受信网关；外部请求头被剥离后重新注入。
- [ ] `CASE_AUTH_REQUIRED=true`，关闭 `ALLOW_INSECURE_CASE_API`。
- [ ] 配置并轮换 `CASE_TRUSTED_PROXY_SECRET`、MCP Bearer 密钥和数据库口令。
- [ ] 病例所有者不能自我批准，reviewer 必须具有 `cardiology-reviewer`。
- [ ] MySQL、cases、runtime 采用一致备份点；完成恢复演练。
- [ ] Windows Service/NSSM 托管、健康检查、日志轮转、启动失败告警。
- [ ] 单实例、单 Uvicorn worker 约束得到监控；第二实例锁测试通过。
- [ ] 日志、截图、问题单和 Agent 上下文默认去标识化。

状态：基础设施或安全负责人未提供证据时记为 `BLOCKED`。

## 11. MCP、Agent 与批量语料库

- [ ] 只有第 7、10 节通过后才启用 MCP。
- [ ] Agent 使用独立最小权限账号，只传 `caseId/assetId`。
- [ ] Agent 不读取服务器磁盘路径，不接收原始患者输入/输出。
- [ ] ECG、心超、混合 MCP 调用与 HTTP 任务空间一致。
- [ ] `D:\project\心衰语料库` 接入前完成去标识、批处理幂等、失败重试、限流和保留期设计。

状态：生产边界未通过时记为 `BLOCKED`，不能为了联调提前放开。

## 12. 验收汇总

| 门禁 | 状态 | 证据/备注 |
|---|---|---|
| 代码回归 |  |  |
| 新发布身份 |  |  |
| 新旧任务版本 |  |  |
| BOM/XML 安全 |  |  |
| PLAX 正常样本 |  |  |
| PLAX 空峰回归 |  |  |
| 模型覆盖 |  |  |
| PhysicalDelta 临床签字 |  |  |
| LA 口径 |  |  |
| CPU 稳定性 |  |  |
| 生产安全 |  |  |
| 备份恢复/服务托管 |  |  |
| MCP/Agent |  |  |
| 批量语料库 |  |  |

验收结论只能填写以下之一：

- `开发联调通过`
- `9/12 条件验收（含明确 waiver）`
- `12/12 算法门禁通过，等待生产安全/临床门禁`
- `生产验收通过`
- `不通过`
