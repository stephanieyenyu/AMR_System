# Aurobox 0.4.0 全目錄盤點與多包裹配送更新報告

完成日期: 2026-07-22  
文件版次: 0.4.0  
程式套件版號: 0.4.0

## 1. 報告目的

本報告依據目前工作區內容，重整 Aurobox 專案文件，使主軸完整對齊「多包裹配送」流程。

本次已檢視範圍包含：

- 根目錄文件與設定：README.md、REPORT.md、pyproject.toml、run.py、.env.example、.gitignore
- 核心程式：src/aurobox 全部 Python 檔
- 測試與腳本：tests、scripts

## 2. 系統現況摘要

### 2.1 系統定位

Aurobox 為 Flashbot 本地硬體控制層，負責：

- 導航指令與回充/返航
- 艙門狀態管理與批次開關門
- 配送狀態同步與任務畫面控制
- 與中央系統進行抵達通知回呼

### 2.2 多包裹能力已上線

關鍵依據來自 api.py、models.py、tasks.py、services.py：

- assign 支援 quantity
- door 狀態支援 assigned/full/picking
- 同一包裹可綁定多個 door_number
- 批次 control_doors 可一次操作多門
- 配送完成後可依全門狀態判斷返航

## 3. 0.4.0 多包裹流程盤點

### 3.1 分配與裝貨

- 入口：POST /api/packages/{package_id}/assign
- quantity 預設 1，可指定多門需求
- 若有 FULL 門，直接 409 擋下（避免配送中又再裝貨）
- 空門挑選使用 with_for_update(skip_locked=True) 防超賣
- 分配後背景任務等待機器人抵達管理室，依序開門

### 3.2 出發與到站

- 入口：POST /api/doors/load
- 將 ASSIGNED 門批次關門並轉 FULL
- 入口：POST /api/robot/dispatch
- 建立 task_id，背景輪詢抵達後：
  - 呼叫中央系統 packages/{id}/arrived
  - 顯示 QR 內容至機器人螢幕

### 3.3 取件、完成、取消

- pickup-complete：清除任務畫面、開門、狀態轉 PICKING
- complete：關門並釋放該包裹所有門；若全部清空則返航
- cancel：關門後維持 FULL（保護包裹仍在艙內）

### 3.4 退件與緊急召回

- return：機器人回管理室
- return-open：管理員檢查時批次開啟啟用門
- return-complete：批次關門並釋放門資源
- return-timeout：逾時強制關門
- recall：取消當前任務，等待硬體重置後強制返航，並將 assigned/picking 保護為 full

## 4. 資料模型與併發控制

### 4.1 Door 資料結構

- 主鍵：id
- 業務鍵：sn + door_number（唯一）
- 狀態：empty、assigned、full、picking
- 護欄：只允許 H_01/H_02/H_03/H_04

### 4.2 RobotState 資料結構

- sn（唯一）
- last_point
- current_task_id

### 4.3 高併發策略

- 分配空門時使用資料列鎖與 skip_locked
- 多執行緒開門用全域 lock + queue 集中處理
- 狀態提交後才開門/關門，降低競態造成的不一致

## 5. 狀態整合策略（Pudu 多來源）

FlashbotController.get_status_summary 會合併三來源：

- v1 status/get_by_sn
- v2 status/get_by_sn
- task/state/get

並輸出穩定欄位：

- state
- move_state
- run_state
- task_state
- battery_level
- current_location

docs 與 reference 內的實測文件也支持以下判斷原則：

- 判斷是否移動以 move_state 為主
- 判斷充電以 is_charging 為主
- task_state 適合看任務語意，不宜當作最即時移動訊號

## 6. 測試與版本對齊狀態

### 6.1 測試檔更新已完成

tests/test_api_integration.py 已完成對齊：

- 改為使用 POST /api/packages/{package_id}/assign
- 移除舊 Door.task_id 假設，改驗證 RobotState.current_task_id
- 對齊現行 control_doors 參數格式與背景執行緒 stub

tests/load_test.py 已升級為 0.4.0 壓測情境：

- quantity > 1
- concurrent assign
- return-timeout
- recall

### 6.2 自動化測試結果

- pytest: 4 passed, 1 skipped

### 6.3 版本號一致性

- README、REPORT、pyproject.toml、src/aurobox/__init__.py 已統一為 0.4.0

## 7. 本次文件更新內容

本次已完成：

- 以多包裹流程重寫 README
- 以全目錄盤點結果重寫 REPORT
- API 清單、狀態模型、退件與召回流程全部對齊現行程式
- 測試章節更新為最新實作狀態，移除已過時風險敘述

## 8. 建議下一步

1. 擴充整合測試到 return-open、dashboard/status、recharge。
2. 建立 migration（例如 Alembic）避免欄位演進造成環境不一致。
3. 在 CI 中加入 pytest 與基本壓測 smoke 檢查。

