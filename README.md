# Aurobox — 社區 AMR 包裹配送系統

住戶用 LINE 收包裹通知、掃碼取貨；自動移動機器人（AMR）把包裹送到住戶門口；管理員用瀏覽器上的 Dashboard 建立包裹、派送、處理例外。

這是一個 monorepo，包含兩個**獨立部署、各自持有資料庫**的服務，以及一個嵌在後端服務裡的管理介面。

```mermaid
flowchart LR
  R[住戶<br/>LINE App] <-->|Messaging API / LIFF| LB
  A[管理員<br/>瀏覽器] <-->|Basic Auth| DB_UI
  subgraph S1["line-backend　業務邏輯與狀態真相"]
    LB[FastAPI + Webhook]
    DB_UI[管理員 Dashboard]
    PG1[(PostgreSQL)]
    LB --- PG1
    DB_UI --- LB
  end
  subgraph S2["flashbot-robot　硬體控制"]
    FB[Flask API]
    PG2[(PostgreSQL)]
    FB --- PG2
  end
  LB <-->|HTTP　4 個整合點| FB
  FB -->|Pudu Open Platform| P[Pudu Flashbot 機器人]
```

---

## 系統組成

| 元件 | 目錄 | 技術 | 負責 |
|---|---|---|---|
| **LINE 後端** | `line-backend/` | FastAPI · PostgreSQL · SQLAlchemy · APScheduler · LINE Messaging API v3 · LIFF | 包裹狀態機、住戶對話、排程、對機器人下指令 |
| **管理員 Dashboard** | `line-backend/app/templates`<br>`line-backend/app/static` | Jinja2 · 原生 CSS/JS（無框架、無 build） | 建立包裹、派送、艙門控制、例外處理、報表、綁定管理 |
| **機器人模組** | `flashbot-robot/` | Flask · PostgreSQL · Pudu Open Platform API | 艙門硬體、機器人移動與導航、QR 顯示 |

Dashboard 不是獨立專案，它掛在 `line-backend` 的同一個 FastAPI 服務底下，用 `Jinja2Templates` + `StaticFiles` 提供。

---

## 三條架構原則

### 1. 狀態只有一個真相來源

`line-backend` 是包裹狀態的**唯一權威**。什麼時候該叫機器人做什麼事、包裹現在是什麼狀態，全部由它決定。`flashbot-robot` 只負責收指令、操作硬體、回報硬體結果，**不重複維護任何業務狀態**。

兩邊各自維護一份業務邏輯是資料不一致的來源，所以刻意不這麼做。

### 2. 兩個獨立資料庫，只靠 HTTP 溝通

兩個服務各連自己的 PostgreSQL，彼此看不到對方的資料表。整個資料模型**沒有任何實體外鍵**——連同一個資料庫內的關聯也是應用程式維護的邏輯關聯。

跨資料庫那條 `packages.door_task_id ↔ doors.door_task_id`，兩邊型別還不一樣（UUID vs VARCHAR(100)），沒有外鍵、沒有觸發器、沒有一致性檢查。任一邊資料被手動改動或 API 呼叫失敗未回滾，兩邊就會不同步且**不會有錯誤訊息**，要等機器人開錯門才會發現。

### 3. 兩個服務只在四個點交會

| 整合點 | 方向 | 呼叫 |
|---|---|---|
| **派送** | 後端 → 機器人 | `POST /api/robot/dispatch` |
| **QR 顯示／開門** | 後端 → 機器人 | `POST /api/door-tasks/{id}/pickup-complete` |
| **開門** | 後端 → 機器人 | `POST /api/doors/return-open` |
| **關門與返航** | 後端 → 機器人 | `POST /api/door-tasks/{id}/complete`、`/api/door-tasks/return` |
| **抵達回報** | 機器人 → 後端 | `POST /door-tasks/{id}/arrived` |

機器人主動打回來的只有「抵達」一支，其餘狀態確認一律由後端主動輪詢 `GET /api/dashboard/status`。

---

## 核心概念

### 包裹狀態機

```
pending ──→ pickup_now ──→ delivering ──→ arrived ──→ completed
   │             │                           │
   │             │                           ├──→ rejected_at_door   住戶拒收／取消退貨
   │             │                           └──→ returned_timeout   8 分鐘未取
   └──→ voided   住戶按「不收」
```

| 狀態 | 介面顯示 | 意義 |
|---|---|---|
| `pending` | 待處理 | 已建立，等住戶回應到貨通知 |
| `pickup_now` | 待派送 | 住戶要取貨（或已預約），等管理員放置與派送 |
| `delivering` | 配送中 | 已裝載、機器人出發中 |
| `arrived` | 已抵達 | 機器人到門口，等住戶掃碼 |
| `completed` | 已完成 | 取貨／放貨完成，門已關 |
| `rejected_at_door` | 拒收（作廢） | 抵達後被拒收，或退貨被取消 |
| `returned_timeout` | 逾時（作廢） | 抵達後 8 分鐘未取 |
| `voided` | 不收（作廢） | 到貨通知階段就拒絕，包裹沒出過門 |

送貨與退貨共用同一張表，以 `task_type`（`delivery` / `return`）區分。

### `door_task_id`：以「一站」為分組單位

同一站的多筆包裹共用一個 `door_task_id`，分組鍵是 **`line_user_id` + `unit` + `task_type` 三者相同**。同一組底下的所有包裹，狀態轉換（抵達、驗證、完成、拒收、逾時）全部綁在一起走。

QR Code 的內容就是 `door_task_id` 本身，所以住戶掃**一次**就會把這一站所有的門一起打開，不論有幾扇門、幾件包裹。

分組鍵必須包含 `task_type`——少了它，同一戶的送貨與退貨會被錯誤合併成同一站。

### `creation_batch_id`：以「一次建立」為分組單位

管理員一次建立多件（`quantity > 1`）時，會建立多筆各自獨立的包裹紀錄，共用同一個 `creation_batch_id`。到貨通知只發一次，但住戶按「取貨」／「預約」／「不收」時一次套用整批。

---

## 專案結構

```
AMR-System/
├── line-backend/                 LINE 後端 + 管理員 Dashboard（FastAPI）
│   ├── app/
│   │   ├── main.py                   Webhook、39 支路由、6 支排程
│   │   ├── models.py                 Package / LineBinding / PackageRecipient / TaskLog
│   │   ├── line_messaging.py         LINE 推播與 Flex Message 封裝
│   │   ├── line_verify.py            LIFF ID Token 驗證
│   │   ├── config.py / db.py / init_db.py
│   │   ├── templates/                Dashboard 四頁的 Jinja2 樣板
│   │   └── static/                   共用 CSS 與各頁 JS
│   ├── tests/
│   └── requirements.txt
│
├── flashbot-robot/               機器人硬體控制（Flask）
│   ├── src/aurobox/
│   │   ├── api.py                    14 支 API 路由
│   │   ├── models.py                 Door / RobotState
│   │   ├── services.py               業務邏輯與狀態協調
│   │   ├── tasks.py                  背景任務（QR 顯示、位置輪詢、召回）
│   │   ├── robot.py                  FlashbotController
│   │   ├── pudu_client.py            Pudu API client 與簽章
│   │   └── app.py / config.py / cli.py / utils.py
│   ├── scripts/                      check_db.py、read_maps_and_position.py
│   └── tests/
│
├── 技術文件/                      所有設計與操作文件，見下方索引
├── render.yaml                   Render Blueprint
└── README.md
```

---

## 快速開始（開發環境）

兩個服務要分別啟動，各自需要自己的 PostgreSQL 與 `.env`。**`.env` 不進版控**。

### line-backend

```bash
cd line-backend
python -m venv venv && venv\Scripts\activate      # Windows
pip install -r requirements.txt
python -m app.init_db                              # 建表
uvicorn app.main:app --reload
```

`.env` 需要：

```env
LINE_CHANNEL_SECRET=
LINE_CHANNEL_ACCESS_TOKEN=
LIFF_ID=                      # 掃碼取貨頁
LIFF_ID_RETURN=               # 退貨申請頁
LINE_LOGIN_CHANNEL_ID=        # 驗證 LIFF ID Token 用
DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/aurobox
ROBOT_API_BASE_URL=http://localhost:5000
ROBOT_HOME_POINT_NAME=office
ADMIN_USERNAME=
ADMIN_PASSWORD=
```

Windows 上可直接用 `start-server.bat`；LINE Webhook 需要對外網址，開發時用 `start-ngrok.bat`。

- Dashboard：<http://localhost:8000/admin>
- API 文件：<http://localhost:8000/docs>

### flashbot-robot

需要 Python 3.10+。

```bash
cd flashbot-robot
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e .
python3 -u run.py --debug
```

`.env` 需要 Pudu 憑證（`Pd_key` / `Pd_secret` / `Aurotek_id`）、`FLASHBOT_SN`、地圖與點位名稱、`DATABASE_URL`、`CENTRAL_API_BASE_URL`（指回 line-backend）。完整範例見 `flashbot-robot/.env.example`。

未設 `DATABASE_URL` 時會退回本機 SQLite（`instance/aurobox.db`），僅適合單機開發。

CLI 可直接驗證與 Pudu 的連線：

```bash
aurobox --sn <SN> status
aurobox --sn <SN> position
aurobox --sn <SN> map-list
```

---

## 部署

`render.yaml` 定義三個資源：

| 資源 | 型別 | rootDir |
|---|---|---|
| `aurobox-postgres` | PostgreSQL | — |
| `line-backend` | Web Service（對外） | `line-backend` |
| `flashbot-robot` | Private Service（僅內網） | `flashbot-robot` |

`flashbot-robot` 是 private service，只有同一個 Render 專案內的服務連得到。部署完成後要到它的 **Connect** 分頁複製 Internal Address，前面加 `http://` 填進 `line-backend` 的 `ROBOT_API_BASE_URL`——這個值帶隨機後綴，第一次部署完才知道。

其餘環境變數標記 `sync: false`，需在 Render Dashboard 手動填入。

詳細步驟見 `技術文件/Render部署文件/`。

---

## 文件索引

| 文件 | 內容 |
|---|---|
| `技術文件/Aurobox-開發文件.md` | 系統整體設計、模組職責、送件／退件時序 |
| `技術文件/BackendAPI-清單.md` | 39 支路由依呼叫者分組，含認證與狀態轉換 |
| `技術文件/Dashboard說明文件/` | Dashboard 建置方式、視覺系統、四頁功能（含截圖） |
| `技術文件/Diagrams/Aurobox-ER/` | ER 圖與資料表描述（Data Dictionary） |
| `技術文件/Diagrams/Aurobox-UserFlow_v3.drawio` | 住戶／系統／管理員三泳道的完整流程 |
| `技術文件/Diagrams/Aurobox-Component v2.drawio` | 元件圖 |
| `技術文件/Diagrams/Aurobox-Sitemap v1.drawio` | Dashboard 與 LIFF 頁面地圖 |
| `技術文件/Diagrams/Package-state-and-Robot-motion_v2.md` | 包裹狀態機與機器人動作對照 |
| `技術文件/Diagrams/System-Architecture.md` | 系統架構 |
| `技術文件/管理員操作手冊/` | 管理員 SOP（md / docx / pdf） |
| `技術文件/LINE部署文件/` | LINE Channel、LIFF、圖文選單設定 |
| `技術文件/Render部署文件/` | Render 部署與使用說明 |
| `技術文件/虛擬機操作紀錄.txt` | 公司 VM 部署測試紀錄 |

各元件的實作細節另見 `line-backend/README.md` 與 `flashbot-robot/README.md`。

---

## 開發須知

**時區不一致。** `line-backend` 存台灣當地時間的 naive datetime，`flashbot-robot` 存 UTC naive。**比對兩庫的時間戳前必須先換算 8 小時**，資料庫本身沒有任何欄位標示這件事。

**艙門開關只有一個入口。** Dashboard 主控台「機器人狀態」欄的開啟／關閉艙門按鈕是唯一入口，例外處理頁不含任何艙門按鈕。這個界線是刻意的——例外處理頁只負責決策（銷案／重新派貨／補發通知），不碰硬體。

**機器人回管理室時艙門保持關閉**，要等管理員在 Dashboard 按「開門」才真的開，包裹不會在無人看管下暴露。
