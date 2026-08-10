# 图像算法 MySQL 数据库（精简版）

本结构按《图像算法分析接口协议》设计，仅保留 4 张核心表，并使用中文字段注释。`schema_mysql8.sql` 只包含 `CREATE TABLE`，不会删除已有数据库或表。

## 表结构

| 表 | 用途 | 关键字段 |
| --- | --- | --- |
| `algorithm_task` | 任务与任务级状态 | `task_id`、`request_id`、`sys_user_id`、`task_state`、`failed_reason` |
| `algorithm_input` | 心超 DCM 与 ECG XML 的统一输入 | `input_type`、`input_id`、`input_path`、`dcm_type`、`input_state` |
| `algorithm_execution` | ECG-FM、心超模型的运行记录 | `algorithm_code`、`execution_state`、`python_queue_name`、`gpu_device` |
| `algorithm_report` | 接口返回的报告与 ROI | `report_id`、`report_type`、`report_result`、`roi_data` |

## 建表完成后的预览示例

以下为一个同时包含心超和 ECG 的任务，在模型完成后数据库中应呈现的典型数据。示例仅用于说明字段关系，不会自动插入数据库。

```text
algorithm_task
┌────┬──────────────┬──────────────┬────────────┬────────────┬───────────┐
│ id │ task_id      │ request_id   │ sys_user_id│ task_state │ failed... │
├────┼──────────────┼──────────────┼────────────┼────────────┼───────────┤
│ 1  │ task-000001  │ request-0001 │ user-1001  │ 2（成功）  │ NULL      │
└────┴──────────────┴──────────────┴────────────┴────────────┴───────────┘

algorithm_input
┌────┬────────────┬────────────────────┬──────────────┬──────────────────────┐
│ id │ input_type │ input_id           │ dcm_type     │ input_path           │
├────┼────────────┼────────────────────┼──────────────┼──────────────────────┤
│ 11 │ CARDIAC... │ dcm-0001           │ a4c          │ /echo/0001.dcm       │
│ 12 │ ECG        │ ecg-0001           │ NULL         │ /ecg/0001.xml        │
└────┴────────────┴────────────────────┴──────────────┴──────────────────────┘

algorithm_execution
┌────┬────────────┬───────────────┬─────────────────┬──────────────┬────────────┐
│ id │ input_db_id│ algorithm_code│ python_queue... │ gpu_device   │ state      │
├────┼────────────┼───────────────┼─────────────────┼──────────────┼────────────┤
│ 21 │ 11         │ MEASUREMENT   │ heart_algo_...  │ cuda:0       │ 2（成功）  │
│ 22 │ 12         │ ECGFM         │ heart_algo_...  │ cuda:1       │ 2（成功）  │
└────┴────────────┴───────────────┴─────────────────┴──────────────┴────────────┘

algorithm_report
┌────┬──────────────┬──────────────┬────────────┬─────────────────────────────────────┐
│ id │ report_id    │ type         │ input_db_id│ 内容                                │
├────┼──────────────┼──────────────┼────────────┼─────────────────────────────────────┤
│ 31 │ report-echo1 │ CU-SUB       │ 11         │ report_result + roi_data（JSON）   │
│ 32 │ report-ecg1  │ ECG          │ 12         │ report_result（预测、测量、患者） │
│ 33 │ task-cu-sum  │ CU-SUMMARY   │ NULL       │ 心超综合汇总（HF 分型 + 核心指标） │
└────┴──────────────┴──────────────┴────────────┴─────────────────────────────────────┘
```

对应接口结果时：

- `algorithm_report.report_result` 序列化为 `reports[].reportResult`。
- 心超结果由 `algorithm_input`（`dcmId`、`dcmPath`）和对应报告（`reportId`、`roi_data`）组成 `cardiacUltrasound[]`。
- ECG 结果由 `algorithm_input`（`ecgId`、`ecgPath`）和对应报告（`reportId`）组成 `ecg[]`。
- 同一任务所有 `algorithm_execution` 成功后，`algorithm_task.task_state` 更新为 `2`；任一子任务失败则更新为 `3` 并记录 `failed_reason`。

## Python 队列与多 GPU

队列在 API 进程内运行，不使用外部消息服务。`PYTHON_GPU_IDS=0,1` 时，心超和 ECG-FM 子任务可分别占用空闲 GPU；同一任务的所有子任务结束后再统一返回结果。

注意：进程内队列在服务重启后不会保留未完成任务；MySQL 接入完成后，任务、执行状态与结果将以数据库记录为准。