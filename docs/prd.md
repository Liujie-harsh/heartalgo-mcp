# 心衰诊断算法服务 MCP 接入产品需求文档（PRD）

## 1. 文档信息

| 项目 | 内容 |
|---|---|
| 文档版本 | v1.0 |
| 编写日期 | 2026-08-12 |
| 状态 | 待评审 |
| 关联系统 | 心衰诊断算法服务（`api.py` / `main.py` / `combined_runner.py` 等） |
| 关联文档 | 《心衰诊断算法API接口与联调测试文档v1.0.md》《输入物化下载层.md》 |

## 2. 背景与问题

### 2.1 现状

心衰诊断算法服务已提供完整的异步任务式 HTTP API：

- `POST /heart-algo/task/start` —— 启动分析任务（心超 DICOM + ECG XML 混合）
- `POST /heart-algo/task/result` —— 查询任务结果

该 API 面向**后端系统**（人/程序按约定调用），已完成真实推理联调、MySQL 持久化、错误脱敏、单图失败隔离、重启恢复等能力验收。

### 2.2 新需求

业务方希望 **LLM 智能体**（如临床 Copilot、Claude Desktop、Dify 等）能够直接调用心衰诊断能力，例如：

> "分析这份心超报告，判断属于哪种心衰分型"
> "这份 ECG 有什么异常，给出 Top-5 概率"

现有 HTTP API 存在两个不适配点：

1. **协议不透明**：调用方必须理解 `requestId`/`sysUserId`/`taskId` 幂等语义、`taskState` 状态机、`reportResult` 二次 JSON 解析等内部约定，LLM 智能体难以自主正确调用。
2. **生态不互通**：每个智能体框架（Claude / Cursor / Dify / LangChain）各有接入方式，为每个框架各写一遍集成成本高。

### 2.3 目标

1. 为 LLM 智能体提供标准化的 **MCP（Model Context Protocol）** 接入方式，一次接入、多端可用（Claude Desktop、Dify、自研 Copilot 等所有 MCP 客户端）。
2. MCP 层作为现有 HTTP API 的**薄适配层**：复用推理、队列、持久化、错误脱敏、输入物化下载等全部既有能力，不重写推理逻辑。
3. 为智能体隐藏内部协议细节，提供**结构化、可直接消费**的诊断结果。

### 2.4 非目标（本期不做）

- 不修改现有 HTTP API 的接口契约（向后兼容）。
- 不做临床准确性改进（属于模型侧）。
- 不做 MCP 协议本身的鉴权标准实现（协议无此标准，由部署层承担）。
- 不为单一框架单独开发插件（LangChain 等通过 MCP 适配器接入）。

## 3. 已确认的关键决策

| 决策项 | 结论 | 说明 |
|---|---|---|
| 消费方 | **LLM 智能体** | MCP 是标准选择 |
| 部署拓扑 | **与算法服务同机同进程** | 共享 store/队列/runner，单进程单端口 |
| 接入形态 | **MCP Server（薄适配层）** | 现有 HTTP API 保持为核心 |
| 输入格式 | **URL（http/https）** | 复用 `InputMaterializer` 既有下载能力 |
| 长任务策略 | **两段式**（主工具 + 状态查询工具） | 绕开 MCP 客户端工具调用超时 |

## 4. 需求范围

### 4.1 功能需求

| 编号 | 需求 | 优先级 |
|---|---|---|
| FR-1 | 提供 MCP 工具 `diagnose_heart_failure`：提交心超/ECG 诊断任务（URL 输入），等待完成并返回结构化诊断结果 | P0 |
| FR-2 | 提供 MCP 工具 `get_diagnosis_result`：按 task_id 查询任务结果（供主工具超时后轮询） | P0 |
| FR-3 | 提供 MCP 工具 `list_supported_views`：返回支持的切面类型与指标目录（含单位/参考范围） | P1 |
| FR-4 | 提供 MCP Resource `heart-algo://diagnosis/{task_id}`：按需读取完整报告 JSON | P1 |
| FR-5 | 提供 MCP Prompt `heart_failure_interpretation`：指导智能体正确解读诊断结果 | P2 |
| FR-6 | MCP 端点挂载到现有 FastAPI 应用（`/mcp`），与 HTTP API 同端口共存 | P0 |
| FR-7 | MCP 层自动生成内部 `task_id`/`requestId`，使用配置的服务账号 `sysUserId`，隐藏幂等细节 | P0 |
| FR-8 | MCP 返回结果前将 `reportResult` JSON 字符串二次解析并展平为结构化字段 | P0 |

### 4.2 非功能需求

| 编号 | 需求 | 说明 |
|---|---|---|
| NFR-1 | 单次工具调用最长等待 ≤50s | 低于常见 MCP 客户端超时（~60s） |
| NFR-2 | 错误脱敏 | 沿用 `to_public_error`，不泄露 stderr/路径/凭据 |
| NFR-3 | 并发安全 | 与 HTTP 共享同一任务队列与 worker，不新增并发风险 |
| NFR-4 | 可测试性 | 复用 FakeRunner 与 sync 模式，无 GPU 可测 |
| NFR-5 | 向后兼容 | 现有 HTTP API 行为完全不变 |

## 5. 总体架构

```
┌─────────────────── uvicorn 单进程 ───────────────────┐
│                                                       │
│  FastAPI (现有, 核心)                                 │
│   ├── POST /heart-algo/task/start        ← 现有后端   │
│   ├── POST /heart-algo/task/result                     │
│   └── /mcp (MCP Streamable HTTP, 新增挂载) ← 智能体    │
│                                                       │
│   app.state 共享:                                     │
│   ├── store (MySQL / InMemory)                        │
│   ├── task_queue (InProcessTaskQueue)                 │
│   ├── runner (CombinedRunner / FakeRunner)            │
│   └── work_root (TASK_WORK_ROOT)                      │
└───────────────────────────────────────────────────────┘
```

### 5.1 架构原则

1. **复用优先**：MCP 工具直接调用 `app.state.store` / `app.state.task_queue` / `app.state.runner`，与 HTTP 端点走同一执行路径（`_execute`），零逻辑复制。
2. **统一任务空间**：MCP 创建的任务与 HTTP 创建的任务共用 taskId 命名空间，任何一端创建的任务另一端均可查询。
3. **自动继承**：重启恢复、错误脱敏、GPU 资源池、输入物化下载等能力全部自动生效。

## 6. MCP 接口设计

### 6.1 Transport

- 远程部署：**Streamable HTTP**（MCP 当前标准），端点 `http://<host>:<port>/mcp`。
- 本地联调：stdio（开发调试用）。

### 6.2 工具

#### 6.2.1 `diagnose_heart_failure`（P0）

**作用**：主入口。提交诊断任务并等待结果。

**入参**：

```json
{
  "cardiac_ultrasound": [
    {"dcm_type": "PLAX", "dcm_id": "plax-001", "url": "https://files.internal/plax/001.dcm"}
  ],
  "ecg": [
    {"ecg_id": "ecg-001", "url": "https://files.internal/ecg/001.xml"}
  ],
  "wait_seconds": 50
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `cardiac_ultrasound[].dcm_type` | string | 是* | 切面类型，枚举校验（PLAX/A4C/Subcostal/RVOT/MV_EA/AV_Vmax/TR_Vmax/MR_Vmax/LVOT_Vmax/TDI_Medial/TDI_Lateral/TAPSE） |
| `cardiac_ultrasound[].dcm_id` | string | 是* | 任务内唯一标识 |
| `cardiac_ultrasound[].url` | string | 是* | 算法服务器可下载的 DICOM URL |
| `ecg[].ecg_id` | string | 否 | 任务内唯一标识 |
| `ecg[].url` | string | 否 | 算法服务器可下载的 aECG XML URL |
| `wait_seconds` | int | 否 | 内部轮询上限，默认 50，范围 1~120 |

\* `cardiac_ultrasound` 与 `ecg` 至少一项非空。

**返回（成功）**：

```json
{
  "status": "completed",
  "task_id": "mcp-a1b2c3",
  "hf_type": "HFrEF",
  "cardiac_ultrasound": [
    {
      "dcm_id": "plax-001",
      "measurements": {
        "lvef": {"value": 26.75, "name_cn": "左室射血分数(EF)", "unit": "%", "reference": "55–70"},
        "lvedd": {"value": 66.27, "name_cn": "左室舒末径", "unit": "mm", "reference": "35–55"}
      },
      "rois": [{"roi_type": "LVEDD", "points": [{"xPos": 322, "yPos": 213}]}],
      "error": null,
      "skip_reason": null
    }
  ],
  "ecg": [
    {
      "ecg_id": "ecg-001",
      "patient_info": {"age": 72, "sex": "M"},
      "measurements": {"qt": 380, "vent_rate": 63},
      "predictions": [
        {"label": "窦性心律", "probability": 0.860783}
      ]
    }
  ]
}
```

**返回（超时未完成）**：

```json
{
  "status": "processing",
  "task_id": "mcp-a1b2c3",
  "hint": "任务仍在分析中，请调用 get_diagnosis_result 查询结果"
}
```

**返回（失败）**：

```json
{
  "status": "failed",
  "task_id": "mcp-a1b2c3",
  "error": "公开中文错误信息"
}
```

#### 6.2.2 `get_diagnosis_result`（P0）

**作用**：按 `task_id` 查询诊断结果，供主工具超时后由智能体在后续轮次继续轮询。

**入参**：`task_id`（string，必填）

**返回**：与 `diagnose_heart_failure` 相同的三种结构（completed / processing / failed），completed 时附带完整结构化结果。

#### 6.2.3 `list_supported_views`（P1）

**作用**：返回支持的切面类型与指标目录，供智能体校验入参。

**返回**：

```json
{
  "views": [
    {"dcm_type": "PLAX", "metrics": ["lvedd", "lvesd", "lvef", "ivs", "lvpw", "la", "aorta", "aorticroot"]}
  ],
  "metrics": {
    "lvef": {"name_cn": "左室射血分数(EF)", "unit": "%", "reference": "55–70"}
  }
}
```

数据来源：`metric_catalog.py`（单一数据源）。

### 6.3 资源

| Resource URI | 内容 | 说明 |
|---|---|---|
| `heart-algo://diagnosis/{task_id}` | 完整报告 JSON（含原始 reports 结构） | 智能体可随时读取；仅返回该任务对服务账号可见的数据 |

### 6.4 Prompt

`heart_failure_interpretation`：内置心衰解读模板，包含：

- HFrEF / HFmrEF / HFpEF 的 LVEF 阈值（<40 / 40–49 / ≥50）；
- ECG 多标签概率**相互独立、总和不要求等于 1**；
- LVEF 由 LVEDD/LVESD 经 Teichholz 公式估算，与金标准可能有偏差；
- 模型输出为辅助分析，不能替代医生诊断；
- 单张心超可能含 `error`/`skip_reason`，不能仅凭任务成功判定每张输入都成功。

## 7. 关键设计细节

### 7.1 长任务处理（两段式）

GPU 推理耗时几十秒至数分钟，而 MCP 客户端对单次工具调用通常有约 60s 超时。因此：

1. `diagnose_heart_failure` 内部轮询最多 `wait_seconds`（默认 50s，低于常见客户端超时）；
2. 未完成时返回 `{status:"processing", task_id}`，并在返回描述中明确指示智能体调用 `get_diagnosis_result`；
3. 智能体在后续轮次继续轮询直至完成（符合 agent 多轮调用行为）。

### 7.2 输入物化复用

MCP 工具收到的 `url` 原样放入 `ImgItem.imgPath`，由现有 `CombinedRunner → InputMaterializer` 自动下载：

- 支持 `allowlist` 精确白名单 / `private_network`（RFC 1918）两种策略；
- 内置 SSRF 防护（DNS 重绑定校验、实际 TCP 对端校验）；
- 单文件上限 512 MiB、超时 60s、`.part` 原子写入；
- 可选 Bearer Token 服务间鉴权。

MCP 层不做二次下载实现，仅透传 URL。

### 7.3 结果结构化

MCP 返回前必须将 `reportResult`（JSON 字符串）二次解析并展平：

- `measurements` 展开为 `{指标键: {value, name_cn, unit, reference}}`；
- ECG `predictions`、`patient_info`、`measurements` 直接置于顶层；
- `rois` 保持 `{roi_type, points}` 结构；
- 单图失败以 `error`/`skip_reason` 表达，任务级失败以 `status:"failed"` + `error` 表达。

智能体无需理解 `reportResult` 二次解析，这是适配层的核心价值。

### 7.4 内部字段映射

| MCP 对外 | 内部实现 |
|---|---|
| `task_id` | 自动生成 `mcp-<uuid>`，作为 `taskId` |
| （隐藏） | `requestId` 自动生成 |
| （隐藏） | `sysUserId` 使用配置的服务账号（如 `mcp-service`） |
| `dcm_type` / `url` | 展平为 `ImgItem(imgId, imgPath=url, imgType, dcmType)` |

## 8. 安全设计

| 项 | 方案 |
|---|---|
| 鉴权 | MCP 协议无鉴权标准；`/mcp` 端点由部署层网关 Token 保护（或挂载路径加 Bearer 校验中间件）。文档声明"内网 + 网关"边界 |
| 输入校验 | `dcm_type` 在 MCP 层做枚举拦截（补 API 层缺口）；URL 合法性交给 `InputMaterializer` 策略 |
| 错误脱敏 | 工具异常统一走 `to_public_error`，仅返回公开中文消息，不泄露 stderr/路径/凭据 |
| 并发限流 | 与 HTTP 共享同一队列，`worker_count` 天然兜底并发 |
| 数据隔离 | Resource/查询均按服务账号 `sysUserId` 校验，仅返回本账号可见任务 |

## 9. 依赖与实施步骤

### 9.1 依赖

| 依赖 | 说明 |
|---|---|
| `mcp`（官方 Python SDK） | 使用 `mcp.server.fastmcp.FastMCP`，提供 `streamable_http_app()` 挂载能力 |
| 现有代码 | `api.py`（`_execute`、store）、`main.py`（`build_app`）、`combined_runner.py`、`input_materializer.py`、`metric_catalog.py`、`algorithm_errors.py` |

> 注意：当前项目**没有 requirements.txt / pyproject.toml**，需补充依赖清单文件。

### 9.2 实施步骤

| 步骤 | 涉及文件 | 内容 |
|---|---|---|
| 1 | 新建 `mcp_server.py` | `build_mcp(app)`：3 工具 + 1 资源 + 1 prompt；复用 `_execute`/store；结果展平与内部字段映射 |
| 2 | 修改 `main.py` | 新增 `--mcp` / `MCP_ENABLED` 开关；`app.mount("/mcp", build_mcp(app).streamable_http_app())` |
| 3 | 新建 `requirements.txt` | 记录 `mcp` 及现有运行依赖 |
| 4 | 新建 `test/test_mcp.py` | 直接调用工具函数 + `mcp.call_tool` 端到端测试（复用 FakeRunner/sync fixture） |
| 5 | 更新文档 | 新增 MCP 联调章节（工具列表、agent 调用示例、鉴权说明） |

## 10. 验收标准

### 10.1 功能验收

- [ ] `diagnose_heart_failure` 提交心超 URL 任务 → 返回 `status:"completed"` + 结构化测量值与 `hf_type`；
- [ ] 提交 ECG URL 任务 → 返回 `patient_info`/`measurements`/`predictions`；
- [ ] 提交混合任务 → 心超与 ECG 结果同时返回；
- [ ] 任务超过 `wait_seconds` 未完成 → 返回 `status:"processing"` + `task_id`；
- [ ] `get_diagnosis_result(task_id)` 可继续取回上述任务结果；
- [ ] `list_supported_views` 返回切面与指标目录；
- [ ] Resource `heart-algo://diagnosis/{task_id}` 可读取完整报告；
- [ ] 单张心超失败 → 该图含 `error`，其他图与汇总结果正常；
- [ ] 任务级失败 → `status:"failed"` + 公开错误信息，无内部细节泄露；
- [ ] 无效 `dcm_type` → 在 MCP 层直接拒绝。

### 10.2 非功能验收

- [ ] 单次工具调用在 `wait_seconds` 内返回，不超客户端超时；
- [ ] 现有 HTTP API 全部测试通过（`test_api.py` 等无回归）；
- [ ] 无 GPU 环境下（FakeRunner + sync）可完成全部 MCP 测试；
- [ ] MCP 创建的任务可通过 HTTP `/result` 查询（统一任务空间验证）。

### 10.3 联调验收（真实环境）

- [ ] MCP Inspector / Claude Desktop 连接 `http://<host>:<port>/mcp` 成功；
- [ ] 使用真实 DICOM + ECG XML URL 完成端到端诊断；
- [ ] 错误场景（文件不存在、格式不支持）返回脱敏错误。

## 11. 风险与开放问题

| 编号 | 风险/问题 | 影响 | 缓解 |
|---|---|---|---|
| R-1 | 部分 MCP 客户端超时 <50s | 主工具可能提前超时 | 两段式设计兜底；`wait_seconds` 可配置 |
| R-2 | 进程内队列不支持多实例扩展（既有约束） | 多实例部署时任务可能落在不同进程 | 本期明确单实例部署；后续如需扩展另立项（Redis/MQ） |
| R-3 | MCP 无鉴权标准 | `/mcp` 可能被未授权访问 | 部署层网关 Token；文档声明安全边界 |
| R-4 | 智能体可能提交超大/恶意 URL | SSRF/资源消耗 | 复用 `InputMaterializer` 白名单/私有网络策略 + 512MiB 上限 |
| R-5 | `mcp` SDK 版本演进 | API 变动 | 锁定版本；验收时用 MCP Inspector 回归 |

## 12. 版本记录

| 版本 | 日期 | 修改说明 |
|---|---|---|
| v1.0 | 2026-08-12 | 初稿：基于"LLM 智能体消费 + 同机同进程"决策，明确 MCP 工具/资源/安全/验收设计 |
