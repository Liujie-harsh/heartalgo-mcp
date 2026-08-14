"""Swagger/OpenAPI 请求示例。"""

START_REQUEST_EXAMPLE = {
    "requestId": "swagger-start-20260814-001",
    "sysUserId": "swagger-user-001",
    "taskId": "swagger-task-20260814-001",
    "cardiacUltrasound": [
        {
            "dcmType": "A4C",
            "dcms": [
                {
                    "dcmId": "bmode-a4c-00007",
                    "dcmPath": (
                        "http://172.16.32.185:19090/"
                        "%E5%88%86%E6%94%AF1-BMode/a4c/"
                        "00007_f740c695836ad433.dcm"
                    ),
                }
            ],
        },
        {
            "dcmType": "PLAX",
            "dcms": [
                {
                    "dcmId": "bmode-plax-00005",
                    "dcmPath": (
                        "http://172.16.32.185:19090/"
                        "%E5%88%86%E6%94%AF1-BMode/plax/"
                        "00005_78904ff78199cc24.dcm"
                    ),
                }
            ],
        },
        {
            "dcmType": "MV_EA",
            "dcms": [
                {
                    "dcmId": "doppler-mv-00033",
                    "dcmPath": (
                        "http://172.16.32.185:19090/"
                        "%E5%88%86%E6%94%AF2-Doppler/"
                        "MV%E4%BA%8C%E5%B0%96%E7%93%A3%E8%A1%80%E6%B5%81/"
                        "00033_944654c643aca3eb.dcm"
                    ),
                }
            ],
        },
        {
            "dcmType": "TDI_Medial",
            "dcms": [
                {
                    "dcmId": "tdi-medial-00005",
                    "dcmPath": (
                        "http://172.16.32.185:19090/"
                        "%E5%88%86%E6%94%AF3-TDI/"
                        "A4C(%E4%BA%8C%E5%B0%96%E7%93%A3%E7%8E%AF"
                        "%E5%86%85%E4%BE%A7)/00005_3e640d9a052f89a8.dcm"
                    ),
                }
            ],
        },
        {
            "dcmType": "TAPSE",
            "dcms": [
                {
                    "dcmId": "mmode-a4c-00010",
                    "dcmPath": (
                        "http://172.16.32.185:19090/"
                        "%E5%88%86%E6%94%AF4-MMode/A4C/"
                        "00010_fdb3ee2ef3695285.dcm"
                    ),
                }
            ],
        },
    ],
    "ecg": [],
}
