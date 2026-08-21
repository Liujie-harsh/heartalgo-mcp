# 图像算法 MySQL 数据库（精简版）

本结构按《图像算法分析接口协议》设计，仅保留 4 张核心表，并使用中文字段注释。`schema_mysql8.sql` 只包含 `CREATE TABLE`，不会删除已有数据库或表。

## 表结构

| 表 | 用途 | 关键字段 |
| --- | --- | --- |
| `algorithm_task` | 任务与任务级状态 | `task_id`、`request_id`、`sys_user_id`、`task_state`、`failed_reason` |
| `algorithm_input` | 心超 DCM 与 ECG XML 的统一输入 | `input_type`、`input_id`、`input_path`、`dcm_type`、`input_state` |
| `algorithm_execution` | 生产化执行审计预留表；当前版本不写入 | `algorithm_code`、`execution_state`、`python_queue_name`、`gpu_device` |
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

algorithm_execution（未来启用独立 Worker/重试后）
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
- 当前版本直接由任务执行结果更新 `algorithm_task.task_state`；`algorithm_execution` 暂不参与状态计算。

## Python 队列与多 GPU

队列在 API 进程内运行，不使用外部消息服务。`PYTHON_GPU_IDS=0,1` 时，心超和 ECG-FM 子任务可分别占用空闲 GPU；同一任务的所有子任务结束后再统一返回结果。

进程内队列本身不会跨重启保留，但服务启动时会通过 `MySQLTaskStore` 做一次恢复：

- 状态 `0-排队中` 的任务重新投递到当前进程队列；数据库原子 `claim` 保证不会重复执行。
- 单实例默认将遗留状态 `1-分析中` 终止为状态 `3`，公共失败原因为“任务因算法服务中断而终止，请重新提交”。
- `TASK_STALE_RUNNING_SECONDS` 可以设置启动扫描的宽限秒数，默认 `0`。多实例或滚动发布不能依赖该策略，应升级为数据库租约、心跳和独立 Worker。

## 启用 MySQLTaskStore

安装数据库依赖：

```powershell
python -m pip install "SQLAlchemy>=2,<3" "PyMySQL>=1.1,<3"
```

使用环境变量传入连接信息，避免把密码写进源码、启动脚本或 Git：

```powershell
$env:TASK_STORE_BACKEND = "mysql"
$env:DATABASE_URL = "mysql+pymysql://<user>:<url-encoded-password>@<host>:3306/<database>?charset=utf8mb4"
$env:TASK_STALE_RUNNING_SECONDS = "0"
python main.py --fake
```

未设置 `TASK_STORE_BACKEND` 时仍使用内存存储，适合不连接数据库的单元测试。
开发阶段只写 `algorithm_task`、`algorithm_input`、`algorithm_report`；
`algorithm_execution` 按当前约定建表但暂不写入。

## 错误脱敏与 CORS

接口只返回稳定中文业务错误；子进程 stderr、绝对路径和 traceback 不进入响应，原始异常仅写服务端日志。未知异常统一返回“算法服务内部错误，请联系管理员”。

CORS 默认关闭，不影响后端到算法服务的 HTTP 调用。只有浏览器需要直连算法服务时才配置精确白名单，多个 Origin 用英文逗号分隔，禁止使用 `*`：

```powershell
$env:CORS_ALLOWED_ORIGINS = "https://app.example.com,https://admin.example.com"
```

## 真实 MySQL 契约测试

测试必须指向专用测试库，不能指向生产库或共享开发库：

```powershell
$env:TEST_DATABASE_URL = "mysql+pymysql://<user>:<url-encoded-password>@127.0.0.1:3306/heart_failure_analytics_test?charset=utf8mb4"
pytest -q test_mysql_task_store.py
```

测试数据使用随机 `codex_<uuid>_` 前缀并在用例结束后删除。未设置
`TEST_DATABASE_URL` 时，真实 MySQL 用例会跳过，同时仍执行 SQLite 快速契约测试。
