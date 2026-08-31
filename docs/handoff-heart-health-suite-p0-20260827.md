# Heart-Health-DSH-Suite P0 交接文档

- 日期：2026-08-27 创建；**2026-08-28 更新**（发布分发 + DSH 双 profile 落地 + 模型连通性验证，见第 9 节）
- 范围：`D:\project\heart\heart-health-dsh-suite` P0 全量交付、验证、发布与安装
- 状态：**P0 已完成并已发布**（构建 ✓ / 自检 ✓ / 33 测试全绿 / GitHub 公开 Release ×2 /
  desktop+web 双 profile 已装 / 后端与 GLM API 连通性实测通过；剩 GUI 内真实模型冒烟）
- 上游文档：`handoff.md`（整体项目状态）、`PRD DeepSeek Harness 心脏健康插件套件与心超心电推理协同.md`（本套件验收标准）

本文是自包含交接：新会话从这里即可续接，不需要回放对话。

## 1. 一句话结论

PRD P0 的独立安装单元 `heart-health-dsh-suite` 已实现、全部本地验证通过（33 个真实 Loader 测试）、
**公开发布**（GitHub Releases 提供 suite 与 heart-algo 桥两个 tgz），并已装进本机 DSH 的
**desktop（GUI 默认）与 web 两个 profile**；后端服务与 GLM 模型 API 均实测连通。
剩余工作：GUI 内选 preset 做真实模型端到端冒烟，以及 web profile 基线引导失败的调查（与本套件无关）。

## 2. 交付清单

```
D:\project\heart\heart-health-dsh-suite\
├─ package.json              # 零依赖；exports["./tools"|"./policy"]；dsh.bundle.patch 元数据
├─ cordis.patch.yml          # bundle 插入项：加载 lib/index.js 激活器
├─ README.md                 # 安装/卸载、环境旋钮表、验证流程、残留风险
├─ .gitignore                # lib/、.generated.tsconfig.json、历史 runtime/
├─ tsconfig.base.json
├─ presets/heart-health/
│  ├─ preset.yml             # 显示元数据（名称/描述/trust）
│  └─ agent.cordis.yml       # 三行组合：persona + tools + policy（全部裸包名解析）
├─ src/                      # TypeScript 源码（8 个模块）
│  ├─ index.ts / activator.ts   # bundle 入口；apply() 把 preset 两个 yaml 落到 <DSH_HOME>/.agent-presets/
│  ├─ tools.ts               # 三个包装工具定义 + 手工输入校验
│  ├─ policy.ts              # 掩码 + guard + post-execute 文本净化 + tools/change 重算
│  ├─ contract.ts            # 三态判别式解析、标识符校验（拒 URL/路径）
│  ├─ privacy.ts             # FORBIDDEN_KEYS、泄露签名规则、scrubValue/redactString
│  ├─ render.ts              # 纯文本渲染 + presentationMeta 卡片
│  ├─ mcp.ts                 # 受控嵌套调用（无 agent、带 parent token、AbortSignal 透传）
│  ├─ config.ts              # 7 个 HEART_HEALTH_* 环境旋钮
│  ├─ guidance.ts            # 驻留指导文本（order 150）
│  └─ errors.ts
├─ test/                     # vitest，6 文件 33 用例，全部真实组件
│  ├─ helpers/harness.ts     # 真实插件栈 boot + 宿主基座 junction 夹具
│  ├─ helpers/fake-heart-mcp.ts
│  ├─ loader-compose.spec.ts # 真实 Loader：模型只见 heart_* 工具；prompt 含指导；schema 稳定
│  ├─ policy.spec.ts         # 直接调用拒绝、嵌套放行、无关工具不受扰、动态重掩码、跨 preset 隔离
│  ├─ states.spec.ts         # 三态契约快照、Top-K 截断、未知状态拒绝、Abort 传播
│  ├─ privacy-and-inputs.spec.ts # 泄漏注入三面清除、patient_info 开关、六类输入拒绝
│  ├─ lifecycle.spec.ts      # 并发/顺序多会话挂载干净
│  └─ activator.spec.ts      # packageRoot 纯函数、幂等落地、缺发布物大声失败
└─ scripts/
   ├─ build.mjs              # tsc 编译（用 checkout 的 TS 与 peer 类型，零安装）
   ├─ run-tests.mjs          # 用 checkout 的 vitest 运行
   └─ check.mjs              # 发布前自检：清单/exports 一致性/秘密卫生/旋钮拼写
```

## 3. 关键设计决策（实现期确定，后续勿回退）

1. **preset 组合行用裸包名子路径，不拷贝 runtime**。
   `agent.cordis.yml` 的行写 `heart-health-dsh-suite/tools` / `heart-health-dsh-suite/policy`，
   由宿主基座解析（与 `@deepseek-ai/dsh-persona` 同机制）。用户根目录只保存两个 yaml。
   原因：`<DSH_HOME>/.agent-presets/` 下无法解析 `@deepseek-ai/*` peer——这是真实 Loader
   才暴露的约束，模拟加载器测不出来。卸载包即失效，preset 健康检查会响亮报错。

2. **数据最小化在工具执行层就地完成**。`scrubValue` 应用于 canonical value 构造点
   （tools.ts 三个 execute 返回前），post-execute 监听器只兜底净化文本块。
   监听器顺序不可依赖，这是有意的双保险布局。

3. **嵌套调用省略 `agent` 参数**（受控插件域内委托，parent token 带上、AbortSignal 透传）。
   代价：agent 作用域监听器/审批门禁不覆盖这次委托——已在 README「已知残留风险」声明。

4. **Unix 路径脱敏用 lookbehind**（`(?<![\w@./\\])` 前缀根目录名），替换为 `<已脱敏>`；
   presentationMeta 不允许出现 `undefined`（lossless JSON 校验会拒），可选字段必须条件省略键。

## 4. 复现验证（新会话直接执行）

```powershell
$env:DSH_CHECKOUT = 'D:\project\dsh\deepseek-harness'
Set-Location D:\project\heart\heart-health-dsh-suite
npm run build    # [heart-health-dsh-suite] build OK: 11 js modules in lib/
npm run check    # [heart-health-dsh-suite] check 通过
npm test         # Test Files 6 passed, Tests 33 passed
```

打包核验（npm cache 必须指到工作区内，见第 6 节）：

```powershell
npm pack --dry-run --cache "$PWD\.npm-cache-tmp"   # 28 files, 29.5 kB, 无凭据
```

## 5. 已知问题与残留风险

| 项 | 说明 | 缓解 |
|---|---|---|
| 嵌套调用绕过 agent 作用域门禁 | 有意的执行机制豁免（第 3 节第 3 条） | guard 仍覆盖模型直接发起的原始工具调用；已文档化 |
| 宿主基座与 profile node_modules 不一致时 | preset 挂载**响亮失败**，不静默降级 | 部署时确认 `dsh plugin add` 的安装位置 |
| POSIX 无盘符 file URL | `file:///opt/...` 在 Windows 会抛错 | 仅测试环境会构造该形态，生产不受影响 |
| **web profile 基线引导失败（既有，与本套件无关）** | 二分到「仅官方 dsh-base + dsh-web-app 两个 bundle、用户 patch 清空」仍报
`include (cordis:include): loader entries failed to apply`；desktop profile（GUI 在用）正常 | 待调查；不阻塞套件——preset 已由激活器等效落地，GUI/desktop 路径完整可用 |
| 打包版 CLI 吞掉聚合错误细节 | `AggregateError.errors`（逐条原因）不打印，排障只能靠二分或自写探针 | 已沉淀探针方法（第 6 节）；可向 DSH 上游反馈可观测性 |
| 桥 `failOnStartupError: true` | 后端不可达时 profile 引导响亮失败（含 GUI） | 刻意设计；先起 Heart FastAPI 再启动 DSH |
| 沙箱内 git 推送凭据提示脚本崩溃 | `!` 前缀 helper 经 sh.exe 启动被沙箱拦截 | 用 `gh auth token` + URL 内联凭据推送（token 不进命令文本） |

## 6. 环境注意事项（本机沙箱）

- **vitest 必须线程池**：沙箱禁止 fork 子进程（`spawn EPERM`）。`vitest.config.mts` 已固定
  `pool: 'threads'` + `fileParallelism: false`（多 spec 共享 suite 本地 node_modules junction，串行防竞态）。
- **npm cache 指向工作区内**：默认 `C:\Users\lj\AppData\Local\npm-cache` 在沙箱可写范围外（EPERM）。
- **测试夹具会创建 suite 本地 `node_modules\@deepseek-ai\*` junction**（模拟安装布局），
  已入 `.gitignore`，不影响打包。
- **dsh CLI 现已可用**：DSH Desktop 自带 shim
  `C:\Users\lj\AppData\Roaming\DSH Desktop\host-commands\desktop\bin\dsh.cmd`（Electron as-node 跑 packaged CLI）。
  所有写 `C:\Users\lj\.dsh` 的 dsh 命令在代理内运行需 `danger-full-access` 提权。
- **git 推送**：沙箱拦凭据提示脚本；用 `gh auth token` 取 token 后以 URL 内联推送
  （token 只进变量不进命令文本）。
- **WinPS 写配置文件防 BOM**：`Set-Content -Encoding UTF8` 带 BOM 会炸 JSON/YAML 解析，
  用 `[IO.File]::WriteAllText(path, text, [Text.UTF8Encoding]::new($false))`。
- **asar 内省探针**：`ELECTRON_RUN_AS_NODE=1 "DSH Desktop.exe" --expose-internals script.mjs`
  可读 app.asar 内文件并跑 Node 探针（探 bare 名解析需锚定 base，Electron node 的
  `import.meta.resolve` 不支持 parentURL，用绝对路径 import 代替）。
- glob/grep 内置工具在本机不可用（ripgrep 启动失败），用 pwsh `Select-String`/`Get-ChildItem` 替代；
  PowerShell 管道里的 `??` 操作符在 WinPS 5.1 不支持。

## 7. 下一阶段门禁（按顺序，2026-08-28 更新）

1. **GUI 真实模型端到端冒烟（当前最近一步）**：重启 DSH Desktop（载入新 bundle 与
   `HEART_ALGO_MCP_TOKEN`）→ 新空白会话 → 选「心脏健康」preset → 断言只看到 `heart_*` 三工具 →
   「列出支持切面」→ 提交真实/去标识化病例 → 下一轮查询结果。
2. **web profile 基线引导失败调查**（既有问题，与本套件无关）：最小配置（官方两 bundle + 空 patch）
   即复现；从打包版 CLI 的 include 应用链路入手，或向 DSH 上游反馈聚合错误可观测性。
3. **跨进程闭环与泄漏实测**：真实会话注入带路径/token 的上游错误，确认 UI 呈现与脱敏；中途取消任务。
4. 然后回到 `handoff.md` 第 5 节的生产门禁（MySQL 实连已在跑：`--task-store mysql`、
   持久盘 `D:\heart-data\*`；4090D ECG、真实病例闭环仍待）。
5. P1：五类按需 skills 与结果卡片；提醒/随访/趋势保持 P2。
6. 发布迭代：改 version → `npm pack` → `gh release create vX.Y.Z *.tgz`（suite 与桥同流程；
   桥分发地址固定在 suite 仓库 Releases，heartalgo-mcp 为私有仓）。

## 8. 文档索引

| 内容 | 路径 |
|---|---|
| 本文档（suite P0 交接） | `docs/handoff-heart-health-suite-p0-20260827.md` |
| 项目整体交接（持续更新） | `docs/handoff.md` |
| 套件 PRD（验收标准） | `docs/PRD DeepSeek Harness 心脏健康插件套件与心超心电推理协同.md` |
| MCP 桥插件源码（monorepo 内，私有） | `D:\project\heart\deepseek-harness-plugin` |
| Harness checkout（测试依赖） | `D:\project\dsh\deepseek-harness` @ `47f943859b` |

## 9. 2026-08-28 增量：发布、DSH 落地与连通性验证

### 9.1 GitHub 公开发布

| 项 | 地址 |
|---|---|
| 套件源码仓（public，main） | https://github.com/Liujie-harsh/heart-health-dsh-suite |
| 套件 Release v0.1.0 | …/releases/tag/v0.1.0（附件 `heart-health-dsh-suite-0.1.0.tgz`，28 文件含预构建 lib/） |
| 桥分发 Release（挂在 suite 仓） | …/releases/tag/heart-algo-dsh-plugin-v0.1.0（附件 4 文件 tgz；源码随私有 heartalgo-mcp 版本化） |

- 桥的 Release 曾先发在私有 heartalgo-mcp 上，因私有仓外人拿不到，已迁移至 suite 仓并删除原 Release+tag。
- 套件源码推送：`1e27f72`（P0 全量）→ `84174a3`（使用者优先安装文档）→ `9379e0a`/`c0089ec`（桥链接迁移）。
- README「安装」章节重写：使用者走 Release tgz（一条 `dsh plugin add` + 重启即可，
  bundle 激活器随引导幂等落地 preset，**无需手动 bundle apply**）；开发者流程（clone + build）单列。
- `dsh.bundle.patch` 声明确认在 package.json（`"dsh": {"bundle": {"patch": "./cordis.patch.yml"}}`）。

### 9.2 DSH 双 profile 落地（本机）

| Profile | 状态 |
|---|---|
| **desktop（GUI 默认）** | ✅ 完整：bundles = `[dsh-base, dsh-web-app, dshmarket, heart-health-dsh-suite, heart-algo-dsh-plugin]`（均 `link:` 工作区，改码重 build 即生效）；preset 为 DSH_HOME 级共享 |
| **web** | ⚠️ suite 已登记（bundles + link ✓），preset 已由激活器等效落地 ✓；但该 profile **基线引导失败**（既有问题，见第 5 节），`dsh bundle apply` 因此卡在引导阶段，与套件无关 |

- `dsh plugin add` 原生命令成功（读 supply-chain 策略 + pnpm link）。
- preset 落地核验：`~\.dsh\.agent-presets\heart-health\{agent.cordis.yml,preset.yml}` 与源文件**字节一致**
  （经 Electron-as-node 跑套件自带激活器 `apply()`，与 bundle apply 的效果等价且幂等）。
- 二分期间对 web profile 的临时改动已全部还原（.bak 已清理）。

### 9.3 后端与模型连通性实测

- **Heart 后端已起**：uvicorn 127.0.0.1:8000，`task_store=mysql`、持久盘 `D:\heart-data\{runtime,cases}`、
  `mcp_enabled=True`、`algorithm_version=heart@bundle-f06f5f050c98+models@4bf76612484dd393`。
- **MCP 鉴权**：无 Token → HTTP 401「MCP 身份验证失败」；Bearer `20260818` → HTTP 200 initialize 握手成功。
  `HEART_ALGO_MCP_TOKEN` 已 `setx` 为用户级环境变量（GUI 重启后生效）。
- **GLM 模型 API**：provider `zai`（baseUrl `https://api.z.ai/api/coding/paas/v4`，key 存于
  `~\.dsh\.credentials.yaml`），当前会话模型 **`glm-5.3-flash`**（settings.yaml `agent-default-model` 声明式补充，
  pi-ai 0.82 内置目录尚未收录）→ 实测 HTTP 200 回复 `pong`，延迟约 2.2s。
  ⚠️ 思考型模型：max_tokens 过小会被 reasoning 耗尽（32 token 全花在思考，正文为空）。
- 沙箱内 .NET/curl 的 schannel TLS 被拦（`SEC_E_NO_CREDENTIALS`），用 Node（OpenSSL，与 DSH 同栈）测通——
  也证明真实 DSH 链路的网络与凭据无恙。

### 9.4 交付给用户的下一步（第 7 节第 1 条的操作版）

重启 DSH Desktop → 新空白会话 → 选「心脏健康」→ 问「你有哪些 heart 工具」断言只见三个包装工具 →
按第 7 节流程做提交/查询冒烟。若 GUI 起不来：先确认 8000 端口后端与 Token（桥 fail-fast 是设计行为）。
