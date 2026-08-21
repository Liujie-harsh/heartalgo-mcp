# 第一阶段：非 Agent 可靠闭环

本阶段提供一个不依赖 Agent Harness 的完整链路：创建病例、上传心超/ECG、提交算法、查询结构化结果、临床复核。同时暴露独立 MCP，后续 dsh、LangGraph、Dify 等只需作为客户端接入。

## 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/heart-algo/portal` | 无前端依赖的联调门户 |
| `POST` | `/heart-algo/cases` | 幂等创建病例 |
| `GET` | `/heart-algo/cases/{case_id}` | 查询病例资产和任务记录 |
| `POST` | `/heart-algo/cases/{case_id}/assets` | multipart 上传 DICOM 或 ECG XML |
| `POST` | `/heart-algo/cases/{case_id}/diagnoses` | 提交病例资产到共享算法队列 |
| `GET` | `/heart-algo/cases/{case_id}/diagnoses/{task_id}` | 查询展平后的结构化报告 |
| `POST` | `/heart-algo/cases/{case_id}/diagnoses/{task_id}/review` | 追加临床复核记录 |
| MCP | `/mcp` | Streamable HTTP MCP 端点 |

MCP 提供三个工具、一个资源模板和一个解释提示词：

- `diagnose_heart_failure`
- `get_diagnosis_result`
- `list_supported_views`
- `heart-algo://diagnosis/{task_id}`
- `heart_failure_interpretation`

MCP 和 HTTP 病例接口调用同一个病例存储、任务存储、队列和 runner，不复制推理逻辑。MCP 只接受已上传的 `case_id`/`asset_id`，不接受模型任意生成的下载 URL。工具契约使用 `snake_case`；调用者不能传 `sys_user_id` 或 `request_id`，服务端自动生成请求 ID，并使用 `MCP_SERVICE_USER_ID`（缺省 `mcp-service`）作为受限服务账号。

## 本地联调

```powershell
python -m pip install -r requirements-dev.txt
python main.py --fake --task-work-root .runtime --case-storage-root .runtime\cases --mcp
```

打开 `http://127.0.0.1:8000/heart-algo/portal`，至少选择一个 DICOM 或 ECG XML 后即可完成全流程。

生产启动时去掉 `--fake`，并配置既有 Measurement/ECG-FM 参数。生产环境必须使用持久化任务存储：

```powershell
$env:TASK_STORE_BACKEND = "mysql"
$env:DATABASE_URL = "mysql+pymysql://..."
$env:TASK_WORK_ROOT = "G:\heart-algo\runtime"
$env:CASE_STORAGE_ROOT = "G:\heart-algo\cases"
$env:CASE_AUTH_REQUIRED = "true"
$env:CASE_TRUSTED_PROXY_SECRET = "<网关与算法服务之间的高强度共享密钥>"
$env:MCP_ENABLED = "true"
$env:MCP_SERVICE_USER_ID = "heart-agent-service"
$env:MCP_SHARED_SECRET = "<Agent Harness 专用高强度 Bearer 密钥>"
python main.py
```

`MCP_ENABLED` 缺省为关闭；命令行缺省只监听 `127.0.0.1`。生产模式不允许内存任务存储，只有明确设置 `ALLOW_VOLATILE_CASE_TASKS=true` 才能绕过该保护用于临时调试。

## 上传约束

- 心超：`modality=CARDIAC_ULTRASOUND`，必须提供受支持的 `dcmType`；当前入口要求 DICOM Part 10 文件头含 `DICM`。
- 心电：`modality=ECG`，文件内容必须以 XML 标记开头，不允许设置 `dcmType`。
- `assetId` 可省略，由服务端生成；指定时用于上传重试幂等。
- 默认单文件上限 512 MiB，可通过 `CASE_ASSET_MAX_BYTES` 调整。
- 上传文件名不写入病例元数据、任务结果或磁盘文件名；服务端返回 SHA-256 和字节数用于追溯。
- 同一用户内，诊断 `requestId` 在病例范围内幂等；不同病例不会错误复用同一任务。相同 `requestId` 如果改换 `assetIds` 会返回 409，不会返回旧输入的报告。

## 结构化结果

完成结果包含：

- `hfType`
- 心超逐图 `measurements`、`rois`、`error`、`skipReason`
- ECG `patientInfo`、`measurements`、`predictions`
- 输入文件 SHA-256/大小
- `algorithmVersion`（推理完成时读取并持久化 `ALGORITHM_VERSION`，升级后查询旧任务不会改写版本）
- `reviewStatus`、`requiresClinicianReview` 和当前 `review`

临床复核只允许在任务完成后提交，历史记录以追加方式保存在病例元数据中。只有 `approved` 会解除待复核标记；`rejected` 仍保持 `requiresClinicianReview=true`。

## 部署边界

生产模式下病例 HTTP API 要求网关同时注入 `X-Authenticated-User` 和 `X-Auth-Proxy-Secret`，并拒绝请求参数中的身份覆盖已验证主体。网关必须删除客户端同名头后再注入，算法服务仍不能直接暴露公网。独立复核人还必须具有 `X-Authenticated-Roles: cardiology-reviewer`，且病例所有者不能自我批准；这些头不在浏览器 CORS allowlist 中。MCP 仅允许受信 Agent Harness 访问，并使用独立的最小权限服务账号。病例创建时会把配置的 `MCP_SERVICE_USER_ID` 写入病例级 ACL，因此服务账号可以调用算法但不会变成病例所有者。

当前文件病例存储用进程内锁保证原子更新，因此这一阶段必须以**单应用实例、单 Uvicorn worker**运行；不要使用 `--workers`。生产启动会取得 `CASE_STORAGE_ROOT/.instance.lock` 的操作系统排他锁，第二个共享该目录的实例会直接拒绝启动。扩容前需把病例元数据迁移到带事务的共享数据库。任务创建采用“先持久化病例提交意图、再创建队列任务”的顺序，启动时会补建预留但未创建的任务；已创建的排队任务继续使用既有任务存储恢复机制。

病例目录、任务工作目录和 MySQL 应纳入同一备份、保留期和访问审计策略。所有结果均为辅助分析，不能跳过临床复核。
