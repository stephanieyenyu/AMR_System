# Aurobox LINE 後端 + 管理員 Dashboard

智連櫃社區 AMR 自動配送系統，三大元件（LINE 後端、機器人端、機器人硬體）中負責 **LINE 對話與管理員 Dashboard** 的部分。本 repo 是唯一的業務邏輯與狀態真相來源：決定包裹目前是什麼狀態、什麼時候該叫機器人做什麼事；機器人端只負責收指令、操作硬體，不重複維護這些業務狀態。

---

## 技術棧

- **後端**：Python / FastAPI / Uvicorn
- **資料庫**：PostgreSQL + SQLAlchemy
- **LINE 整合**：LINE Messaging API v3 SDK、LIFF（掃碼取貨頁、退貨申請頁）
- **排程**：APScheduler
- **開發期對外連線**：ngrok
- **管理員 Dashboard**：內嵌在 `main.py` 裡的原生 HTML + Vanilla JS，無框架、無建置流程

---

## 專案結構

```
line-backend/
├── requirements.txt
├── start-server.bat
├── start-ngrok.bat
└── app/
    ├── config.py         讀取 .env 環境變數
    ├── db.py             資料庫連線設定
    ├── models.py         Package / LineBinding / PackageRecipient / TaskLog
    ├── init_db.py        建立資料表用的腳本
    ├── line_verify.py     驗證 LIFF 傳來的 ID Token
    ├── line_messaging.py  封裝呼叫 LINE Messaging API（推播、Flex Message、datetimepicker）
    └── main.py            FastAPI 主程式、Webhook、所有 API 路由、管理員 Dashboard
```

---

## 資料模型

### `Package`：核心狀態機所在

| 欄位 | 說明 |
|---|---|
| `unit` / `line_user_id` | 門牌 / 收件人 LINE User ID |
| `status` | 狀態機（見下方） |
| `task_type` | `delivery`（送貨，管理員建立）或 `return`（退貨，住戶主動申請） |
| `package_count` | 固定為 1；一次多件是靠建立多筆各自 `package_count=1` 的紀錄、用 `creation_batch_id`／`door_task_id` 分組加總呈現，不是單一欄位直接存件數 |
| `door_id` / `door_task_id` | 分配的艙門編號 / 這扇門「這一次」被使用的任務ID。同一個 `door_task_id` 底下所有包裹，狀態轉換（抵達/驗證/完成/拒收/逾時）全部綁在一起走 |
| `creation_batch_id` | 建立包裹時 quantity>1，同一批 N 筆共用此值，只發一次到貨通知，但住戶按取貨/預約/不收會一次套用到整批 |
| `door_assigned_at` | 艙門分配（放置包裹開門）時間，逾時判斷用 |
| `stop_dispatched_at` | 這一站真正呼叫 `/api/robot/dispatch` 派送出去的時間，防止並發重複派送同一站 |
| `arrived_at` | 機器人抵達時間，逾時判斷用 |
| `scheduled_pickup_at` | 住戶預約取貨的時間（整點），到這個時間前不能放置/派送 |
| `returned_at` | 機器人實際返回管理室的時間（拒收/逾時退回專用，此時門還沒開） |
| `return_door_opened_at` / `door_closed_at` | 管理員開門檢查 / 取出包裹後關門的時間 |
| `return_retrieved_at` | 退貨任務：管理員確認已從艙門取出退貨件的時間 |
| `acknowledged_at` | 不收（voided）的包裹，管理員按「確定」已知悉的時間 |
| `case_closed_at` | 銷案時間 |
| `redispatched_at` / `redispatched_to` | 例外處理頁按「重新派貨」的時間 / 新建立的包裹 ID |
| `pending_pickup_notified_at` | 例外處理頁「通知住戶」的時間，只能通知一次 |

### `LineBinding`：門牌綁定

`line_user_id`（主鍵）、`unit`、`name`、`status`（active/inactive）、`solo_notify`（是否限本人接收到貨通知）。

### `PackageRecipient`：一筆包裹通知過的所有人

同門牌多人綁定時，一筆包裹可能同時通知多位收件人，任何一人操作（取貨/拒收/退貨取消）都會套用到整筆包裹。

### `TaskLog`：完整事件稽核紀錄

每日報表的資料來源，記錄從建立包裹到銷案的每一個關鍵事件（`created`／`door_assigned`／`dispatched`／`arrived`／`completed`／`rejected_at_door`／`returned_timeout`／`redispatched`／`case_closed`／`return_requested`／`return_retrieved`／`return_cancelled` 等數十種 `event_type`，完整清單見 `models.py` 註解），取代舊版只用 `print()` 印 console、服務重啟就消失的做法。

---

## 核心概念

### 包裹狀態機

```
pending ──▶ pickup_now ──▶ delivering ──▶ arrived ──▶ completed
                                              │
                                              ├──▶ rejected_at_door（拒收 / 退貨取消）
                                              └──▶ returned_timeout（逾時未處理）
pending ──▶ voided（到貨當下直接不收）
```

- `pending`：包裹剛登記，已通知住戶，等待住戶回應
- `pickup_now`：住戶確認要收（或已預約時段），等待管理員放置包裹（分配艙門）
- `delivering`：已裝載、機器人正在前往這一站的路上（或排隊等前面的站處理完）
- `arrived`：機器人已抵達，住戶可以掃碼取貨 / 放入退貨物品
- `completed`：任務結束。送貨的 `completed` 代表艙門已釋放；退貨的 `completed` 代表物品剛被放入、艙門仍是滿的，要等管理員按「確認取出」（`return_retrieved_at`）
- `rejected_at_door`：送貨被拒收，或退貨被住戶取消（兩者共用同一個狀態值，走同一套「機器人帶回→開門/關門→銷案」流程，靠 `task_type` 分辨顯示文字）
- `returned_timeout`：機器人抵達後住戶超過時限沒有任何動作，系統自動觸發退回
- `voided`：住戶在到貨通知當下直接按「不收」，包裹從沒出過門

### `door_task_id`：以「一站」為分組單位

一位收件人在同一個門牌、同一種任務類型底下，可能同時用到不只一扇艙門。這些包裹共用同一個 `door_task_id`：一次 dispatch、一次抵達回報，就能讓整組一起轉成 `arrived`；一次掃碼、一次「完成」，就能一次關閉整組的門。

**同一戶如果同時有送貨、退貨兩個任務，是兩個獨立的 `door_task_id`**（`task_type` 不同）：不會共用同一扇艙門，也不會互相假設對方的狀態——機器人到了其中一個任務的地點，不代表另一個任務也一起處理好了，兩邊都要各自收到明確的 dispatch/arrived 事件才會前進。艙門佔用判斷（`is_door_actively_held`）也會擋掉「同收件人同門牌但不同 `task_type`」共用同一扇艙門的情況。

### 預約取貨：半點捨入規則

LINE 原生 datetimepicker 選時間，以半點為捨入基準：選 9:00–9:30（含半點）算「9–10」時段，9:31–9:59 算「10–11」時段。時間滾輪最早可選時間直接用「現在」，不會進位到下一個整點，否則會把當前小時整段擋掉、選不到本該可以選的時間；「是不是未來時段」的驗證則是拿使用者實際選的時間比較，不是拿捨入後的時段起點比較（避免捨入後時段起點早於現在、被誤判成過去）。每個時段有上限戶數。

---

## LINE 使用者功能

- **綁定門牌**：輸入「門牌 姓名」（例如 `5F-1 王小明`），僅需一次
- **收件**：到貨通知可選「取貨」「預約取貨」或「不收」；機器人抵達後掃碼開門、取出包裹按「取貨完成」；抵達後也可以按「拒收」
- **退貨**：圖文選單「退貨」→ 選件數送出（LIFF 頁面）→ 機器人抵達後掃碼開門、放入物品後按「放貨完成」；抵達後也可以按「暫時不退貨」取消
- **我的包裹**：查詢自己名下所有還沒結束的包裹狀態，含退回包裹的作廢倒數時間
- **關門**：忘記按「完成」時的補救指令
- **通知設定**：同門牌多人時，可切換「限本人接收到貨通知」或「同門牌所有人都收到」
- **使用說明**：輸入「使用說明」隨時查看完整操作指引

---

## 管理員 Dashboard

HTTP Basic Auth 保護，四個頁面：

| 路徑 | 功能 |
|---|---|
| `/admin` | 主控台：建立包裹（支援一次多件）、機器人即時狀態與艙門狀態、包裹清單（放置/派送）、門牌查詢、艙門手動開關 |
| `/admin/reports` | 每日報表：包裹狀態統計、完整任務時間軸（`TaskLog`） |
| `/admin/exceptions` | 例外處理：拒收/逾時/退貨取消/不收 待處理清單，開關門確認、銷案、補發通知、重新派送、確認退貨取出 |
| `/admin/residents` | 住戶管理：LINE 綁定紀錄查詢與維護 |

主要管理操作：

- **建立包裹 / 放置 / 全部派送**：登記包裹（quantity>1 會建立多筆各自 `package_count=1` 的紀錄，共用 `creation_batch_id`）→ 分配艙門 → 批次派送給機器人
- **緊急召回 / 回充電**
- **艙門手動開/關**：機器人狀態欄的開關門鍵，是艙門開關的唯一入口
- **例外處理頁**：拒收/逾時/退貨取消的包裹，開門取出、關門、銷案；針對已結案但異常的包裹可以重新派貨（`redispatched_at`/`redispatched_to` 記錄新舊包裹關聯）或手動聯繫住戶

---

## 排程任務（APScheduler）

| 排程 | 週期 | 用途 |
|---|---|---|
| `check_pickup_timeout` | 1 分鐘 | 送貨 `arrived` 超過時限自動觸發拒收流程 |
| `check_assign_timeout` | 1 分鐘 | 艙門分配了但管理員一直沒裝載，逾時釋放艙門 |
| `check_return_timeout` | 1 分鐘 | 退貨 `arrived` 超過時限自動觸發退回流程 |
| `poll_robot_returned` | 20 秒 | 輪詢機器人是否已回到管理室，更新對應包裹 |
| `check_stuck_dispatch` | 2 分鐘 | 安全網：有進行中的任務、但沒有其他事件會觸發下一步時，主動重試派送邏輯 |

---

## 對外呼叫：機器人端 API

透過 `ROBOT_API_BASE_URL` + `/api/...` 呼叫，統一走 `call_robot_api()` 封裝，具備逾時、重試（`retries=1`）與失敗記錄（寫入 `TaskLog`）：

| 端點 | 用途 |
|---|---|
| `POST /door-tasks/{id}/assign` | 指派艙門 |
| `POST /door-tasks/{id}/assign-timeout` | 指派了但沒裝載，逾時釋放艙門 |
| `POST /doors/load` | 批次關閉已裝載的艙門 |
| `POST /robot/dispatch` | 派機器人前往某一站 |
| `POST /door-tasks/{id}/pickup-complete` | 住戶掃碼驗證通過，開門 |
| `POST /door-tasks/{id}/complete` | 取貨完成／放貨完成，關門 |
| `POST /door-tasks/{id}/cancel` | 拒收／暫時不退貨，關門+收任務畫面 |
| `POST /door-tasks/return` | 帶著拒收/退貨包裹返回管理室 |
| `POST /doors/return-open` | 返回管理室後開門供管理員檢查 |
| `POST /doors/return-complete` | 管理員確認取出，關門釋放艙門 |
| `POST /doors/return-timeout` | 開著檢查太久沒關，強制關門 |
| `GET /dashboard/status` | Dashboard 顯示機器人即時狀態、艙門狀態 |
| `POST /robot/recall` | 緊急召回 |
| `POST /robot/recharge` | 回充電站 |

---

## 快速開始（開發環境）

```bash
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

建立 `.env`：

```env
LINE_CHANNEL_SECRET=
LINE_CHANNEL_ACCESS_TOKEN=
LIFF_ID=
LIFF_ID_RETURN=
LINE_LOGIN_CHANNEL_ID=
DATABASE_URL=postgresql+psycopg://postgres:密碼@localhost:5432/aurobox_line
ROBOT_API_BASE_URL=
ROBOT_HOME_POINT_NAME=
ADMIN_USERNAME=
ADMIN_PASSWORD=
APP_ENV=development
```

```bash
python -m app.init_db
uvicorn app.main:app --reload --port 8000
```

開發階段用 ngrok 對外連線，Webhook URL 設為 `https://網址/webhook`，兩個 LIFF App 的 Endpoint URL 分別設為 `/liff/scan`（取貨掃碼）與 `/liff/return-request`（退貨申請）。啟動後開 `http://localhost:8000/docs` 可互動測試所有 API。

---

## 已知限制

- `handle_postback` 的「不收」（`REJECT`）分支在多收件人情境下，通知同門牌其他人的段落引用了一個未定義變數（`triggered_name`），會被外層 `except` 吞掉、不影響主流程，但那則「已取消收件」的通知不會真的送出——尚未修復
