# 心衰算法 DeepSeek Harness 插件

这是一个独立、可安装的 DeepSeek Harness（`dsh`）bundle。它使用 Harness 自带的
`@deepseek-ai/dsh-mcp-client`，把本项目的 Streamable HTTP MCP 端点注册为模型可调用的
原生工具。推理、任务队列和病例数据仍由 Python 服务管理，本插件不复制算法逻辑。

## 目录边界

本目录可以单独复制或打包。运行时只需要：

- `package.json`：声明 `dsh.bundle.patch`，供 `dsh plugin` 识别；
- `cordis.patch.yml`：挂载远程 MCP 服务；
- `README.md`：部署和验证说明。

## 前置条件

- Node.js 20.12 或更高版本，以及可用的 DeepSeek Harness CLI；
- `pnpm` 已加入 `PATH`（`dsh plugin` 会把依赖管理命令转发给 pnpm；可先运行 `corepack enable`）；
- 心衰算法服务可从 Harness 所在主机访问；
- 生产模式下，插件 Token 与算法服务的 `MCP_SHARED_SECRET` 必须一致。

## 1. 启动算法 MCP 服务

在项目根目录执行。下面是无需模型权重的本地联调方式：

```powershell
$env:MCP_ENABLED='true'
$env:MCP_SHARED_SECRET='replace-with-a-long-random-secret'
python main.py --fake --mcp --host 127.0.0.1 --port 8000
```

真实推理环境沿用项目原有的模型、MySQL、病例存储和鉴权配置，只需额外启用 MCP：

```text
MCP_ENABLED=true
MCP_SHARED_SECRET=<与插件端相同的随机密钥>
```

服务端点默认为 `http://127.0.0.1:8000/mcp`。

## 2. 安装到 DeepSeek Harness

在项目根目录设置连接参数并安装到实际使用的 profile：

```powershell
$env:HEART_ALGO_MCP_URL='http://127.0.0.1:8000/mcp'
$env:HEART_ALGO_MCP_TOKEN='replace-with-a-long-random-secret'
dsh plugin --profile web add ./deepseek-harness-plugin
dsh --profile web --dump-config
dsh web
```

也可以安装到 `headless` 或其他自定义 profile：

```powershell
dsh plugin --profile headless add ./deepseek-harness-plugin
```

环境变量必须在启动 `dsh` 的同一进程环境中存在。不要把真实 Token 写进
`cordis.patch.yml` 或提交到 Git。

## 配置

| 环境变量 | 必填 | 默认值 | 用途 |
|---|---:|---|---|
| `HEART_ALGO_MCP_URL` | 否 | `http://127.0.0.1:8000/mcp` | Streamable HTTP MCP 地址 |
| `HEART_ALGO_MCP_TOKEN` | 生产必填 | 无 | 生成 `Authorization: Bearer ...` 请求头 |

插件将单次工具调用超时设为 60 秒。提交和查询是两个独立的短调用；真实推理在
Python 服务的任务队列中异步执行，不会占用一次 MCP 调用等待推理完成。

## 调用前的数据准备

MCP 工具面支持两条输入路径：

**路径 A（一站式）**：直接调用 `mcp__heart-algo__analyze_case_files`，传入本地文件
路径列表（每项含 `path`、`modality`，心超另需 `dcm_type`），服务会自动创建病例、
登记资产并提交分析。病例归属 `MCP_SERVICE_USER_ID` 服务账号，适合 Agent 自主闭环。

**路径 B（病例门户）**：仍可先通过病例 HTTP API 完成登记，适合归属真实医生的病例：

1. `POST /heart-algo/cases` 创建病例。请求体包含 `requestId` 和 `sysUserId`；
2. `POST /heart-algo/cases/{case_id}/assets` 以 multipart 上传心超 DICOM 或 ECG XML；
3. 保留响应中的 `caseId` 和各资产的 `assetId`，再交给 Harness Agent。

创建病例时，服务会把 `MCP_SERVICE_USER_ID` 对应的服务账号加入授权范围，因此 MCP
插件只可访问经病例接口明确授权给该服务账号的数据。生产环境的 HTTP API 身份验证、
可信代理头和用户隔离仍按本项目部署文档配置，MCP Token 不替代病例 API 的用户鉴权。

## 可用工具

连接成功后，模型会看到以下工具：

**输入与分析**

- `mcp__heart-algo__analyze_case_files`：一站式分析——从本地文件路径创建病例、登记资产并提交诊断，返回 `case_id` 与 `task_id`；
- `mcp__heart-algo__diagnose_heart_failure`：用 `case_id` 和可选 `asset_ids` 提交诊断任务，立即返回 `task_id`；
- `mcp__heart-algo__get_diagnosis_result`：按 `task_id` 查询长任务。

**解读与报告**

- `mcp__heart-algo__interpret_diagnosis`：规则解读——按参考范围标注异常指标、输出 LVEF 分型（HFrEF/HFmrEF/HFpEF）与 E/A、E/e' 等组合指标；
- `mcp__heart-algo__generate_report`：把已完成任务渲染成 Markdown/JSON 报告草稿，可 `save_to_case` 存回病例工件；
- `mcp__heart-algo__compare_diagnoses`：同病例两次任务的指标纵向对比（绝对/相对变化、LVEF 分型迁移）。

**病例与任务检索**

- `mcp__heart-algo__list_cases`：列出服务账号可见的病例摘要；
- `mcp__heart-algo__get_case_detail`：病例资产、任务实时状态、复核历史与报告工件详情；
- `mcp__heart-algo__list_tasks`：任务列表，可按 `case_id` 过滤。

**临床复核**

- `mcp__heart-algo__get_review_status`：查询任务的复核状态与历史；
- `mcp__heart-algo__submit_review`：记录临床复核结论（approved/rejected）；复核人不能是病例所有者。

**能力发现**

- `mcp__heart-algo__list_supported_views`：查询支持的切面与指标。

例如可让 Agent 执行：

> 分析已登记病例 `case-...` 中的资产 `asset-...`。先调用诊断工具；使用返回的
> `task_id` 查询到 completed 或 failed，最后按结构化结果总结，并明确说明仅供临床辅助。

当前 Harness MCP bridge 只把 MCP Tools 注册到 `ctx.tools`；服务端 Resource 和 Prompt
仍保留给支持这些能力的其他 MCP 客户端，本插件不会伪造对应工具。

## 验证与排错

```powershell
cd deepseek-harness-plugin
npm run check
npm pack --dry-run
dsh --profile web --dump-config
```

`npm run check` 完全位于本目录内且无需安装依赖，可验证 bundle 清单、关键 MCP 配置和
凭据引用。安装到 Harness 后，`dsh --profile web --dump-config` 是最终的运行时解析检查；
输出中应能找到 `heart-algo-mcp`。仓库维护者还可在项目根目录运行：

```powershell
python -m pytest test/test_deepseek_harness_plugin.py -q
```

若 Harness 启动时提示连接失败，请依次检查：

1. 算法服务是否以 `--mcp` 或 `MCP_ENABLED=true` 启动；
2. `HEART_ALGO_MCP_URL` 是否能从 Harness 主机访问；
3. 两端 Token 是否完全一致；
4. 反向代理是否透传 MCP 的请求头和流式响应。

卸载命令：

```powershell
dsh plugin --profile web remove heart-algo-dsh-plugin
```
