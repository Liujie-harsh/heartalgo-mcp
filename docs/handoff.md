# Heart 算法服务与 DeepSeek Harness 交接

- 更新日期：2026-08-27
- 仓库：`D:\project\heart`
- 当前分支：`master`，HEAD `bff1e4f`
- 下一阶段目标：在不阻塞现有 4090D GPU subprocess 上线的前提下，完成生产部署边界、真实 MCP 验收和 `heart-health-dsh-suite` P0。

本文只记录当前状态、证据入口、剩余门禁和下一步。详细契约与设计不在此重复，直接引用对应文档。

## 1. 当前结论

1. PLAX CPU 性能证据闭环已经完成，GPU 价值明确；4090D 上现有 GPU subprocess 灰度通过，可继续作为当前上线方案。
2. 持久推理 Worker（性能计划 P1）尚未实现。它是后续吞吐/成本优化，不阻塞当前 GPU subprocess 上线。
3. Heart HTTP/MCP、病例、结构化结果和临床复核代码已具备本地自动化基础；真实生产 MCP、真实 MySQL 本轮集成测试、4090D ECG 和混合模态链路仍未完成验收。
4. 现有 `heart-algo-dsh-plugin` 只负责 MCP 连接，工程自检和 dry-run 打包通过；新的 `heart-health-dsh-suite`（preset/guidance/wrapper tools/policy）尚未实现，但现有 Harness 接缝足以支撑 PRD P0。
5. `docs/prd.md` 的“LLM 直接提交 URL、主工具等待结果”契约已经过期。当前正式方向是病例门户/API 先上传资产，MCP 只接受 `case_id` 和可选 `asset_ids`，提交与查询保持两段式。

## 2. 关键文档与证据

| 内容 | 路径 |
|---|---|
| Heart 第一阶段接口与部署边界 | `D:\project\heart\docs\phase1-reliable-loop.md` |
| 旧 MCP PRD（需更新契约） | `D:\project\heart\docs\prd.md` |
| PLAX 性能优化计划与待办 | `D:\project\heart\docs\plax-performance-optimization-plan.md` |
| CPU 六模型分析 | `D:\project\heart\docs\plax-performance-analysis-20260821.md` |
| DeepSeek Harness 产品 PRD | `D:\project\heart\docs\PRD DeepSeek Harness 心脏健康插件套件与心超心电推理协同.md` |
| 现有 MCP Bridge 插件 | `D:\project\heart\deepseek-harness-plugin` |
| CPU P0 阶段计时证据 | `D:\heart-data\validation\perf-plax-stage-20260821-145440` |
| GPU 单模型/六模型/热运行证据 | `D:\heart-data\gpu推理测试\822test` |
| GPU 服务灰度证据 | `D:\heart-data\gpu推理测试\gpu灰度测试` |
| GPU 10 次稳定性汇总 | `D:\heart-data\gpu推理测试\gpu灰度测试\logs-gpu-gray\gpu-gray-stability-10.json` |
| GPU 服务日志 | `D:\heart-data\gpu推理测试\gpu灰度测试\logs-gpu-gray\service.log` |

所有日志、截图、任务样本和后续问题单继续按去标识化原则处理；本文不记录数据库口令、Token、患者标识或可复用凭据。

## 3. 已验证状态

### 3.1 PLAX CPU 与 GPU

- 同一份 94 帧去标识化 PLAX DICOM SHA-256：`9a0f031297808923ca3e1a6e3112aa751fa170b87c24d72ed4da20edb654c74e`。
- CPU 六模型总耗时：2683.704 秒（44.73 分钟）。
- CPU 前向推理：2561.970 秒，占 95.464%。
- CPU 进程树峰值 RSS：1732.12–1747.09 MiB。
- 4090D 灰度连续 10 个完整任务：10 成功、0 失败。
- GPU 灰度端到端中位数：78.491 秒；范围 68.470–84.579 秒；波动系数约 5.58%。
- 相对 CPU 完整任务，当前 GPU 灰度端到端约快 34.2 倍。
- GPU 六模型阶段平均 71.814 秒，其中距离标注视频 23.43 秒（32.62%）、Python 导入 19.98 秒（27.82%）、resize 7.71 秒（10.74%）、前向推理 7.25 秒（10.10%）。
- 10 次相同输入的 12 类坐标/距离 CSV 每类均只有一个唯一 SHA-256，重复执行输出稳定。
- CPU/GPU 坐标最大差异为 1 像素；结构化距离差异很小，但 LVEF 约有 0.66 个百分点差异，仍需算法/临床容差确认。

本次 10 连跑证明的是服务常驻下的重复 subprocess 稳定性，不是模型常驻 Worker 热启动；相同 DICOM 重复也不能完全证明不同患者任务之间没有串数据。

### 3.2 GPU 资源与灰度边界

- 运行环境证据：RTX 4090 D、PyTorch `2.4.0+cu121`、torchvision `0.19.0+cu121`、`cuda:0`。
- 既有运行期采样曾记录约 1332 MiB 显存峰值、100% GPU 利用率；本次灰度目录中的 `nvidia-smi-final.txt` 是停止后的快照（1 MiB、无进程），不能代替 10 连跑期间的峰值曲线。
- 灰度服务使用 `task_store=memory`、`mcp_enabled=False`，因此不能当作生产 MySQL/MCP 验收。
- 灰度只执行了 PLAX。服务日志中的 ECG 项仍是 Windows 默认路径，不能作为 4090D Linux ECG 已部署的证据。
- 灰度算法版本为 `heart@bff1e4f+models@4bf76612484dd393+runtime@cu121-4090d`。

### 3.3 自动化测试

2026-08-24 使用项目 Conda 环境和独立 pytest 基目录完成全套测试：

```text
203 passed, 7 skipped in 8.14s
```

7 项跳过全部为需要 `TEST_DATABASE_URL` 的真实 MySQL 集成测试。默认 Windows 临时目录 ACL 已损坏，直接运行 pytest 会产生 `PermissionError`；复测应显式指定新的 `--basetemp`：

```powershell
& "C:\Users\lj\miniconda3\envs\heart\python.exe" -m pytest -q test `
  --basetemp "D:\heart-data\validation\pytest-basetemp-next"
```

### 3.4 Git 状态

当前可见提交：

- `237fc29 feat: establish heart service and PLAX observability`
- `f0b3b4d docs: record PLAX P0 performance evidence`
- `bff1e4f docs: complete six-model PLAX stage analysis`

工作区存在用户创建的未跟踪 DeepSeek Harness 文档，不要删除、覆盖或随意加入其他变更。开始任何实现前先运行 `git status --short`。

## 4. 推荐生产拓扑

在现有代码没有远程 GPU Worker 的前提下，应把整个 Heart FastAPI/MCP 服务与推理脚本一起运行在 4090D：

```text
本地稳定服务器
├─ MySQL 8
├─ DeepSeek Harness
│  ├─ heart-algo-dsh-plugin
│  └─ heart-health-dsh-suite
└─ VPN/SSH/受信网关
             │
             ▼
4090D Linux
├─ Heart FastAPI（病例 API + /mcp）
├─ 持久 CASE_STORAGE_ROOT / TASK_WORK_ROOT
├─ Measurement PLAX GPU subprocess
└─ ECG-FM runner
```

关键约束：

- 4090D 访问本地 MySQL 应走 VPN、受限专线或受管 SSH reverse tunnel；不要把 3306 暴露公网。
- Heart 生产模式必须使用 `TASK_STORE_BACKEND=mysql`，不得设置易失回退。
- 文件病例存储仍在 `CASE_STORAGE_ROOT`，不在 MySQL；4090D 的病例目录必须使用持久盘，并与 MySQL 纳入一致备份和恢复演练。
- 当前文件病例存储只允许单应用实例、单 Uvicorn worker；不要使用 `--workers` 横向扩容。
- `/mcp` 使用独立 Bearer secret；病例 HTTP API 使用可信网关身份头，两者不能互相替代。
- `MCP_SERVICE_USER_ID` 应在创建正式病例前固定，因为病例 ACL 在创建时记录该服务账号。
- 建议服务继续监听回环地址，通过 VPN、SSH tunnel 或受信反向代理访问。

## 5. 部署与真实环境剩余门禁

按优先级执行：

1. **本地 MySQL 实连**：配置临时 `TEST_DATABASE_URL` 跑完 7 个集成测试；验证 schema、账号最小权限、断线语义和服务重启恢复。
2. **4090D 持久化**：确定 `CASE_STORAGE_ROOT`、`TASK_WORK_ROOT` 的持久盘和备份，不依赖可释放容器临时目录。
3. **4090D ECG-FM**：部署 Linux 项目目录、权重和 Python 环境，显式配置 `ECGFM_PROJECT_DIR`、`ECGFM_CHECKPOINT`、`ECGFM_PYTHON` 和超时。
4. **生产 MCP**：启用 `MCP_ENABLED=true` 和共享密钥；验证无 Token 拒绝、正确 Token 连接、重连及日志不泄露凭据。
5. **真实病例闭环**：使用去标识化 DICOM 与 ECG XML 分别完成 PLAX-only、ECG-only、混合任务；检查 processing/completed/failed、输入哈希、算法版本、逐资产错误和复核状态。
6. **故障与恢复**：覆盖无效 case/asset、损坏 DICOM/XML、Measurement/ECG timeout、子进程强杀、服务重启、MySQL 短暂断连和 MCP 断线恢复。
7. **并发与资源**：GPU 使用单任务槽；用两份不同 DICOM 交替执行至少 20 次，同时采集服务 RSS、GPU 显存/利用率和输出隔离。
8. **临床门禁**：完成 PhysicalDelta 人工金标准校准，并签署 CPU/GPU 数值容差，特别是 LVEF 差异。

当前 300 秒 GPU Measurement timeout 足够覆盖灰度任务，但不能支持约 45 分钟的 CPU 自动回退。若保留 CPU fallback，必须按运行模式设置独立 timeout，并避免静默把用户请求降级成超长任务。

## 6. DeepSeek Harness PRD 进度

目标文档：`docs/PRD DeepSeek Harness 心脏健康插件套件与心超心电推理协同.md`。

### 已完成基础

- Heart MCP 提供 `diagnose_heart_failure`、`get_diagnosis_result`、`list_supported_views`、诊断 Resource 和解释 Prompt。
- 当前 MCP 契约只接受已上传病例的 `case_id/asset_ids`，不接受 URL、路径或二进制内容。
- `deepseek-harness-plugin` 已通过 `npm run check`。
- 使用独立 npm cache 的 `npm pack --dry-run` 通过，发布物只有 README、manifest、Cordis patch 和检查脚本，无凭据。
- 当前 Node.js 为 `v22.19.0`，满足 Harness 源码 `^22.19.0 || >=24.0.0`。
- Harness 源码基线：`D:\project\dsh\deepseek-harness`，commit `47f943859b`。其中已有用户修改和未跟踪 `.npmrc`，不要覆盖。

### 已完成（P0，2026-08-27 本轮）

独立安装单元 `D:\project\heart\heart-health-dsh-suite` 已按 PRD P0 实现并全部验证通过：

- preset/guidance/tools/policy 四层齐备：三个 `heart_*` 包装工具、驻留指导（order 150）、
  前缀隐藏 + guard 拒绝策略；canonical 判别式三态输出、Top-K 截断、presentation metadata。
- 数据最小化在包装层构造 canonical value 时就地 `scrubValue`；`patient_info` 仅保留 age/sex，
  可经 `HEART_HEALTH_KEEP_PATIENT_INFO` 整体关闭。
- 关键设计修正：preset 组合行不再拷贝 runtime JS，改用裸包名子路径
  （`heart-health-dsh-suite/tools` / `/policy`）由宿主基座解析（与 `@deepseek-ai/dsh-persona` 同机制）；
  用户根目录只保存两个 yaml，代码始终跟随安装的包。这是真实 Loader 才暴露的约束。
- 验证证据：`npm run build`（11 个 js 模块）、`npm run check` 全绿、
  33 个 vitest 用例全绿（真实 AgentPresets/Loader 组合、三态契约、隐私泄漏注入清除、
  输入拒绝、policy 掩码/guard、并发与顺序生命周期）、
  `npm pack --dry-run` 发布清单 28 个文件无凭据无冗余目录。

测试运行方式：`$env:DSH_CHECKOUT='D:\project\dsh\deepseek-harness'` 后在 suite 目录执行
`npm run build && npm run check && npm test`。沙箱禁止 fork 子进程，vitest 配置固定为
`pool: 'threads'` 且 `fileParallelism: false`；npm 操作需 `--cache <工作区内目录>`。

### 尚未实现

- 真实 Harness profile 的 `plugin add`、`bundle apply`、`dump-config` 和实际 MCP 调用
  （依赖本地 `dsh` CLI 可用性；当前源码启动有 `uv_os_get_passwd ... ENOMEM` 环境问题）。
- 跨进程 FakeRunner Streamable HTTP MCP 验收（第 7 节第 4 步）。
- PRD P1 五类按需 skills 与结果卡片；提醒、随访和趋势保持 P2。

关键实现约束：原始 `heart-algo-dsh-plugin` 应注册在 host/profile 全局层；heart-health preset 在自己的 scope 注册包装工具，并限制继承的原始 MCP 工具。`tools.restrict()` 不会隐藏同 scope 自己注册的工具，因此不能把原始 MCP client 和包装工具简单放进同一个 preset 层。包装工具内部应传递 parent execution token 与 AbortSignal，通过受控的嵌套调用读取 MCP `structuredContent`，不得解析渲染文本。

本机 `dsh` 尚未全局安装；`corepack pnpm` 因当前网络/注册表访问失败，源码方式启动又遇到 `uv_os_get_passwd ... ENOMEM`。这些属于 Harness 本地运行环境阻塞项，应在真实 Loader 验收前解决，不需要占用 4090D。

## 7. 下一会话建议顺序

1. 先在本地使用 Heart FakeRunner 和可控假 MCP 实现 `heart-health-dsh-suite` P0，不使用 GPU。
2. 完成 wrapper 工具的输入拒绝、三状态、畸形 MCP、隐私、policy、取消和生命周期测试。
3. 用真实 Harness Loader 组装 host MCP bridge + heart-health preset，断言模型只看到 `heart_*` 工具。
4. 启动本地 Heart FakeRunner Streamable HTTP MCP，做跨进程提交与查询验收。
5. 解决本地 MySQL 到 4090D 的安全网络、ECG Linux 环境和持久病例盘。
6. 最后只消耗少量 4090D 时间完成 PLAX-only、ECG-only、混合任务、断线恢复和可选真实 DeepSeek 模型冒烟。
7. P0 稳定后再进入 PRD P1 的五类按需 skills 与结果卡片；提醒、随访和趋势保持 P2，不提前扩张。

## 8. 建议技能

- `implement`：按 DeepSeek Harness PRD P0 实现独立 bundle、preset、tools 和 policy。
- `tdd`：先以假 MCP、FakeRunner 和 Loader 测试固定 wrapper、安全与生命周期契约。
- `diagnosing-bugs`：处理 Harness Loader、MCP 重连、4090D ECG、MySQL 网络或真实推理故障。
- `review`：P0 完成后按 PRD 和 Harness 源码规范双轴复核，再进入真实环境验收。
- `handoff`：下一阶段完成或切换会话时，继续更新本文，只保留最新状态和证据入口。
