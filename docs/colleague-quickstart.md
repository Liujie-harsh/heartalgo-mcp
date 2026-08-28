# 同事快速启动（后端服务）

收到本源码包后，按以下步骤把 heart-algo 后端跑起来，并与 DSH 的 heart 插件链路对接。

## 1. 环境准备（二选一）

```powershell
# 方式 A：conda（推荐，环境规格与开发机一致）
conda env create -f env.yml
conda activate heart

# 方式 B：pip
pip install -r requirements.txt
```

## 2. 体验模式（零外部依赖：无 torch / 无 MySQL / 无 Token）

```powershell
python main.py --fake --task-store memory --mcp --host 0.0.0.0 --port 8000
```

- `--fake`：使用 FakeRunner，不加载 PLAX/ECG 真实模型
- `--task-store memory`：任务存内存，不依赖 MySQL
- `--mcp`：开启 `/mcp` Streamable HTTP 端点（fake 模式下允许匿名）

看到 `Uvicorn running on http://0.0.0.0:8000` 即成功。

## 3. 与 DSH 插件链路对接

在运行 DSH Desktop 的机器上：

```powershell
# 两个 Release 包（见 heart-health-dsh-suite 仓库 Releases）
dsh plugin --profile desktop add .\heart-algo-dsh-plugin-0.1.0.tgz
dsh plugin --profile desktop add .\heart-health-dsh-suite-0.1.0.tgz

# 指向本后端（fake 模式未设 Token，DSH 侧也不用设）
setx HEART_ALGO_MCP_URL "http://<本机IP>:8000/mcp"

# 重启 DSH Desktop → 新空白会话 → agent preset 选「心脏健康」
```

验证：会话内问「你有哪些 heart 工具」→ 应只见
`heart_submit_diagnosis` / `heart_get_diagnosis_result` / `heart_list_supported_views`。

## 4. 真实推理模式（需要额外资产，不在本包内）

| 资产 | 说明 |
|---|---|
| Measurement 工程 | PLAX 测量脚本目录（--measurement-script-dir） |
| ecg-fm 工程 + 权重 | --ecg-project-dir / --ecg-checkpoint |
| MySQL 8 | 任务持久化（--task-store mysql 时必需） |
| torch 环境 | 真实 runner 依赖 |

去掉 `--fake` 并按需设置：

```powershell
$env:DATABASE_URL = "mysql+pymysql://<user>:<pwd>@<host>:3306/<db>?charset=utf8mb4"
$env:MCP_SHARED_SECRET = "<共享密钥>"   # 生产 MCP 必需；DSH 侧 HEART_ALGO_MCP_TOKEN 与之一致
python main.py --measurement-script-dir <dir> --measurement-python <python> `
  --ecg-project-dir <dir> --ecg-checkpoint <pt> --ecg-python <python> `
  --task-work-root <dir> --task-store mysql --case-storage-root <dir> `
  --host 0.0.0.0 --port 8000
```

完整生产口径（单实例、持久盘、鉴权、备份）见 `docs/server-validation-checklist.md` 与 `docs/handoff.md` 第 4 节。

## 5. 常见问题

- **DSH 起不来**：桥是 fail-fast 设计，99% 是后端没起或 URL/端口不通；先 `curl http://<ip>:8000/mcp` 看是否有响应（401 也算通）。
- **会话里没有 heart_* 工具**：preset 没选「心脏健康」，或两个 tgz 没装进同一个 profile。
- **看到 mcp__heart-algo__* 原始工具**：说明 preset 未生效（策略未挂载）。
- 病例上传走病例 HTTP API（`case_api.py`，`/cases` 系端点），模型只接受 `case_id`/`asset_ids`。
