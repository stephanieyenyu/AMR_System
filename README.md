# Aurobox — 社區 AMR 包裹配送系統

單一 repo 底下兩個資料夾，各自是獨立的服務：

- **`line-backend/`**：LINE 後端 + 管理員 Dashboard，業務邏輯與狀態真相來源
- **`flashbot-robot/`**：機器人硬體控制層，串接 Pudu 開放平台 API

`line-backend` 和`flashbot-robot` 沒有共用資料庫，純粹透過 HTTP API 溝通。目前部署在 **Render**，`line-backend`、`flashbot-robot` 各自是獨立的 Web Service。

```
AMR-System/
├── render.yaml                    ← Render 服務設定（目前服務是手動建立，這份檔案不會被讀取，純文件用途）
├── README.md                      ← 本檔案
├── Aurobox-開發文件.md             ← 完整開發文件（架構、資料流、模組說明、除錯指南）
├── Render部署指南.md               ← 從零開始建立一份全新 Render 部署
├── Render使用說明.md               ← 系統已部署好之後，日常怎麼使用
│
├── line-backend/
│   ├── app/
│   │   ├── main.py                ← 路由、業務邏輯、排程任務
│   │   ├── models.py              ← Package / LineBinding / PackageRecipient / TaskLog
│   │   ├── config.py
│   │   ├── db.py
│   │   ├── init_db.py
│   │   ├── line_messaging.py      ← LINE Flex Message 建構
│   │   ├── line_verify.py
│   │   ├── templates/             ← 四個管理頁面的 Jinja2 樣板
│   │   └── static/                ← 對應的 CSS/JS
│   ├── tests/
│   ├── requirements.txt
│   ├── migrate_add_*.py           ← 一次性資料庫欄位遷移腳本
│   ├── start-server.bat           ← 本機開發用
│   └── start-ngrok.bat            ← 本機開發用
│
└── flashbot-robot/
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
    ├── run.py
    ├── pyproject.toml
    ├── README.md
    └── REPORT.md
```

---

## 想了解系統架構、怎麼開發新功能？

完整的系統架構、資料流程圖、兩個模組的關鍵檔案說明、Pudu API 清單、色彩系統/共用函式規範、以及新增功能/除錯的具體步驟，都寫在：

📄 **[`Aurobox-開發文件.md`](./Aurobox-開發文件.md)**

## 想部署 / 想知道怎麼用？

- 系統**已經部署好了**，日常只要打開瀏覽器連到 Render 給的網址就能用，不需要安裝任何東西 → 📄 **[`Render使用說明.md`](./Render使用說明.md)**
- 想**重新建立一份全新的部署**（例如要開第二套測試環境，或這套環境需要重建）→ 📄 **[`Render部署指南.md`](./Render部署指南.md)**

## 本機開發

如果要在本機起服務測試（不透過 Render），`line-backend/` 底下的 `start-server.bat`／`start-ngrok.bat` 是本機開發用的啟動腳本，需要自己準備本機 PostgreSQL 並設定對應的環境變數（`LINE_CHANNEL_SECRET`、`DATABASE_URL` 等，見 `app/config.py`）。`flashbot-robot/` 同理，用 `python run.py` 啟動，環境變數見 `src/aurobox/config.py`。

---

## 已知注意事項

1. **`render.yaml` 目前不會被 Render 讀取**——兩個服務是手動用「New → Web Service」建立的，不是用「New → Blueprint」建立，只有透過 Blueprint 管理的服務才會讀這個檔案。這個檔案目前純粹是文件用途，實際部署設定要在 Render Dashboard 各服務的 Settings 頁面裡改。
2. **`flashbot-robot` 沒有 `requirements.txt`**，是用 `pyproject.toml` 管理套件（`pip install .`）。
3. **`flashbot-robot/tests/` 裡的測試疑似跟現行 `api.py` 不同步**（沿用舊有已知問題，尚未確認是否已修正，見開發文件第 5.5 節）。

其餘細節（環境變數命名、Render 平台限制、資料庫欄位遷移流程等）都寫在《Aurobox-開發文件.md》與《Render部署指南.md》裡，不在這裡重複。
