# MCP 工具面扩展：从"提交+查询"到完整分析闭环

## 背景

扩展前 MCP 服务只有 3 个工具（提交分析、查询结果、能力目录），输入必须先经病例
门户/HTTP API 登记，结果解读完全依赖调用方模型自由发挥。本次扩展围绕六个方向补全
闭环，**不改变任何既有算法、存储与鉴权语义**：

| 方向 | 新增工具 |
|---|---|
| 一站式输入 | analyze_case_files |
| 结果智能解读 | interpret_diagnosis |
| 报告生成 | generate_report |
| 纵向对比 | compare_diagnoses |
| 病例/任务管理 | list_cases、get_case_detail、list_tasks |
| 复核闭环 | get_review_status、submit_review |

diagnose_heart_failure / get_diagnosis_result / list_supported_views 三个既有
工具与 resource、prompt 保持原契约不变。

## 新工具契约

### analyze_case_files(files, request_id=None, submit=True)

- files 每项：{"path", "modality": "CARDIAC_ULTRASOUND"|"ECG", "dcm_type"(心超必填), "asset_id"(可选)}
- 服务端自动：建病例（归属 MCP 服务账号并自授权）→ add_asset 复用 DICOM/XML
  内容校验、SHA-256、大小上限 → submit_case_diagnosis 提交任务
- 路径校验前置（文件必须存在、modality/dcm_type 组合合法、asset_id 不重复），
  失败时在创建病例前快速报错
- request_id 传稳定值可让建病例步骤幂等；缺省每次调用新建病例
- submit=False 时只登记资产不提交分析

### interpret_diagnosis(task_id)

规则解读（interpretation.py，纯函数、零外部依赖）：

- **异常标注**：METRIC_META 参考范围四种格式（"20–37" / "≤280" / "≥17" / "—"）
  统一解析，逐资产输出 {metric, name_cn, value, unit, reference, status: low|high}
- **LVEF 分型**：<40 HFrEF，40–49 HFmrEF，≥50 HFpEF（与 prompt 协议一致）
- **组合指标**：E/A（优先取测量值，缺 mv_ea 时由 mv_e/mv_a 推导）；
  E/e′ = mv_e / avg(tdi_medial, tdi_lateral)，≥14 标记异常
- **ECG 摘要**：每份 ECG 取概率 ≥0.5 的 top-5 预测
- **资产级透明**：unavailable_assets 单列 error/skip_reason，任务 completed
  不代表每张输入成功
- 固定输出 Teichholz 估算、模型分型非诊断、ECG 概率独立、需临床复核四条 caveat

### generate_report(task_id, format="markdown", save_to_case=False)

- report_render.py 渲染 Markdown 报告草稿：概要（含复核状态声明）、异常提示表、
  组合指标表、逐资产心超测量表（数值/单位/参考/提示）、ECG 测量与预测、
  输入 SHA-256 可追溯性、说明与限制、最近复核记录
- format="json" 输出结构化报告对象；save_to_case=True 写入病例
  artifacts/report-{task_id}.md|.json（FileCaseStore.save_case_artifact：
  原子写、SHA-256 幂等、元数据登记，get_case_detail 可查）

### compare_diagnoses(case_id, task_id_a, task_id_b)

- 仅对比同一病例、均已完成的任务；未完成时返回双方状态不报错
- 输出同名指标合并对比：绝对差、百分比变化、方向、notable 标记
  （LVEF 阈值 5 个百分点，其余相对变化 ≥10%）；LVEF 分型迁移（from/to）

### list_cases / get_case_detail / list_tasks

- FileCaseStore.list_cases_for_service：按病例级 ACL 扫描授权病例摘要
  （资产/任务计数、最近复核决定）；损坏的病例元数据跳过不阻塞列表
- get_case_detail 在 public_case 基础上叠加任务实时状态
  （queued/processing/completed/failed/unknown），并暴露报告工件（不含磁盘路径）
- list_tasks 支持按 case_id 过滤

### get_review_status(task_id) / submit_review(task_id, decision, reviewer_id, comment)

- 复核结论记录复用既有 record_review 审计语义
- MCP 侧新增两条守卫：任务必须已完成（taskState==2）；**复核人不能与病例所有者
  相同**（与 HTTP 鉴权路径的自我复核禁令对齐）。服务账号自建病例因此无法经 MCP
  自我批准，复核必须由真实临床人员身份执行

## 设计边界（与既有原则一致）

- 解读、报告、对比全部是**规则比对与展示**，不生成诊断结论、不出治疗方案
- requires_clinician_review 语义贯穿所有新工具输出
- 不新增任何绕过病例 ACL 的路径：所有读取仍走 get_case_for_service /
  find_case_for_task；get_case_by_id 仍仅限内部授权使用
- 一站式病例归属服务账号并在授权列表中，门户路径的医生归属病例不受影响

## 测试

test/test_mcp_extensions.py（9 项）：

- interpretation 纯函数：参考格式解析、LVEF 切点、异常标注、E/A 与 E/e′ 推导、
  对比 delta 与分型迁移
- report_render：章节完整性、复核声明、测量表、追溯表
- MCP 闭环（内存 Client(mcp)）：一站式分析→解读→报告落盘→检索→复核→状态查询
  全链路；自我复核与非法 DICOM/缺失文件的错误路径；跨病例对比拒绝；
  双任务随访对比（LVEF 35.48→45 delta=9.52、HFrEF→HFmrEF）；JSON 报告与非法
  format 拒绝

环境提示：在受限沙盒或特殊 ACL 环境下运行时，可设 HEART_TEST_TMPDIR 把集成测试
的临时根目录重定向到可写位置。

## 文件清单

- 新增：interpretation.py、report_render.py、test/test_mcp_extensions.py
- 修改：mcp_server.py（9 个新工具 + instructions 更新 + prompt 微调）、
  case_store.py（list_cases_for_service、save_case_artifact、
  read_case_artifact、public_case 暴露 artifacts）、插件 README.md
- 结果：MCP 工具面 3 → 12，全量测试 212 passed / 7 skipped（MySQL 集成测试按需跳过）
