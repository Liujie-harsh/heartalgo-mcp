-- 图像算法分析服务精简版数据库（MySQL 8.0.16+）
-- 设计原则：接口本身以 JSON 返回报告和 ROI，因此不再把每个 ROI、坐标点、输入关联拆成独立表。
SET NAMES utf8mb4;

-- 1. 分析任务：对应 requestId、sysUserId、taskId、taskState、failedReason。
CREATE TABLE algorithm_task (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '数据库内部主键',
  task_id VARCHAR(128) NOT NULL COMMENT '协议 taskId，任务唯一标识',
  request_id VARCHAR(128) NOT NULL COMMENT '协议 requestId，用于幂等判断',
  sys_user_id VARCHAR(128) NOT NULL COMMENT '协议 sysUserId，任务发起用户',
  task_state TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '0-排队中；1-分析中；2-成功；3-失败',
  failed_reason TEXT NULL COMMENT '任务失败原因，仅失败时填写',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  started_at DATETIME(6) NULL COMMENT '开始分析时间',
  finished_at DATETIME(6) NULL COMMENT '结束时间',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '最近更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_task_id (task_id),
  UNIQUE KEY uk_user_request (sys_user_id, request_id),
  KEY idx_task_state_created (task_state, created_at),
  CONSTRAINT chk_task_state CHECK (task_state IN (0, 1, 2, 3))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='算法分析任务';

-- 2. 统一输入：同时保存心超 DCM 与 ECG XML。
-- 心超记录填写 dcm_type；心电记录的 dcm_type 为 NULL。
CREATE TABLE algorithm_input (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '数据库内部主键',
  task_db_id BIGINT UNSIGNED NOT NULL COMMENT '所属任务，关联 algorithm_task.id',
  input_type VARCHAR(32) NOT NULL COMMENT 'CARDIAC_ULTRASOUND-心超；ECG-心电',
  input_id VARCHAR(128) NOT NULL COMMENT '心超对应 dcmId；心电对应 ecgId',
  input_path TEXT NOT NULL COMMENT '心超对应 dcmPath；心电对应 ecgPath',
  dcm_type VARCHAR(64) NULL COMMENT '心超切面 dcmType，合法值：PLAX/A4C/Subcostal/RVOT/MV_EA/AV_Vmax/TR_Vmax/MR_Vmax/LVOT_Vmax/TDI_Medial/TDI_Lateral/TAPSE；心电为空',
  input_state TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '0-待处理；1-处理中；2-成功；3-失败',
  failed_reason TEXT NULL COMMENT '单个输入失败原因',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  updated_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '最近更新时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_task_input (task_db_id, input_type, input_id),
  KEY idx_input_task_state (task_db_id, input_state),
  CONSTRAINT fk_input_task FOREIGN KEY (task_db_id) REFERENCES algorithm_task(id) ON DELETE CASCADE,
  CONSTRAINT chk_input_type CHECK (input_type IN ('CARDIAC_ULTRASOUND', 'ECG')),
  CONSTRAINT chk_input_state CHECK (input_state IN (0, 1, 2, 3))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='心超与心电统一输入';

-- 3. 模型执行：一条记录代表一次 ECG-FM 或心超 Measurement 调用。
-- python_queue_name 记录本服务进程内 Python 队列名称；队列只在当前 API 进程内生效。
CREATE TABLE algorithm_execution (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '数据库内部主键，也是队列任务执行 ID',
  task_db_id BIGINT UNSIGNED NOT NULL COMMENT '所属任务',
  input_db_id BIGINT UNSIGNED NULL COMMENT '所属输入；心超综合报告可为空',
  algorithm_code VARCHAR(64) NOT NULL COMMENT 'MEASUREMENT-心超；ECGFM-心电',
  model_version VARCHAR(128) NULL COMMENT '模型或权重版本',
  execution_state TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '0-排队中；1-运行中；2-成功；3-失败',
  python_queue_name VARCHAR(64) NULL COMMENT '进程内 Python 队列名称，仅用于执行审计',
  gpu_device VARCHAR(32) NULL COMMENT '实际执行 GPU，例如 cuda:0',
  attempt_no INT UNSIGNED NOT NULL DEFAULT 1 COMMENT '执行尝试次数',
  error_code VARCHAR(64) NULL COMMENT '标准化错误码',
  failed_reason TEXT NULL COMMENT '执行失败原因',
  input_path TEXT NULL COMMENT '任务隔离后的实际输入路径',
  output_path TEXT NULL COMMENT '任务隔离后的实际输出路径',
  started_at DATETIME(6) NULL COMMENT '开始时间',
  finished_at DATETIME(6) NULL COMMENT '结束时间',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '入队时间',
  PRIMARY KEY (id),
  KEY idx_execution_task_state (task_db_id, execution_state, created_at),
  KEY idx_execution_input (input_db_id),
  KEY idx_execution_python_queue_state (python_queue_name, execution_state),
  CONSTRAINT fk_execution_task FOREIGN KEY (task_db_id) REFERENCES algorithm_task(id) ON DELETE CASCADE,
  CONSTRAINT fk_execution_input FOREIGN KEY (input_db_id) REFERENCES algorithm_input(id) ON DELETE CASCADE,
  CONSTRAINT chk_execution_state CHECK (execution_state IN (0, 1, 2, 3))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='模型执行记录';

-- 4. 分析报告：对应 /result 的 reports[]。
-- report_result 保存 reportResult 的 JSON 内容；roi_data 保存心超 rois 数组。
-- 查询结果时：读取 algorithm_input 的 input_id/input_path 与本表 report_id 即可组装 cardiacUltrasound[] 或 ecg[]。
CREATE TABLE algorithm_report (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '数据库内部主键',
  task_db_id BIGINT UNSIGNED NOT NULL COMMENT '所属任务',
  input_db_id BIGINT UNSIGNED NULL COMMENT '关联输入；任务级综合报告可以为空',
  execution_id BIGINT UNSIGNED NULL COMMENT '产生该报告的模型执行记录',
  report_id VARCHAR(128) NOT NULL COMMENT '协议 reportId',
  report_type VARCHAR(32) NOT NULL COMMENT 'CU-SUB-心超单图报告；CU-SUMMARY-心超综合汇总报告；ECG-心电报告',
  report_result JSON NOT NULL COMMENT '协议 reportResult 的 JSON 内容，返回接口时序列化为字符串',
  roi_data JSON NULL COMMENT '心超 rois 数组，元素含 roiType 和 points；ECG 与 CU-SUMMARY 为空',
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_report_id (report_id),
  UNIQUE KEY uk_report_execution (execution_id),
  KEY idx_report_task_type (task_db_id, report_type),
  KEY idx_report_input (input_db_id),
  CONSTRAINT fk_report_task FOREIGN KEY (task_db_id) REFERENCES algorithm_task(id) ON DELETE CASCADE,
  CONSTRAINT fk_report_input FOREIGN KEY (input_db_id) REFERENCES algorithm_input(id) ON DELETE CASCADE,
  CONSTRAINT fk_report_execution FOREIGN KEY (execution_id) REFERENCES algorithm_execution(id) ON DELETE SET NULL,
  CONSTRAINT chk_report_type CHECK (report_type IN ('CU-SUB', 'CU-SUMMARY', 'ECG'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='算法分析报告';