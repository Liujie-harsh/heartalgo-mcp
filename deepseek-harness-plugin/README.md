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

- Node.js 20 或更高版本，以及可用的 DeepSeek Harness CLI；
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

插件将单次工具调用超时设为 130 秒，以覆盖服务端允许的最大
`wait_seconds=120` 以及少量传输开销；服务默认等待 50 秒，未完成时会返回
`processing`，随后应调用查询工具继续轮询。

## 可用工具

连接成功后，模型会看到以下工具：

- `mcp__heart-algo__diagnose_heart_failure`：提交心超/ECG 诊断任务；
- `mcp__heart-algo__get_diagnosis_result`：按 `task_id` 查询长任务；
- `mcp__heart-algo__list_supported_views`：查询支持的切面与指标。

例如可让 Agent 执行：

> 调用心衰算法分析这个 ECG URL；如果仍在处理中，使用返回的 task_id 继续查询，
> 最后按结构化结果总结，并明确说明仅供临床辅助。

当前 Harness MCP bridge 只把 MCP Tools 注册到 `ctx.tools`；服务端 Resource 和 Prompt
仍保留给支持这些能力的其他 MCP 客户端，本插件不会伪造对应工具。

## 验证与排错

```powershell
python -m pytest test/test_deepseek_harness_plugin.py -q
dsh --profile web --dump-config
```

在 dump 结果中应能找到 `heart-algo-mcp`。若 Harness 启动时提示连接失败，请依次检查：

1. 算法服务是否以 `--mcp` 或 `MCP_ENABLED=true` 启动；
2. `HEART_ALGO_MCP_URL` 是否能从 Harness 主机访问；
3. 两端 Token 是否完全一致；
4. 反向代理是否透传 MCP 的请求头和流式响应。

卸载命令：

```powershell
dsh plugin --profile web remove heart-algo-dsh-plugin
```
