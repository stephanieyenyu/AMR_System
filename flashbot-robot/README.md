# Aurobox 0.5.0

Aurobox 是一套針對 Pudu Flashbot 的控制與協調層，負責處理門口任務、包裹狀態流轉、機器人派送與回收流程，並透過 HTTP API 與 CLI 提供操作介面。

## 0.5.0 版本重點

- 完整整理專案文件，將版本說明統一更新為 0.5.0。
- 補齊 README 與 REPORT 的使用說明與流程描述。
- 保留目前已實作的門任務、包裹派送、回收與召回流程說明。
- 提供安裝、設定、執行與測試的基礎操作指南。

## 功能概覽

Aurobox 目前支援以下核心能力：

- 包裹分配與門任務建立
- 門口裝載與狀態更新
- 機器人派送與 QR 驗證
- 揀取完成、完成結案與取消流程
- 返回流程與召回流程
- Dashboard 狀態查詢

## 系統架構

```mermaid
flowchart LR
  Client[Client / CLI / Webhook] --> API[Flask API]
  API --> DB[(PostgreSQL)]
  API --> Controller[FlashbotController]
  Controller --> Pudu[Pudu Open Platform]
  API --> Tasks[Background Tasks]
  Tasks --> API
```

## 專案結構

```text
src/aurobox/
  api.py           # API 路由與流程入口
  app.py           # Flask app factory
  config.py        # 環境變數與設定載入
  models.py        # Door / RobotState / 任務模型
  services.py      # 業務邏輯與狀態協調
  tasks.py         # 背景任務與流程處理
  robot.py         # FlashbotController
  pudu_client.py   # Pudu API client 與簽章處理
  utils.py         # payload 與工具函式
  cli.py           # CLI 入口

scripts/
  check_db.py
  read_maps_and_position.py

tests/
  test_pudu_client.py
  test_api_integration.py
  load_test.py
```

## 主要資料狀態

### Door 狀態

- empty：門口沒有待處理任務
- assigned：已被指派但尚未開始
- full：門口已滿載
- picking：正在處理揀取

### RobotState 狀態

- last_point：機器人最近位置
- current_task_id：目前執行中的任務 ID

## 主要流程

### 1. 包裹指派

- POST /api/packages/{package_id}/assign
- 會依據數量與門口狀態進行分配
- 若門口已滿，返回 409

### 2. 門口裝載

- POST /api/doors/load
- 將 assigned 的任務推進為可裝載狀態

### 3. 機器人派送

- POST /api/robot/dispatch
- 由 door_task_id 與 units 啟動任務
- 會觸發 Pudu 相關的 arrived 與 QR 驗證流程

### 4. 揀取與完成

- POST /api/packages/{package_id}/pickup-complete
- POST /api/packages/{package_id}/complete
- 會將狀態由 picking 推進為完成或結案

### 5. 取消與返回

- POST /api/packages/{package_id}/cancel
- POST /api/packages/return
- POST /api/packages/return-open
- POST /api/doors/return-complete
- POST /api/doors/return-timeout

### 6. 召回流程

- POST /api/robot/recall
- 可針對 assigned 或 picking 的任務進行回收

## API 摘要

### 基本健康檢查

- GET /
- GET /healthz

### 任務與門口流程

- POST /api/door-tasks/<door_task_id>/assign
- POST /api/door-tasks/<door_task_id>/assign-timeout
- POST /api/doors/load
- POST /api/robot/dispatch
- POST /api/door-tasks/<door_task_id>/pickup-complete
- POST /api/door-tasks/<door_task_id>/complete
- POST /api/door-tasks/<door_task_id>/cancel
- POST /api/door-tasks/return
- POST /api/doors/return-open
- POST /api/doors/return-complete
- POST /api/doors/return-timeout
- POST /api/robot/recharge
- POST /api/robot/recall
- GET /api/dashboard/status

## 需求與環境

- Python 3.10+
- PostgreSQL 14+
- requests
- flask
- flask-sqlalchemy
- python-dotenv
- cryptography
- psycopg2-binary

## 安裝與啟動

### 1. 建立虛擬環境

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

### 2. 安裝與啟動 PostgreSQL

建議直接在本機安裝 PostgreSQL，不依賴 Docker。以下以 Ubuntu / Debian 為例：

```bash
sudo apt update
sudo apt install -y postgresql postgresql-contrib
sudo systemctl enable postgresql
sudo systemctl start postgresql
```

建立資料庫與使用者：

```bash
sudo -u postgres psql
CREATE USER myuser WITH PASSWORD 'mypassword';
CREATE DATABASE aurobox_db OWNER myuser;
GRANT ALL PRIVILEGES ON DATABASE aurobox_db TO myuser;
\q
```

若你想要使用 Docker 也可以，但這不是必要條件：

```bash
docker run --name aurobox-postgres \
  -e POSTGRES_USER=myuser \
  -e POSTGRES_PASSWORD=mypassword \
  -e POSTGRES_DB=aurobox_db \
  -p 5432:5432 \
  -d postgres:15
```

### 3. 建立環境變數

建立 .env，範例如下：

```env
Pd_key=YOUR_PUDU_API_KEY
Pd_secret=YOUR_PUDU_API_SECRET
Aurotek_id=YOUR_SHOP_ID
FLASHBOT_SN=8FF055923050007
DEFAULT_MAP_NAME=YOUR_MAP_NAME
HOME_POINT_NAME=office
CHARGE_POINT_NAME=charging_point
DOOR_MODE=4_DOORS
DATABASE_URL=postgresql://myuser:mypassword@localhost:5432/aurobox_db
CENTRAL_API_BASE_URL=https://your-central-api.example.com
```

### 4. 啟動服務

```bash
python3 -u run.py --debug
```

啟動後可透過以下網址存取：

- http://0.0.0.0:5000

## CLI 範例

```bash
aurobox --sn 8FF055923050007 status
aurobox --sn 8FF055923050007 position
aurobox --sn 8FF055923050007 map-list
aurobox --sn 8FF055923050007 recharge
aurobox --sn 8FF055923050007 --shop-id YOUR_SHOP_ID open-map --map-name map1
aurobox --sn 8FF055923050007 --shop-id YOUR_SHOP_ID call --map-name map1 --point charging_point
```

## 除錯與觀察

- 可於 instance/robot_commands.log 查看機器人指令紀錄
- 可使用 scripts/check_db.py 檢查資料庫內容
- 可使用 scripts/read_maps_and_position.py 讀取地圖與位置資訊

## 測試

目前測試涵蓋：

- tests/test_pudu_client.py
- tests/test_api_integration.py
- tests/load_test.py

可使用以下指令執行：

```bash
pytest
```

## 版本說明

- 0.5.0：文件與流程說明整合更新，作為完整的 0.5.0 發布文件版本

