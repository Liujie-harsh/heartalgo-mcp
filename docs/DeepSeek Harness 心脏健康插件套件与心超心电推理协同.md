## PRD: DeepSeek Harness 心脏健康插件套件与心超/心电推理协同

## Problem Statement

当前心衰算法服务已经完成病例创建、心超 DICOM 与 ECG XML 上传、异步推理、结构化结果、临床复核和持久化可靠闭环，也已经通过 MCP 暴露诊断提交、结果查询和支持切面查询工具。现有 DeepSeek Harness bundle 能把这些 MCP 工具注册到模型，但它仍只是通用协议桥：模型会直接看到原始 MCP 工具，需要自行理解病例先行、资产选择、异步任务状态、心超测量值、ECG 多标签概率、模型版本、临床复核状态和安全边界。

临床人员和业务操作员不应学习底层 MCP 命名、任务状态机或 Harness 插件机制，才能完成一次心脏健康分析。与此同时，通用 Agent 可能把 ECG-only 结果误写成心衰分型、把 completed 误解为每张心超都成功、忽略 pending 临床复核、虚构缺失指标、把概率总和当成单分类分布，或者在模型上下文中暴露不必要的患者数据。

需要一套适配心脏健康场景的 DeepSeek Harness 插件组合，与现有心超/心电推理 MCP bundle 协同：提供受控的心脏健康 Agent preset、稳定的领域工具接口、结构化解释、安全约束、可重放的展示和可验证的端到端工作流，同时保持算法服务与 Harness 解耦，不复制推理逻辑，不改变现有 HTTP/MCP 契约。

## Solution

构建一个独立、可安装的 DeepSeek Harness 插件套件 heart-health-dsh-suite，并与现有 heart-algo-dsh-plugin 组合安装到同一 profile。套件不修改 DeepSeek Harness 源码，也不把推理模型搬入 Node 进程；本地 Harness 源码仅作为接口、测试和兼容性基线。

P0 提供以下组合：

- heart-health preset：面向心脏健康的新会话组合，限定可见能力，装载领域指令、包装工具和安全策略；只允许空白会话选择，避免中途改变工具与提示词。
- heart-health guidance：向模型提供心超/ECG 解读规则、任务状态处理、错误与跳过项检查、临床复核要求、禁止补造信息和紧急风险措辞边界。
- heart-health tools：对已有 MCP 工具做稳定包装，暴露 heart_submit_diagnosis、heart_get_diagnosis_result 和 heart_list_supported_views；包装层提取 structuredContent，保留结构化字段，提供稳定输出 schema 和 Harness UI 展示意图。
- heart-health policy：隐藏或阻止模型直接调用底层 mcp__heart-algo__* 工具，只允许包装工具通过受控的嵌套调用访问；拒绝 URL/本地路径作为诊断输入；对模型可见结果执行数据最小化和安全说明。
- heart-health bundle：把上述插件和 preset 作为一个可由 dsh plugin 安装的配置层发布，并继续依赖现有 MCP bundle 提供实际连接。

P0 的用户流程为：业务系统或病例门户先创建病例并上传资产；用户把 case_id 和可选 asset_ids 交给心脏健康 Agent；Agent 提交诊断并获得 task_id；当状态为 processing 时，在后续轮次查询；completed 时按结构化心超、ECG、输入追溯、算法版本和复核状态解释；failed 时只报告公开错误并给出可操作的下一步。

P1 在 P0 稳定后增加按需技能，包括病例分析准备、心超指标解读、ECG 多标签解读、混合结果总结和临床复核摘要；技能只在相关任务中加载，避免把全部医学说明常驻每次请求。P1 还可增强心脏结果卡片和可观测性，但不新增算法结论。

P2 才评估随访提醒、复核待办和纵向趋势。任何会主动触达患者、生成治疗建议或基于阈值自动升级处置的能力必须单独评审，不属于本 PRD 的默认交付。

## User Stories

1. As a 心内科医生, I want 使用自然语言提交一个已登记病例, so that 我不需要理解 MCP 工具命名和内部任务字段。
2. As a 心内科医生, I want 选择病例中的部分资产进行分析, so that 我可以只复核本次关心的心超切面或 ECG。
3. As a 心内科医生, I want 在结果未完成时获得明确的 processing 状态和 task_id, so that 我可以稍后继续查询而不是误判失败。
4. As a 心内科医生, I want 在结果完成后看到心超测量、ECG 预测和心衰分型分别呈现, so that 不同模态的证据不会混在一起。
5. As a 心内科医生, I want 清楚看到每张心超的 error 和 skip_reason, so that 任务成功不会掩盖单张资产失败。
6. As a 心内科医生, I want 清楚看到 requires_clinician_review 和 review_status, so that 未复核结果不会被当作最终临床结论。
7. As a 心内科医生, I want 看到算法版本和输入追溯摘要, so that 我能判断结果来自哪个可追踪发布版本。
8. As a 心内科医生, I want Agent 不补造缺失测量值或患者信息, so that 输出保持可审计。
9. As a 心内科医生, I want ECG 多标签概率被解释为相互独立, so that 概率总和不会被误解为必须等于 1。
10. As a 心内科医生, I want ECG-only 结果不会被强行写成心衰分型, so that 模态能力边界被尊重。
11. As a 心内科医生, I want LVEF 估算方法和局限被说明, so that Teichholz 估算不会被当作金标准。
12. As a 临床复核人员, I want 一键获得结构化复核摘要, so that 我能快速定位异常指标、失败资产和待确认项。
13. As a 临床复核人员, I want Agent 区分算法观察、模型分型和临床判断, so that 责任边界清晰。
14. As a 临床复核人员, I want failed 状态只展示脱敏错误, so that 内部路径、stderr 和凭据不会泄露。
15. As a 病例操作员, I want 得知必须先通过病例 API 或门户上传资产, so that 我不会把文件路径或 URL 直接传给诊断工具。
16. As a 病例操作员, I want 获得无效 case_id、asset_id 或无资产病例的可操作提示, so that 我能修正输入而不是反复重试。
17. As a 病例操作员, I want 查询支持的心超切面和指标, so that 上传前可检查模型支持范围。
18. As a 业务系统集成者, I want 稳定的 heart_* 工具名称和 schema, so that 底层 MCP 命名或渲染变化不会直接影响上层提示。
19. As a 业务系统集成者, I want 工具返回 canonical JSON 而不是要求解析自然语言, so that Code Mode 和自动化流程可可靠消费。
20. As a 业务系统集成者, I want processing、completed 和 failed 成为显式判别状态, so that 工作流分支可测试。
21. As a 业务系统集成者, I want submit 和 status 保持两段式, so that 数分钟 CPU 推理不会占住一次 Agent 工具调用。
22. As a Harness 用户, I want 通过一个 heart-health preset 启动垂域 Agent, so that 通用编码工具不会默认出现在临床会话中。
23. As a Harness 用户, I want 新建会话时选择心脏健康 preset, so that 工具和指导在第一轮之前固定。
24. As a Harness 用户, I want 子 Agent 继承父会话的同一 preset 组合, so that 子任务不会突然获得不同工具或提示。
25. As a Harness 用户, I want 插件热卸载后清理工具、策略和监听器, so that 不会留下幽灵能力。
26. As a Harness 用户, I want MCP 断线时看到明确的连接失败并在恢复后继续使用, so that 我能区分算法失败和连接故障。
27. As a 安全负责人, I want 原始 DICOM/XML 不进入模型上下文或 Harness 会话日志, so that 临床文件只留在算法服务的受控存储中。
28. As a 安全负责人, I want Harness 只传递 case_id、asset_ids、task_id 和最小必要结构化结果, so that 数据暴露面受控。
29. As a 安全负责人, I want Token 只从部署配置读取且不进入提示词、日志和错误, so that 凭据不会因会话重放泄露。
30. As a 安全负责人, I want 模型不能绕过包装层直接调用原始心脏 MCP 工具, so that 数据最小化和安全策略始终生效。
31. As a 安全负责人, I want 所有模型可见指导和工具变化可由会话记录重建, so that 审计与重放一致。
32. As a 运维人员, I want MCP URL、Token、超时、命名空间和故障策略可配置, so that 开发、测试和生产可以使用同一插件。
33. As a 运维人员, I want 配置错误在加载或首次可解析点明确失败, so that 不会静默启动一个没有诊断能力的 Agent。
34. As a 运维人员, I want 查看最终 compose 后的 profile 配置, so that 我能验证 bundle 顺序和生效项。
35. As a 运维人员, I want 插件发布物可离线检查和打包, so that 受限服务器不依赖运行时在线构建。
36. As a 插件开发者, I want 使用 DeepSeek Harness 已有 tools、preset、system-prompt 和 session 扩展点, so that 不需要修改 agent-loop。
37. As a 插件开发者, I want 使用真实 Loader 组合测试而不是只测 apply 函数, so that 发布配置和模块解析错误能被发现。
38. As a 插件开发者, I want 使用假 MCP 服务覆盖成功、处理中、失败和畸形响应, so that 工具包装行为可确定性验证。
39. As a 插件开发者, I want 使用真实算法服务 fake runner 做跨进程验收, so that Harness 与 Python MCP 的协议确实互通。
40. As a 插件开发者, I want 对真实 DeepSeek 模型保留可选 e2e 冒烟, so that 自然语言工具选择和安全说明能被验证。
41. As a 产品负责人, I want P0、P1、P2 范围明确分层, so that 临床安全能力不会与提醒、趋势或治疗建议一起失控扩张。
42. As a 产品负责人, I want 每个阶段有可客观验证的验收标准, so that “Agent 看起来能用”不会替代真实证据。

## Implementation Decisions

- 采用外置 npm bundle 和 Agent preset，不在 DeepSeek Harness 源码仓库内直接开发产品代码；本地源码 checkout 用于接口对齐、源码运行和兼容性测试。
- 目标 Harness 源码基线要求 Node 22.19 或更高的 22.x，或 Node 24 及以上；插件 CI 至少覆盖项目实际部署版本。
- 现有 heart-algo-dsh-plugin 继续只负责 Streamable HTTP MCP 连接；新套件不得复制 MCP transport、算法任务队列或推理逻辑。
- 新套件以 heart-health-dsh-suite 作为一个安装单元，内部包含 guidance、tools、policy 和 preset 贡献，用户不需要逐个拼装。
- bundle 通过 dsh.bundle 声明配置层，所有 Cordis 注册通过 effect 或事件监听器完成并可逆清理。
- heart-health preset 在 agent scope 中贡献领域能力，避免把临床工具和指导暴露给同进程的其他 preset。
- preset 只允许在空白会话选择；已经产生历史的会话不得切换，以保证工具 schema、提示词和会话日志一致。
- P0 包装工具固定为 heart_submit_diagnosis、heart_get_diagnosis_result 和 heart_list_supported_views。
- heart_submit_diagnosis 只接受 case_id 和可选 asset_ids，不接受 URL、文件路径、二进制内容或自由文本病例资料。
- heart_get_diagnosis_result 只接受 task_id，一次调用只查询一次；不在单个工具调用中等待完整 CPU 推理。
- 包装工具通过工具运行时嵌套调用底层 MCP 工具，提取和验证 structuredContent，并返回稳定 canonical JSON；不从 MCP 的渲染文本反解析字段。
- 底层 MCP 工具由 preset 的工具可见性策略隐藏；包装工具在不受 agent 可见性掩码影响的受控内部调用路径上访问底层定义，并关联 parent execution token。
- 若底层 MCP 工具未注册、名称冲突或输出缺少预期字段，包装工具明确失败，不静默降级到自然语言解析。
- 对 processing、completed、failed 三种状态使用判别式输出；completed 保留 hf_type、cardiac_ultrasound、ecg、inputs、algorithm_version、requires_clinician_review、review_status 和 review。
- 工具输出展示与 canonical JSON 分离；Native 模式使用简洁文本，Web UI 使用 generic 结果卡片，卡片从参数和持久化 presentation metadata 纯函数重放。
- guidance 常驻内容仅包含每轮都必须遵守的安全约束和调用状态机；更长的心超、ECG 和复核说明放入按需 skills。
- 指导明确 ECG 为多标签预测、LVEF 为估算、completed 不代表每项成功、模型输出不能替代医生诊断、不得生成治疗方案或补造数据。
- policy 在工具执行前拒绝模型直接调用原始 mcp__heart-algo__* 名称，并允许包装工具的受控嵌套调用。
- policy 在工具执行后执行模型可见结果最小化；禁止暴露文件路径、内部错误、Token 和未批准的扩展字段。
- P0 默认允许年龄和性别等现有最小患者信息，但把是否向模型保留 patient_info 设为显式配置；生产默认值在安全评审时最终确认。
- 所有部署可变项使用验证后的配置字段，包括 MCP namespace、底层工具名、结果字段策略、最大可见 ECG prediction 数量和是否要求 completed 后仍显示临床复核警示。
- 凭据继续由 MCP bundle 从环境或 Harness credentials 服务解析，新套件不读取、存储或回显 Token。
- 原始 DICOM/XML 上传继续走病例门户或 HTTP API；P0 不增加 Harness 文件上传工具。
- 插件不写入算法数据库；case_id、asset_ids 和 task_id 的授权、幂等和数据隔离继续由算法服务负责。
- 会话日志保留包装工具调用、参数、状态和模型可见结果，以满足模型可见即可重建；原始文件和内部 MCP transport 细节不写入会话。
- P1 skills 包含病例诊断准备、心超指标解读、ECG 多标签解读、混合报告总结和临床复核摘要，每个 skill 有明确触发范围和安全说明。
- P1 可增加诊断结果 UI card，但 UI 不能展示 canonical 结果中不存在的推断。
- 不新建 HeartHealth Service capability seam，除非两个以上独立消费者确实需要共享稳定运行时能力；优先使用现有 tools、system prompt、preset 和 session 扩展点。
- 不修改 agent-loop；拦截、策略、展示和上下文注入全部走已公开 Cordis/Harness seam。
- 插件包使用 ESM、严格 TypeScript、明确 peer dependency，并发布预构建产物；Git 安装如需要 prepare，必须文档化 pnpm allowBuilds 风险。
- 发布时固定兼容的 Harness commit 或版本范围，并为破坏性 upstream 变化维护兼容矩阵。
- P0 验收以现有 FakeRunner 和本地 MCP 服务为基础，不要求 GPU；真实模型和真实临床样本分别属于后续联调证据。

## Testing Decisions

- 好测试只验证外部行为：模型能看到哪些工具、工具接受什么输入、返回什么 canonical JSON、会话记录什么、用户看到什么，以及卸载后能力是否消失；不把私有函数名、内部 Map 或调用次数当作主要断言。
- 最高层、首选测试 seam 是一个真实 Loader 组装的 heart-health profile：真实 dsh-mcp-client 连接一个可控 MCP server，脚本化模型发起提交与查询，断言包装工具、模型可见结果和持久化会话日志。
- 跨项目验收 seam 启动算法服务 FakeRunner、病例 HTTP API 和真实 Streamable HTTP MCP，再启动 Harness 测试 profile；测试创建病例、上传最小测试资产、提交、查询到 completed，并验证结构化结果。
- Loader 组合测试必须从发布物入口加载 bundle，验证 dsh.bundle、Cordis patch、模块解析、inject、配置表达式和 preset 组合。
- 工具目录断言只暴露 heart_* 包装工具；原始 mcp__heart-algo__* 不出现在该 preset 的模型可见 schema 中。
- 工具单元测试覆盖 case_id 必填、asset_ids 可选、禁止 URL/路径字段、底层工具缺失、MCP isError、缺 structuredContent 和畸形结构。
- 状态测试覆盖 processing、completed 和 failed；processing 保留 task_id 且不虚构结果，failed 不泄露内部信息，completed 保留临床复核字段。
- 心超测试覆盖多资产、一张失败一张成功、skip_reason、缺失测量和未知 hf_type。
- ECG 测试覆盖 ECG-only、空 predictions、多标签概率不归一化、patient_info 可见性配置和 Top-K 展示限制。
- 混合任务测试同时断言 cardiac_ultrasound 与 ecg，不允许其中一个模态覆盖另一个。
- policy 测试覆盖模型直接调用原始 MCP 工具被拒绝、包装工具嵌套调用被允许、其他非心脏工具不受全局误伤。
- 隐私测试向 MCP 响应注入路径、stderr、Token-like 字段和额外患者标识，断言模型内容、presentation metadata 和会话日志均不含这些值。
- 生命周期测试卸载 preset 或插件 fiber 后断言工具、prompt section、guard 和监听器全部清理；HMR 重载不重复注册。
- 取消和超时测试断言 AbortSignal 传递到嵌套 MCP 调用，超时只终止当前查询，不改变算法服务中已发布任务。
- 快照测试覆盖 assembled system prompt、工具 schema、processing 回合、completed 回合和 failed 回合；至少一个场景固定完整模型可见头部。
- Web 展示如加入结果卡片，增加浏览器重放快照，证明 session replay 与实时展示一致。
- 发布物测试执行 pack dry-run、publint/hygiene 等价检查、NodeNext consumer smoke 和 plain Node 导入，避免只在 tsx 源码模式通过。
- 可选真实模型 e2e 在存在 DeepSeek API key 时运行，提示模型分析已登记病例，验证它选择包装工具、在 processing 后查询、输出临床免责声明且不补造数据。
- 真实模型 e2e 不用关键词自证；测试从会话日志和工具记录验证实际调用路径。
- 真实临床准确性不由插件自动化测试证明；真实样本结果必须沿用算法项目的模型覆盖、PhysicalDelta、CPU 稳定性和临床复核门禁。
- 参考既有 prior art：MCP client 的真实工具注册/重连测试、dsh-tools 执行管线测试、agent-presets 的站立组合与切换测试、发布 bundle 的 Loader smoke、算法项目的病例 MCP 客户端测试和 FakeRunner 闭环测试。

## Out of Scope

- 修改心超或 ECG 模型、权重、阈值、标签或临床准确性。
- 把 Python 推理逻辑、任务队列、病例存储或 MySQL 数据迁入 Harness。
- 修改现有病例 HTTP API 或 MCP 工具契约。
- 在 P0 中通过 Harness 上传 DICOM/XML、读取任意本地临床文件或接受诊断 URL。
- 让模型绕过病例授权或访问其他用户的 case_id/task_id。
- 自动批准临床复核、替代医生签字或把 pending 结果标成最终结论。
- 生成个体化药物、剂量、手术或急救处置方案。
- 根据单一模型阈值自动触发患者通知、急诊升级或护理动作。
- 长期病程数据库、跨病例趋势分析和人群统计。
- P0 中的日历提醒、定时随访、冷会话外部通知和任务调度。
- 支持 MCP Resources 和 Prompts；当前 Harness MCP bridge 只消费 Tools。
- 通用医疗知识库、RAG、联网搜索或第三方电子病历连接。
- 多实例算法服务、分布式队列或文件病例存储的横向扩展。
- 在 DeepSeek Harness 上游仓库内维护产品 fork。
- 对未经临床验证的 3 个豁免切面宣称生产可用。
- 把插件测试通过表述为医疗器械、临床有效性或合规认证。

## Further Notes

建议按三个里程碑执行。

P0“受控诊断 Agent”完成标准：

- heart-health-dsh-suite 可安装到独立 profile，并与 heart-algo-dsh-plugin 一起通过 dump-config。
- 新会话选择 heart-health preset 后只看到包装工具和必要交互能力。
- 已登记病例可提交并异步查询；三种任务状态和混合模态结果均符合稳定 JSON schema。
- 模型输出始终带临床辅助和复核状态说明，不补造数据。
- 原始文件、内部路径、stderr 和 Token 不进入模型可见内容或会话日志。
- 真实 Loader、FakeRunner 跨进程验收、关键快照和生命周期测试通过。

P1“领域技能与展示”完成标准：

- 五类按需 skills 可发现、可加载且不常驻无关请求。
- 心超、ECG、混合报告和复核摘要的快照稳定。
- Web 结果卡片可从持久化 metadata 重放，实时与冷读取一致。
- 领域说明经过临床人员审阅，明确版本和适用边界。