# Aurobox — 社區 AMR 包裹配送系統（合併專案）

本專案原本分別放在 GitHub `Liao-YuSheng/Aurobox` 這個 repo 的兩個獨立 branch：

- **`main`**：LINE 後端 + 管理員 Dashboard（負責業務邏輯與狀態真相來源）
- **`flashbot`**：機器人硬體控制層，串接 Pudu 開放平台 API

這裡把兩個 branch 的內容各自完整保留，合併成同一個資料夾下的兩個子專案，並補上一份共用的 `docker-compose.yml`，讓兩個服務可以在同一個環境裡一起啟動、直接用容器名稱互相溝通（開發期兩邊各自需要 ngrok 對外的作法，在這個合併專案的容器化部署下不再需要）。

```
aurobox/
├── docker-compose.yml       ← 新增：一次啟動兩個服務 + 各自的 PostgreSQL
├── .gitignore               ← 新增：合併專案頂層規則
├── README.md                ← 本檔案
├── docs/
│   └── 開發文件.md           ← 完整開發文件（架構、資料流、模組說明、除錯指南）
│
├── line-backend/            ← 原 main branch 全部內容（未更動）
│   ├── app/
│   │   ├── main.py
│   │   ├── models.py
│   │   ├── config.py
│   │   ├── db.py
│   │   ├── init_db.py
│   │   ├── line_messaging.py
│   │   └── line_verify.py
│   ├── tests/
│   ├── requirements.txt
│   ├── Dockerfile
│   ├── .env.example
│   ├── start-server.bat
│   └── start-ngrok.bat
│
└── flashbot-robot/           ← 原 flashbot branch 全部內容（未更動）
    ├── src/aurobox/
    │   ├── app.py
    │   ├── api.py
    │   ├── robot.py
    │   ├── pudu_client.py
    │   ├── services.py
    │   ├── tasks.py
    │   ├── models.py
    │   ├── utils.py
    │   ├── config.py
    │   └── cli.py
    ├── tests/
    ├── scripts/
    ├── docs/                 ← 機器人團隊自己的架構規劃文件（原本就有，與上面的 docs/ 是不同資料夾）
    ├── run.py
    ├── pyproject.toml
    ├── Dockerfile
    ├── .env.example
    ├── README.md
    └── REPORT.md
```

> **關於「合併」的範圍說明**：`line-backend/` 與 `flashbot-robot/` 兩個資料夾內部的程式碼**完全沒有更動**，就是原本兩個 branch 的內容（各自的 `README.md`、`REPORT.md` 也都保留原樣）。新增的只有頂層的 `docker-compose.yml`、`.gitignore`、這份 `README.md`，以及 `docs/開發文件.md`。這樣未來如果要拆回兩個獨立 repo/branch，也只要把這兩個資料夾各自搬出去即可，不會有交叉污染的風險。

---

## 快速開始

### 1. 設定環境變數

```bash
cp line-backend/.env.example line-backend/.env
cp flashbot-robot/.env.example flashbot-robot/.env
```

編輯這兩份 `.env`，至少要填：

| 檔案 | 必填變數 |
|---|---|
| `line-backend/.env` | `LINE_CHANNEL_SECRET`、`LINE_CHANNEL_ACCESS_TOKEN`、`LINE_LOGIN_CHANNEL_ID`、`LIFF_ID`、`LIFF_ID_RETURN`、`ADMIN_USERNAME`、`ADMIN_PASSWORD`（`.env.example` 裡沒列出這兩個，但 `app/config.py` 有預設值 `aurotek`/`flashbot`，正式使用務必在 `.env` 覆寫） |
| `flashbot-robot/.env` | `Pd_key`、`Pd_secret`、`Aurotek_id`、`FLASHBOT_SN`、`DEFAULT_MAP_NAME`、`HOME_POINT_NAME`、`CHARGE_POINT_NAME`、`DOOR_MODE` |

**兩份 `.env` 裡的 `DATABASE_URL`，用 docker-compose 啟動時要改成指向 compose 內的資料庫服務名稱**（不是 `localhost`）：

```env
# line-backend/.env
DATABASE_URL=postgresql+psycopg://postgres:postgres@db-line:5432/aurobox_line

# flashbot-robot/.env
DATABASE_URL=postgresql://myuser:mypassword@db-robot:5432/aurobox_db
```

（帳號密碼要對應 `docker-compose.yml` 裡 `db-line` / `db-robot` 兩個服務目前設定的 `POSTGRES_USER` / `POSTGRES_PASSWORD`，要改密碼的話兩邊要一起改。）

`ROBOT_API_BASE_URL`（line-backend 用）與 `CENTRAL_API_BASE_URL`（flashbot-robot 用）**不用改**——`docker-compose.yml` 已經用 `environment:` 區塊覆寫成 `http://flashbot-robot:5000` 與 `http://line-backend:8000`，會蓋掉 `.env` 裡開發用的 ngrok 網址。

### 2. 啟動

```bash
docker compose up --build
```

啟動後：

- LINE 後端 API / 管理員 Dashboard：`http://localhost:8000`（Dashboard 在 `/admin`，FastAPI Swagger 文件在 `/docs`）
- 機器人控制模組 API：`http://localhost:5000`
- 兩個 PostgreSQL 分別開在本機 `5433`（對應 line-backend）與 `5434`（對應 flashbot-robot），方便用 DBeaver/psql 之類的工具直接檢查資料。

### 3. 建立資料表

兩個服務容器啟動時**不會**自動幫你跑資料庫 migration：

```bash
# line-backend：第一次啟動需要手動建表
docker compose exec line-backend python -m app.init_db

# flashbot-robot：db.create_all() 已內建在 create_app() 裡，容器啟動時會自動建表
# （不需要手動執行，但艙門預設值重置也是在這個時機做的，第一次啟動請確認 log 沒有報錯）
```

### 4. 查看 Log

```bash
docker compose logs -f line-backend
docker compose logs -f flashbot-robot

# 機器人與 Pudu API 的逐筆指令/回應紀錄（獨立於上面的容器 log，已用 volume 保留）
docker compose exec flashbot-robot cat instance/robot_commands.log
```

---

## 想了解架構與後續怎麼開發？

完整的系統架構、資料流程圖、兩個模組的關鍵檔案說明、Pudu API 清單、以及未來新增功能/除錯注意事項，都寫在：

📄 **[`docs/開發文件.md`](./docs/開發文件.md)**

其中也記錄了合併/分析過程中發現的幾個現有程式碼問題（例如兩邊埠號設定不一致、部分測試與現行 API 路由脫節、README 與程式碼有落差的地方等），建議接手維護前先讀過一遍。

---

## 已知注意事項（合併當下發現，尚未修正於原始程式碼）

這些是把兩個 branch 內容組合起來後觀察到的既有問題，**沒有更動任何一邊的原始程式碼**，只在這裡列出來供你決定是否/如何修正：

1. **`flashbot-robot` 的埠號設定原本互相矛盾**：`run.py` 的 `--port` 參數預設是 `6000`，但 `Dockerfile` 只 `EXPOSE 5000` 且沒有在 `CMD` 帶入 `--port`，`README.md` 又寫預設是 `5000`。本專案的 `docker-compose.yml` 已經在 `command:` 明確指定 `--port 5000`，讓實際監聽的埠與對外文件一致，但如果你直接用 `flashbot-robot/Dockerfile` 單獨建置容器（不透過這份 compose），仍然會遇到原本的埠號不一致，需要自行注意。
2. **`line-backend/.gitignore` 檔尾原本殘留一行未清乾淨的 Git merge conflict 標記**（`>>>>>>> 8bb6076f...`）。這份合併專案的頂層 `.gitignore` 是重新寫過的乾淨版本，但原本 `line-backend/.gitignore` 這個檔案本身**還留著那個問題**，建議找時間回原本的 `main` branch 修掉。
3. 其餘（測試與現行 API 路由不同步、部分 docstring 落後於實際上鎖邏輯等）詳見 `docs/開發文件.md` 第 5 章。
