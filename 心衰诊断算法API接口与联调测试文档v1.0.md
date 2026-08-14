# 心衰诊断算法 API 接口与联调测试文档

## 1. 文档信息

| 项目 | 内容 |
|---|---|
| 文档版本 | v1.0 |
| 编写日期 | 2026-08-12 |
| 接口协议基线 | 图像算法分析接口协议 v3.1 |
| 服务类型 | FastAPI 异步任务服务 |
| 数据类型 | 心脏超声 DICOM、HL7 aECG XML |
| 当前部署示例 | `http://<算法服务器IP>:18000` |
| Swagger | `http://<算法服务器IP>:18000/docs` |
| OpenAPI | `http://<算法服务器IP>:18000/openapi.json` |

本文档描述当前算法 API 的实际接口、字段契约、调用流程及已经在服务器上执行过的真实联调测试数据，供后端开发、前端联调、mentor 评审和测试验收使用。

## 2. 当前能力和验收状态

| 能力 | 当前状态 | 说明 |
|---|---|---|
| 真实心超推理 | 已通过 | PLAX 真实 DICOM 推理成功，返回测量值和 ROI |
| 真实 ECG 推理 | 已通过 | 真实 HL7 aECG XML 推理成功，返回患者信息、测量值和 Top-5 预测 |
| 心超与 ECG 混合任务 | 已通过 | 同一任务返回 CU-SUB、CU-SUMMARY、ECG 三类报告 |
| 单张心超失败隔离 | 已通过 | 单图失败不影响其他心超图和汇总报告 |
| 错误脱敏 | 已通过 | API 响应不返回 stderr、traceback、Python 路径或模型路径 |
| MySQL 持久化 | 已通过 | 成功结果在服务重启后仍可查询 |
| `algorithm_execution` | 符合当前约定 | 当前版本暂不写入，服务器检查结果为 0 条 |
| 状态 0/1 重启恢复 | 暂缓验收 | 当前阶段不作为后端联调阻塞项 |
| CORS | 暂缓验收 | 当前前端尚未接入；后端到算法服务的调用不受 CORS 影响 |
| 临床准确性 | 待临床确认 | 接口和模型链路通过不等同于临床准确性验收 |

## 3. 调用约定

### 3.1 通用约定

- 请求协议：HTTP。
- 请求方法：`POST`。
- 请求及响应编码：UTF-8。
- 请求头：`Content-Type: application/json`。
- 当前版本未在算法服务内部实现鉴权，生产环境应由后端、网关或内网访问控制承担鉴权。
- `cardiacUltrasound` 和 `ecg` 至少应有一个包含有效输入；调用方不应提交空任务。
- 心超路径必须是算法服务器可访问的 DICOM 文件路径。
- ECG 路径必须是算法服务器可访问的 HL7 aECG XML 文件路径，不支持 ECG 图片。
- Windows 路径在 JSON 中需要将反斜杠写成 `\\`。

### 3.2 异步任务流程

1. 调用 `/heart-algo/task/start` 创建任务。
2. 接口快速返回 `taskId` 和 `taskState`。
3. 后端使用相同 `taskId` 和 `sysUserId` 轮询 `/heart-algo/task/result`。
4. `taskState=2` 表示成功，读取 `reports`、`cardiacUltrasound` 和 `ecg`。
5. `taskState=3` 表示失败，读取 `failedReason`。

建议轮询间隔为 2～5 秒。心超和 ECG 模型运行时间受 GPU、输入大小和队列长度影响，调用方不应使用固定短超时判断模型失败。

### 3.3 任务状态

| taskState | 含义 | 调用方行为 |
|---:|---|---|
| 0 | 排队中 | 继续轮询 |
| 1 | 分析中 | 继续轮询 |
| 2 | 分析成功 | 读取报告和源文件关联信息 |
| 3 | 分析失败 | 停止轮询并读取 `failedReason` |

### 3.4 幂等与数据隔离

- `taskId` 全局唯一。
- `(sysUserId, requestId)` 在同一用户内唯一。
- 同一用户使用相同 `requestId` 重试时，返回第一次创建的任务，不重复执行。
- 不同用户可以使用相同 `requestId`。
- 其他用户不能复用已经存在的 `taskId`。
- 查询结果时必须同时提供正确的 `taskId` 和 `sysUserId`。
- `/result` 请求中的 `requestId` 是本次查询流水号，响应中的 `responseId` 与它一致，不要求等于创建任务时的 `requestId`。

## 4. 支持的心超切面

| dcmType | 类型 | 主要输出 |
|---|---|---|
| `PLAX` | B-Mode | LVEDD、LVESD、LVEF、IVS、LVPW、LA、Aorta、AorticRoot |
| `A4C` | B-Mode | RVBase |
| `Subcostal` | B-Mode | IVC |
| `RVOT` | B-Mode | PA |
| `MV_EA` | Doppler | MV E 峰、A 峰、E/A |
| `AV_Vmax` | Doppler | 主动脉瓣峰值流速 |
| `TR_Vmax` | Doppler | 三尖瓣反流峰值流速 |
| `MR_Vmax` | Doppler | 二尖瓣反流峰值流速 |
| `LVOT_Vmax` | Doppler | 左室流出道峰值流速 |
| `TDI_Medial` | TDI | 二尖瓣环间隔侧 e' |
| `TDI_Lateral` | TDI | 二尖瓣环侧壁 e' |
| `TAPSE` | M-Mode | TAPSE |

调用方必须保证 DICOM 内容与 `dcmType` 一致。仅依赖 DICOM 标签无法稳定区分 PW、CW 和 TDI，因此 `MV_EA` 输入必须是二尖瓣舒张期 PW 血流频谱。

## 5. 开始分析任务

### 5.1 接口

```http
POST /heart-algo/task/start
Content-Type: application/json
```

### 5.2 请求结构

```json
{
  "requestId": "string",
  "sysUserId": "string",
  "taskId": "string",
  "cardiacUltrasound": [
    {
      "dcmType": "PLAX",
      "dcms": [
        {
          "dcmId": "string",
          "dcmPath": "string"
        }
      ]
    }
  ],
  "ecg": [
    {
      "ecgId": "string",
      "ecgPath": "string"
    }
  ]
}
```

### 5.3 请求字段

| 字段 | 类型 | 必填 | 说明 |
|---|---|---:|---|
| `requestId` | string | 是 | 请求流水号，用于同一用户内的幂等判断 |
| `sysUserId` | string | 是 | 发起任务的系统用户 ID |
| `taskId` | string | 是 | 调用方分配的任务 ID，全局唯一 |
| `cardiacUltrasound` | array | 是 | 心超切面分组；无心超时传 `[]` |
| `cardiacUltrasound[].dcmType` | string | 是 | 心超切面类型，见第 4 节 |
| `cardiacUltrasound[].dcms` | array | 是 | 当前切面下的 DICOM 列表 |
| `dcms[].dcmId` | string | 是 | 当前任务内的 DICOM 唯一标识 |
| `dcms[].dcmPath` | string | 是 | 算法服务器可访问的 DICOM 完整路径 |
| `ecg` | array | 是 | ECG 数据；无 ECG 时传 `[]` |
| `ecg[].ecgId` | string | 是 | 当前任务内的 ECG 唯一标识 |
| `ecg[].ecgPath` | string | 是 | 算法服务器可访问的 HL7 aECG XML 完整路径 |

当前 ECG-FM 链路每个任务只支持一份 ECG XML。

### 5.4 返回结构

```json
{
  "responseId": "test-001",
  "resultCode": 0,
  "resultMsg": "success",
  "taskId": "test-001",
  "taskState": 0
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `responseId` | string | 与本次请求的 `requestId` 一致 |
| `resultCode` | integer | `0` 表示请求已接受；`1` 表示失败 |
| `resultMsg` | string | 成功信息或公开错误信息 |
| `taskId` | string | 实际命中的任务 ID；幂等重试时可能是第一次创建的任务 ID |
| `taskState` | integer | 当前任务状态，见第 3.3 节 |

## 6. 查询分析结果

### 6.1 接口

```http
POST /heart-algo/task/result
Content-Type: application/json
```

### 6.2 请求结构

```json
{
  "requestId": "query-test-001",
  "sysUserId": "test-usere2e",
  "taskId": "test-001"
}
```

### 6.3 返回结构

```json
{
  "responseId": "query-test-001",
  "resultCode": 0,
  "resultMsg": "success",
  "taskId": "test-001",
  "taskState": 2,
  "reports": [],
  "cardiacUltrasound": [],
  "ecg": []
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `responseId` | string | 与本次查询请求的 `requestId` 一致 |
| `resultCode` | integer | `0` 表示正常；`1` 表示任务失败或未找到 |
| `resultMsg` | string | 状态说明或公开错误信息 |
| `taskId` | string | 查询的任务 ID |
| `taskState` | integer | 当前任务状态 |
| `failedReason` | string/null | 仅状态 3 时返回 |
| `reports` | array | 当前任务的报告列表 |
| `cardiacUltrasound` | array | 心超源文件、报告及 ROI 的关联列表 |
| `ecg` | array | ECG 源文件和报告的关联列表 |

### 6.4 报告类型

| reportType | 含义 | 是否关联单个输入 |
|---|---|---|
| `CU-SUB` | 单张心超 DICOM 的测量报告 | 是，通过 `cardiacUltrasound[].reportId` 关联 |
| `CU-SUMMARY` | 当前任务的心超综合汇总报告 | 否，位于顶层 `reports[]` |
| `ECG` | 单份 ECG XML 的患者信息、测量值和模型预测 | 是，通过 `ecg[].reportId` 关联 |

`reportResult` 的类型是字符串，但字符串内容本身是 JSON。后端读取时需要进行第二次 JSON 解析。

例如：

```javascript
const payload = JSON.parse(report.reportResult);
```

### 6.5 ROI 结构

```json
{
  "roiType": "LVEDD",
  "points": [
    {"xPos": 322, "yPos": 213},
    {"xPos": 243, "yPos": 330}
  ]
}
```

`roiType` 表示指标名，不表示 `POINT`、`LINE` 或 `REGION` 等几何类型。当前常见值包括：

```text
LVEDD / LVESD / IVS / LVPW / LA / Aorta / AorticRoot / RVBase / IVC / PA
```

Doppler、TDI 和 M-Mode 报告可能返回空 `rois`。

## 7. 心超指标元数据

每项心超测量值使用以下通用结构：

```json
{
  "value": 66.27,
  "name_cn": "左室舒末径",
  "unit": "mm",
  "reference": "35–55"
}
```

当前指标目录如下：

| 指标键 | 中文名 | 单位 | 参考范围 |
|---|---|---|---|
| `aorticroot` | 主动脉根部内径 | mm | 20–37 |
| `aorta` | 升主动脉内径 | mm | 20–37 |
| `lad` | 左房内径 | mm | 19–40 |
| `lvedd` | 左室舒末径 | mm | 35–55 |
| `lvesd` | 左室缩末径 | mm | 25–35 |
| `ivs` | 室间隔厚 | mm | 6–11 |
| `lvpw` | 左室后壁厚 | mm | 6–11 |
| `rvbase` | 右室内径 | mm | 0–20 |
| `ivc` | 下腔静脉内径 | mm | 10–25 |
| `pa` | 主肺动脉 | mm | 0–26 |
| `lvef` | 左室射血分数（EF） | % | 55–70 |
| `mv_e` | 二尖瓣 E 峰流速 | cm/s | 60–130 |
| `mv_a` | 二尖瓣 A 峰流速 | cm/s | 40–100 |
| `mv_ea` | E/A | - | 0.8–2.0 |
| `av_vmax` | 主动脉瓣峰值流速 | cm/s | 70–220 |
| `tr_vmax` | 三尖瓣反流峰值流速 | cm/s | ≤280 |
| `mr_vmax` | 二尖瓣反流峰值流速 | cm/s | — |
| `lvot_vmax` | 左室流出道峰值流速 | cm/s | 70–120 |
| `tdi_lateral` | 二尖瓣环侧壁 e' | cm/s | ≥10 |
| `tdi_medial` | 二尖瓣环间隔侧 e' | cm/s | ≥7 |
| `tapse` | TAPSE | mm | ≥17 |

当前 HF 分型规则：

| LVEF | hf_type |
|---|---|
| `< 40%` | `HFrEF` |
| `40%～49%` | `HFmrEF` |
| `≥ 50%` | `HFpEF` |

## 8. 服务器真实联调测试数据

### 8.1 测试环境

| 项目 | 测试值 |
|---|---|
| 算法服务端口 | `18000` |
| 任务存储 | MySQL |
| 心超模型目录 | `G:\meaurements\measurements\Measurement` |
| ECG-FM 项目目录 | `G:\ecg-fm\ecg-fm\ecg-fm` |
| ECG-FM 权重 | `G:\ecg-fm\ecg-fm\weights\mimic_iv_ecg_finetuned.pt` |
| 推理产物目录 | `G:\heart-algo\runtime-e2e` |

以下路径是服务器内部测试路径，仅用于当前测试环境。生产联调时应换成后端实际落盘且算法服务器可访问的路径。

### 8.2 真实测试文件

| 数据 | 路径 | 用途 |
|---|---|---|
| PLAX DICOM | `G:\meaurements\measurements\Measurement\data\testdata\test1-Bmodel\plax\00003_2dbbf05f7a19120f.dcm` | 心超和混合任务成功测试 |
| ECG XML | `G:\ecg-fm\ecg-fm\ecg-fm\data\xml_data\2.xml` | ECG 和混合任务成功测试 |
| MV_EA 失败样例 | `G:\meaurements\measurements\Measurement\data\testdata\test2-Doppler\MV_EA\00021_fbec2c817d7a5442.dcm` | 单图失败隔离测试 |
| 不存在的 ECG | `G:\ecg-fm\ecg-fm\ecg-fm\data\xml_data\3.xml` | 错误脱敏测试 |

## 9. 已执行的 Swagger 测试案例

### 9.1 案例一：真实心超成功

请求：

```json
{
  "requestId": "test-001",
  "sysUserId": "test-usere2e",
  "taskId": "test-001",
  "cardiacUltrasound": [
    {
      "dcmType": "PLAX",
      "dcms": [
        {
          "dcmId": "plax-00003",
          "dcmPath": "G:\\meaurements\\measurements\\Measurement\\data\\testdata\\test1-Bmodel\\plax\\00003_2dbbf05f7a19120f.dcm"
        }
      ]
    }
  ],
  "ecg": []
}
```

实际结果：

```json
{
  "resultCode": 0,
  "taskId": "test-001",
  "taskState": 2,
  "reportTypes": ["CU-SUB", "CU-SUMMARY"],
  "measurements": {
    "ivs": 8.86,
    "lad": 49.3,
    "lvef": 26.75,
    "lvpw": 12.03,
    "aorta": 28.16,
    "lvedd": 66.27,
    "lvesd": 57.81,
    "aorticroot": 21.52,
    "mv_ea": null,
    "hf_type": "HFrEF"
  }
}
```

实际返回了 LVEDD、LVESD、IVS、LVPW、LA、Aorta 和 AorticRoot 共 7 类 ROI。示例：

```json
[
  {
    "roiType": "LVEDD",
    "points": [
      {"xPos": 322, "yPos": 213},
      {"xPos": 243, "yPos": 330}
    ]
  },
  {
    "roiType": "LVESD",
    "points": [
      {"xPos": 316, "yPos": 211},
      {"xPos": 247, "yPos": 313}
    ]
  }
]
```

验收结论：通过。

### 9.2 案例二：真实 ECG 成功

请求：

```json
{
  "requestId": "test-002",
  "sysUserId": "test-usere2e",
  "taskId": "test-002",
  "cardiacUltrasound": [],
  "ecg": [
    {
      "ecgId": "ecg-1",
      "ecgPath": "G:\\ecg-fm\\ecg-fm\\ecg-fm\\data\\xml_data\\2.xml"
    }
  ]
}
```

实际结果：

```json
{
  "resultCode": 0,
  "taskId": "test-002",
  "taskState": 2,
  "reports": [
    {
      "reportType": "ECG",
      "reportResultParsed": {
        "ecgId": "ecg-1",
        "patientInfo": {
          "age": 72,
          "sex": "M",
          "patientId": "0004657885"
        },
        "measurements": {
          "qt": 380,
          "qtc": 389,
          "pAxis": 60,
          "tAxis": 45,
          "qrsAxis": 48,
          "ventRate": 63,
          "prInterval": 172,
          "qrsDuration": 86
        },
        "predictions": [
          {"label": "窦性心律", "probability": 0.860783},
          {"label": "室性早搏", "probability": 0.289694},
          {"label": "心肌梗死", "probability": 0.250626},
          {"label": "数据质量差", "probability": 0.210157},
          {"label": "心动过缓", "probability": 0.184483}
        ]
      }
    }
  ]
}
```

注意：这些概率来自多标签分类，各标签概率相互独立，因此总和不要求等于 1。

验收结论：通过。

### 9.3 案例三：心超与 ECG 混合任务成功

请求：

```json
{
  "requestId": "test-003",
  "sysUserId": "test-usere2e",
  "taskId": "test-003",
  "cardiacUltrasound": [
    {
      "dcmType": "PLAX",
      "dcms": [
        {
          "dcmId": "plax-00003",
          "dcmPath": "G:\\meaurements\\measurements\\Measurement\\data\\testdata\\test1-Bmodel\\plax\\00003_2dbbf05f7a19120f.dcm"
        }
      ]
    }
  ],
  "ecg": [
    {
      "ecgId": "ecg-1",
      "ecgPath": "G:\\ecg-fm\\ecg-fm\\ecg-fm\\data\\xml_data\\2.xml"
    }
  ]
}
```

实际结果摘要：

```json
{
  "resultCode": 0,
  "taskId": "test-003",
  "taskState": 2,
  "reportTypes": ["CU-SUB", "ECG", "CU-SUMMARY"],
  "cardiacUltrasoundCount": 1,
  "ecgCount": 1
}
```

验收结论：通过。

### 9.4 案例四：单张心超失败隔离

请求：

```json
{
  "requestId": "test-004",
  "sysUserId": "test-usere2e",
  "taskId": "test-004",
  "cardiacUltrasound": [
    {
      "dcmType": "PLAX",
      "dcms": [
        {
          "dcmId": "plax-00003",
          "dcmPath": "G:\\meaurements\\measurements\\Measurement\\data\\testdata\\test1-Bmodel\\plax\\00003_2dbbf05f7a19120f.dcm"
        }
      ]
    },
    {
      "dcmType": "MV_EA",
      "dcms": [
        {
          "dcmId": "mv-ea-00008",
          "dcmPath": "G:\\meaurements\\measurements\\Measurement\\data\\testdata\\test2-Doppler\\MV_EA\\00021_fbec2c817d7a5442.dcm"
        }
      ]
    }
  ],
  "ecg": []
}
```

实际结果摘要：

```json
{
  "resultCode": 0,
  "taskId": "test-004",
  "taskState": 2,
  "reports": [
    {
      "reportType": "CU-SUB",
      "dcmId": "plax-00003",
      "result": "成功，包含完整 PLAX 测量值和 ROI"
    },
    {
      "reportType": "CU-SUB",
      "dcmId": "mv-ea-00008",
      "error": "心超输入或模型文件不存在",
      "measurements": {}
    },
    {
      "reportType": "CU-SUMMARY",
      "measurements": {
        "lvef": 26.75,
        "mv_ea": null,
        "hf_type": "HFrEF"
      }
    }
  ]
}
```

该案例说明：

- 单张心超失败时，任务仍可以是状态 2。
- 失败输入通过对应 CU-SUB 的 `error` 表达。
- 成功输入的测量值和 ROI 继续返回。
- 汇总报告继续生成，缺失指标的 `value` 为 `null`。
- 后端不能仅根据任务状态判断每一张输入是否成功，还应检查 CU-SUB 中是否存在 `error` 或 `skipReason`。

验收结论：通过。

### 9.5 案例五：错误脱敏

请求：

```json
{
  "requestId": "test-005",
  "sysUserId": "test-usere2e",
  "taskId": "test-005",
  "cardiacUltrasound": [],
  "ecg": [
    {
      "ecgId": "ecg-1",
      "ecgPath": "G:\\ecg-fm\\ecg-fm\\ecg-fm\\data\\xml_data\\3.xml"
    }
  ]
}
```

实际响应：

```json
{
  "responseId": "test-005",
  "resultCode": 1,
  "resultMsg": "ECG 输入文件不存在",
  "taskId": "test-005",
  "taskState": 3,
  "failedReason": "ECG 输入文件不存在",
  "reports": [
    {
      "reportId": "test-005:ecg-1:ecg",
      "reportType": "ECG",
      "reportResult": "{\"ecgId\":\"ecg-1\",\"error\":\"ECG 输入文件不存在\"}"
    }
  ],
  "cardiacUltrasound": [],
  "ecg": [
    {
      "ecgId": "ecg-1",
      "ecgPath": "G:\\ecg-fm\\ecg-fm\\ecg-fm\\data\\xml_data\\3.xml",
      "reportId": "test-005:ecg-1:ecg"
    }
  ]
}
```

响应中没有出现 stderr、traceback、Python 路径或模型权重路径。

验收结论：通过。

### 9.6 案例六：成功结果重启恢复

服务重启后重新查询 `test-001` 和 `test-003`：

- 两个任务仍为状态 2。
- `reports`、`cardiacUltrasound`、`ecg` 和 ROI 均可从 MySQL 恢复。
- 不需要重新调用 `/start`。

验收结论：通过。

### 9.7 数据库执行记录检查

执行：

```sql
SELECT COUNT(*) AS execution_count
FROM algorithm_execution;
```

实际结果：

```text
+-----------------+
| execution_count |
+-----------------+
|               0 |
+-----------------+
```

这是当前开发阶段的预期行为。当前版本只写入：

- `algorithm_task`
- `algorithm_input`
- `algorithm_report`

`algorithm_execution` 是未来独立 Worker、模型版本、GPU、重试次数和执行审计功能的预留表。

## 10. 错误响应约定

### 10.1 任务级错误

任务整体失败时：

```json
{
  "resultCode": 1,
  "taskState": 3,
  "resultMsg": "公开中文错误信息",
  "failedReason": "公开中文错误信息"
}
```

### 10.2 单张心超错误

部分心超失败但任务仍成功时，在对应 CU-SUB 的 `reportResult` 中返回：

```json
{
  "dcmId": "mv-ea-00008",
  "error": "心超输入或模型文件不存在",
  "measurements": {}
}
```

### 10.3 当前公开错误示例

| 场景 | 公开消息 |
|---|---|
| ECG 文件不存在 | `ECG 输入文件不存在` |
| ECG 文件格式不支持 | `ECG 输入文件格式不支持：仅支持 XML 文件` |
| ECG 十二导联缺失 | `ECG 输入不完整：缺少十二导联长节律信号（...）` |
| ECG 采样点不一致 | `ECG 输入不完整：十二导联采样点数量不一致` |
| ECG 采样率缺失 | `ECG 输入不完整：采样率缺失或不合法` |
| ECG 数据转换失败 | `ECG 数据转换失败` |
| ECG 模型推理失败 | `ECG 模型推理失败` |
| ECG 超时 | `ECG 数据转换超时（超过 N 秒）` 或 `ECG 模型推理超时（超过 N 秒）` |
| 心超显存不足 | `心超模型显存不足，请稍后重试` |
| 心超文件缺失 | `心超输入或模型文件不存在` |
| 心超图像超范围 | `心超图像超出模型支持范围` |
| 未分类内部异常 | `算法服务内部错误，请联系管理员` |

完整 stderr、绝对路径和 traceback 只写服务器受控日志，不通过 API 返回。

## 11. 后端接入注意事项

1. 后端应保存 `taskId`，通过 `/result` 轮询，不应等待 `/start` 完成推理。
2. `taskState=0` 和 `taskState=1` 都是正常处理中状态。
3. `taskState=2` 只表示任务整体可交付；仍需逐个解析 CU-SUB，检查 `error` 和 `skipReason`。
4. `reportResult` 是 JSON 字符串，需要二次解析。
5. `CU-SUMMARY` 是任务级心超汇总，不关联单张 DICOM。
6. `dcmPath` 和 `ecgPath` 必须是算法服务器能够访问的路径。浏览器本地路径不能直接传给算法服务。
7. 同一任务的 `dcmId` 和 `ecgId` 应保持唯一。
8. 后端重试相同业务请求时必须复用原 `requestId`，避免重复任务。
9. 生产启动必须使用 MySQL TaskStore；内存 TaskStore 不具备跨重启持久化能力。
10. 当前没有写入 `algorithm_execution`，后端不应依赖该表判断任务完成。

## 12. 当前暂缓项与已知边界

### 12.1 状态 0/1 重启恢复

恢复逻辑已在代码中实现，但当前服务器端异常中断场景尚未完成稳定验收，因此暂不作为后端联调阻塞项，也不建议在业务层依赖自动恢复承诺。

后端在任务长时间停留于 0 或 1 时，应保留人工重试或重新提交机制。

### 12.2 CORS

当前前端尚未直接调用算法 API，CORS 暂缓验收。后端服务到算法服务的 HTTP 调用不受浏览器 CORS 机制限制。

### 12.3 临床准确性

- 当前 LVEF 由 LVEDD/LVESD 使用 Teichholz 公式估算。
- LVEF 与医生金标准仍需临床核对。
- ECG Top-5 的临床合理性仍需医生审核。
- 模型输出属于辅助分析结果，不能替代医生诊断。
- 当前不输出 GLS。

## 13. 后端最小联调验收清单

- [ ] 后端可以提交仅心超任务并轮询到状态 2。
- [ ] 后端可以提交仅 ECG 任务并解析 ECG `reportResult`。
- [ ] 后端可以提交混合任务并识别三种 `reportType`。
- [ ] 后端可以关联 `cardiacUltrasound[].reportId` 与 CU-SUB。
- [ ] 后端可以关联 `ecg[].reportId` 与 ECG 报告。
- [ ] 后端可以二次解析 `reportResult` JSON 字符串。
- [ ] 后端可以识别单张心超 `error`，同时保留其他成功结果。
- [ ] 后端可以处理状态 3 和 `failedReason`。
- [ ] 后端重发请求时正确复用 `requestId`。
- [ ] 服务重启后，后端仍能查询已成功任务。

