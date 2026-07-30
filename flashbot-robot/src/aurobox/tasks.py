"""Background threading tasks."""
import time
from flask import current_app
from . import db
import requests as http_requests
import threading
import queue
from .models import Door, DoorStatus, RobotState
from .services import update_robot_state
from .utils import build_custom_call_payload

timeout_seconds = 300
# 宣告全域 Lock，防止多個分派任務同時執行開門動作
_assign_lock = threading.Lock()
_assign_queue = queue.Queue()
_recall_lock = threading.Lock()

def _queue_door_action(
    app,
    controller,
    sn: str,
    door_number: str,
    action: str,  # 新增參數： 'OPEN' 或 'CLOSE'
    timeout_seconds: int = 300,
    poll_interval: int = 5,
):
    """
    背景執行緒：統籌處理艙門動作請求 (開或關)，確保依序執行。
    """
    # 1. 把動作與門號一起塞進佇列，例如 ("OPEN", "H_01")
    _assign_queue.put((action, door_number))
    
    # 2. 確保同一時間只有一個執行緒在處理佇列
    if not _assign_lock.acquire(blocking=False):
        print(f"[系統] 已有統籌執行緒在運作，新任務 ({action} 艙門 {door_number}) 已加入佇列等待。", flush=True)
        return

    try:
        with app.app_context():
            # 只有當我們需要「開門(OPEN)」時，才需要等機器人回管理室
            # 如果是單純排隊要「關門(CLOSE)」，就不強制等，直接執行
            needs_to_wait = any(item[0] == "OPEN" for item in list(_assign_queue.queue))
            
            if needs_to_wait:
                time.sleep(10)
                print(f"[系統] 開始輪詢機器人是否抵達管理室", flush=True)
                arrived = controller.wait_until_arrived(
                    sn=sn,
                    timeout_seconds=timeout_seconds,
                    poll_interval=poll_interval,
                )
                if not arrived:
                    print(f"[系統] 輪詢超時，機器人未能在預期時間內抵達", flush=True)
                    while not _assign_queue.empty():
                        _assign_queue.get()
                    return
                print(f"[系統] 機器人已抵達，準備依序執行佇列中的艙門動作...", flush=True)

            # 3. 依序執行動作
            while True:
                if _assign_queue.empty():
                    break
                    
                current_action, current_door = _assign_queue.get()
                
                try:
                    if current_action == "OPEN":
                        controller.control_doors(sn=sn, control_states=[{"operation": True, "door_number": current_door}])
                        door_record = Door.query.filter_by(sn=sn, door_number=current_door).first()
                        if door_record and door_record.status == DoorStatus.ASSIGNED.value:
                            door_record.status = DoorStatus.LOADING.value
                            db.session.commit()
                        print(f"[系統] 成功開啟艙門 {current_door}", flush=True)
                        
                    elif current_action == "CLOSE":
                        controller.control_doors(sn=sn, control_states=[{"operation": False, "door_number": current_door}])
                        # 關門的 DB 狀態更新會交由呼叫這支 API 的主程式 (assign-timeout) 處理，
                        # 這裡只負責卡住硬體的排隊順序
                        print(f"[系統] 成功關閉艙門 {current_door}", flush=True)
                        
                    time.sleep(1.5)  # 每個動作之間保留 1.5 秒的硬體緩衝時間
                    
                except Exception as e:
                    print(f"[系統] 艙門動作失敗 ({current_action} 艙門 {current_door}): {e}", flush=True)
                    
    finally:
        _assign_lock.release()

def _return_for_assign(
    app,
    controller,
    sn: str,
    door_number: str,
    timeout_seconds: int = timeout_seconds,
    poll_interval: int = 5,
):
    """背景執行緒：統籌處理開門請求，確保每個門只被呼叫一次開門。"""
    
    # 1. 第一時間把這次要開的門塞進全域佇列
    _assign_queue.put(door_number)
    
    # 2. 確保同一時間只有一個執行緒在等待並消耗佇列
    if not _assign_lock.acquire(blocking=False):
        print(f"[系統] 已有統籌執行緒在運作，新任務艙門 {door_number} 已加入佇列等待。", flush=True)
        return

    try:
        with app.app_context():
            time.sleep(10)
            print(f"[系統] 開始輪詢機器人是否抵達管理室", flush=True)
            
            arrived = controller.wait_until_arrived(
                sn=sn,
                timeout_seconds=timeout_seconds,
                poll_interval=poll_interval,
            )
            
            if not arrived:
                print(f"[系統] 輪詢超時，機器人未能在預期時間內抵達", flush=True)
                # 超時的話，清空沒處理完的佇列避免後續發生異常開門
                while not _assign_queue.empty():
                    _assign_queue.get()
                return

            print(f"[系統] 機器人已抵達，準備依序開啟佇列中的艙門...", flush=True)
            
            # 3. 只要佇列裡面還有任務，就拿出來開門 (開完就丟掉，不會重複)
            while True:
                if _assign_queue.empty():
                    break  # 確定全空才準備跳出
                    
                door_to_open = _assign_queue.get()
                try:
                    controller.control_doors(sn=sn, control_states=[{"operation": True, "door_number": door_to_open}])
                    door_record = Door.query.filter_by(sn=sn, door_number=door_to_open).first()
                    if door_record and door_record.status == DoorStatus.ASSIGNED.value:
                        door_record.status = DoorStatus.LOADING.value
                        db.session.commit()

                    print(f"[系統] 成功開啟艙門 {door_to_open}", flush=True)
                    time.sleep(1) 
                except Exception as e:
                    print(f"[系統] 開門失敗 (艙門 {door_to_open}): {e}", flush=True)
                    
    finally:
        # 確保即使發生異常，也一定會釋放 Lock
        _assign_lock.release()

def _poll_notify_display_qr(
    app,
    controller,
    sn: str,
    door_task_id: str,
    task_id: str = None,
    timeout_seconds: int = 300, # 建議給個預設值，避免全域變數讀不到
    poll_interval: int = 5,
) -> None:
    """背景執行緒：輪詢機器人狀態直到抵達，再通知中央大腦。"""
    
    # 1. 第一層：先進入 Flask 應用程式的 Context
    with app.app_context():
        # 2. 第二層：把所有邏輯用 try 包起來，確保最後一定會走到 finally
        try:
            time.sleep(10)
            print("start polling", flush=True)
            
            arrived = controller.wait_until_arrived(
                sn=sn,
                timeout_seconds=timeout_seconds,
                poll_interval=poll_interval,
            )
            
            if not arrived:
                print(f"[系統] 任務 {door_task_id} 輪詢超時，未收到抵達確認", flush=True)
                return # 這裡 return 後，會自動跳去執行 finally，安全釋放連線！
            
            # === 關鍵修復：任務一致性檢查 (防禦 Recall / 二次 Dispatch) ===
            robot_state = RobotState.query.filter_by(sn=sn).first()
            if robot_state:
                current_db_task_id = robot_state.current_task_id
                
                # 資料庫裡面的任務 ID 已經跟當初傳進來的不一樣
                if task_id and current_db_task_id != task_id:
                    print(f"[系統] 偵測到任務已變更 (可能遭遇 Recall)。任務 {door_task_id} 原任務 {task_id} 已失效，取消抵達推播！", flush=True)
                    return
            
            # 安全優化：在背景執行緒中，直接用傳入的 app 讀取 config 最安全
            callback_base_url = app.config.get('CENTRAL_API_BASE_URL', '')
            if not callback_base_url:
                print("[系統] 未設定 CENTRAL_API_BASE_URL，無法啟動主動推播功能", flush=True)
                return
            
            base = callback_base_url.rstrip('/')
            url = f"{base}/door-tasks/{door_task_id}/arrived"
            
            try:
                import requests as http_requests # 確保有引入
                resp = http_requests.post(url, timeout=20)
                if resp.ok:
                    print(f"[系統] 抵達通知成功 ({resp.status_code})  →  {url}", flush=True)
                else:
                    print(f"[系統] 抵達通知回應異常 ({resp.status_code})  →  {url}\n回應內容: {resp.text[:300]}", flush=True)
            except http_requests.exceptions.Timeout:
                print(f"[系統] 抵達通知超時: 中央大腦伺服器無回應  →  {url}", flush=True)
            except Exception as e:
                print(f"[系統] 抵達通知失敗: {e}  →  {url}", flush=True)
            
            if task_id:
                print(f"[系統] 抵達定點，準備顯示 QR Code (Task ID: {task_id})", flush=True)
                payload_qr = {
                    "sn": sn,
                    "payload": {
                        "call_mode": "QR_CODE",
                        "task_id": task_id,
                        "mode_data": {
                            "qrcode": door_task_id,
                            "text": "請掃描 QR Code 取件"
                        }
                    }
                }
                try:
                    res = controller.custom_content(payload=payload_qr)
                    if res and res.get('message') == 'SUCCESS':
                        print("[系統] QR Code 畫面切換成功", flush=True)
                    else:
                        print(f"[系統] QR Code 顯示異常: {res}", flush=True)
                except Exception as e:
                    print(f"[系統] QR Code 顯示失敗: {e}", flush=True)

        # 3. 最關鍵的防護網：在離開 Context 前，歸還資料庫連線
        finally:
            from .models import db # 確保有拿到 db
            db.session.remove()

def _wait_and_execute_recall(app, controller, sn, home_point):
    """背景執行緒：等待防護牆解除後，自動執行召回任務"""
    
    if not _recall_lock.acquire(blocking=False):
        print("[系統] 已經有排隊中的召回任務，略過重複啟動。", flush=True)
        return
    
    with app.app_context():
        try:
            print("[系統] 進入召回排隊等待：等待住戶取件流程結束...", flush=True)

            wait_start = time.time()
            while True:
                if time.time() - wait_start > 480:  # 8 分鐘超時
                    print("[系統] 召回排隊等待超時，強制終止任務", flush=True)
                    break
                # 重新整理 DB Session 確保讀到最新狀態
                db.session.remove()
                
                live_status = controller.get_status_summary(sn)
                move_state = live_status.get('move_state')
                
                active_doors = Door.query.filter_by(sn=sn).all()
                is_picking = any(
                    door.status in [DoorStatus.PICKING.value, DoorStatus.PUTTING.value] 
                    for door in active_doors
                )
                
                robot_state = RobotState.query.filter_by(sn=sn).first()
                active_task_id = robot_state.current_task_id if robot_state else None
                is_at_door = (robot_state and robot_state.last_point != home_point)
                
                # 防護牆條件判定
                is_protected = is_picking or (is_at_door and move_state in ['APPROACHING', 'ARRIVE']) or (is_at_door and move_state == 'IDLE' and active_task_id != None)
                
                if not is_protected:
                    print("[系統] 防護牆已解除！", flush=True)
                    # === 智慧判斷：如果住戶取完件後，系統已經自動判定全空並返航了，就不需要重複召回 ===
                    time.sleep(3)
                    db.session.remove()

                    robot_state = RobotState.query.filter_by(sn=sn).first()
                    if robot_state and robot_state.last_point == home_point:
                        print("[系統] 機器人已經在自動返航的路上，排隊召回任務圓滿結束。", flush=True)
                        return
                    break # 跳出迴圈，準備執行強制召回
                
                # 每 3 秒檢查一次狀態
                time.sleep(3)

            # 防護牆解除，且未自動返航，開始強制召回
            print("[系統] 開始執行排隊中的強制召回任務！", flush=True)
            
            # 此時若還有殘留任務 (例如還有其他包裹沒送)，將其註銷
            if active_task_id:
                try:
                    controller.custom_call_cancel({"task_id": active_task_id})
                except Exception:
                    pass
                update_robot_state(sn, clear_task=True)
                time.sleep(6) # 等待硬體重置

            print(f"[系統] 發送新導航指令回管理室 {home_point}...", flush=True)
            payload = build_custom_call_payload(sn=sn, point=home_point)
            res = controller.custom_call2(payload=payload)
            
            new_task = res.get('data', {}).get('task_id') if res and res.get('message') == 'SUCCESS' else None
            update_robot_state(sn, point=home_point, task_id=new_task)

            # 處理本機資料庫的艙門保護 (將未送達的包裹鎖定為 FULL)
            active_doors = Door.query.filter_by(sn=sn).with_for_update().all()
            for door in active_doors:
                if door.status in [DoorStatus.PICKING.value, DoorStatus.ASSIGNED.value]:
                    door.status = DoorStatus.FULL.value
            db.session.commit()
            print("[系統] 排隊召回任務執行完畢！", flush=True)
            
        finally:
            db.session.remove()
            _recall_lock.release()

'''
def _hardware_watchdog(app, controller, sn):
    """背景執行緒：專職監控硬體異常狀態 (如 STUCK 超時)"""
    try:
        with app.app_context():
            # print("[系統] 硬體異常監控守門犬已啟動...", flush=True)
            stuck_start_time = None
            
            while True:
                # 迴圈內第一件事：清快取連線
                db.session.remove() 
                
                try:
                    live_status = controller.get_status_summary(sn)
                    move_state = live_status.get('move_state')
                    
                    robot_state = RobotState.query.filter_by(sn=sn).first()
                    active_task_id = robot_state.current_task_id if robot_state else None
                    
                    # 只有在「身上有任務」且「發生 STUCK」時才開始計時
                    if active_task_id and move_state == 'STUCK':
                        if stuck_start_time is None:
                            stuck_start_time = time.time()
                            print(f"[系統] 偵測到機器人 STUCK，開始計時...", flush=True)
                            
                        elif time.time() - stuck_start_time > 120:
                            print(f"[系統] 機器人 STUCK 超過 2 分鐘，強制介入中斷任務！", flush=True)
                            
                            # 1. 撤銷硬體任務
                            try:
                                controller.custom_call_cancel({"task_id": active_task_id})
                            except Exception as e:
                                print(f"[系統] 撤銷任務失敗: {e}", flush=True)
                                
                            # 2. 清空資料庫任務狀態，讓機器人回到待機
                            update_robot_state(sn, clear_task=True)
                            
                            # 3. 重置計時器
                            stuck_start_time = None 
                            
                            # TODO: 可以在這裡呼叫緊急 API 通知管理員，或是寫入 Error Log
                            callback_base_url = current_app.config.get('CENTRAL_API_BASE_URL', '')

                            if callback_base_url:
                                base = callback_base_url.rstrip('/')
                                url = f"{base}/"
                                try:
                                    resp = http_requests.post(url, timeout=5)
                                    if resp.ok:
                                        print(f"[系統] 已成功發送 STUCK 警報至中央大腦 ({resp.status_code})", flush=True)
                                    else:
                                        print(f"[系統] 發送警報失敗 ({resp.status_code}): {resp.text}", flush=True)
                                except Exception as e:
                                    print(f"[系統] 無法連線至中央大腦發送警報: {e}", flush=True)
                            else:
                                print("[系統] 未設定 CENTRAL_API_BASE_URL，略過警報推播", flush=True)
                            
                    else:
                        # 只要狀態不是 STUCK (脫困了)，或是身上沒任務，就清除計時器
                        if stuck_start_time is not None:
                            print(f"[系統] 機器人已脫困或任務已結束，STUCK 計時器重置。", flush=True)
                            stuck_start_time = None
                            
                except Exception as e:
                    print(f"[系統] Watchdog 發生異常: {e}", flush=True)
                
                # 讓執行緒睡 5 秒，避免佔用 CPU 資源
                time.sleep(5) 
                
    finally:
        # 迴圈意外結束時的終極防護
        db.session.remove()
'''
        
'''
def _push_dashboard_status_loop(app, poll_interval: int = 3):
    """背景執行緒：輪詢監控目標狀態，當發生變化時立刻推播給中央大腦。"""
    with app.app_context():
        controller = app.pudu_controller
        sn = app.config.get('ROBOT_SN')
        callback_base_url = current_app.config.get('CENTRAL_API_BASE_URL', '')
        if not callback_base_url:
            print("[系統] 未設定 CENTRAL_API_BASE_URL，無法啟動主動推播功能", flush=True)
            return
        base = callback_base_url.rstrip('/')
        
        url = f"{base}/admin/robot-status"
        print(f"[系統] 啟動狀態變更監控執行緒 (間隔 {poll_interval}s)，將推播至 {url}", flush=True)

        # 宣告暫存變數，用來記憶上一次成功推播的狀態
        previous_target_state = None

        while True:
            try:
                # 1. 查詢 Pudu 硬體狀態
                live_status = controller.get_status_summary(sn)
                move_state = live_status.get('move_state')
                
                # 2. 查詢本機資料庫的最後點位
                robot_state = RobotState.query.filter_by(sn=sn).first()
                last_point = robot_state.last_point if robot_state else app.config.get('HOME_POINT_NAME')
                
                # 核心邏輯：組裝位置字串
                if move_state == "MOVING" or move_state == "APPROACHING":
                    live_status['current_location'] = "MOVING"
                else:
                    live_status['current_location'] = last_point

                # 3. 查本機資料庫：目前四個艙門的使用狀況
                doors = Door.query.filter_by(sn=sn).order_by(Door.door_number).all()
                door_states = [{
                    'door_number': door.door_number,
                    'status': door.status,
                    'door_task_id': door.door_task_id
                } for door in doors]
                
                # 4. 提取要監控的「目標資訊」進行比對
                current_target_state = {
                    # 將 4 個門的狀態轉為 Tuple 列表，方便比對
                    "doors": [(d.door_number, d.status) for d in doors], 
                    "location": live_status.get('current_location'),
                    "battery": live_status.get('battery_level'),
                    "move_state": move_state
                }

                # 如果目前的狀態與上一次紀錄的狀態「不同」，才進行推播
                if current_target_state != previous_target_state:
                    
                    # 組裝要推播的完整 Payload
                    payload = {
                        'status': 'success',
                        'data': {
                            'robot_status': live_status,
                            'door_states': door_states
                        }
                    }
                    
                    print(payload, flush=True)
                    # 發送 POST 請求主動推播
                    resp = http_requests.post(url, json=payload, timeout=10)
                    
                    if resp.ok:
                        print(f"[系統] 偵測到目標資訊變更，已即時推播狀態！", flush=True)
                        # 成功推播後，將目前的狀態記憶下來，作為下一次比對的基準
                        previous_target_state = current_target_state
                        poll_interval = 3
                    else:
                        print(f"[系統] Dashboard 推播回應異常 ({resp.status_code})", flush=True)
                        poll_interval = 300
                        # 注意：若推播失敗，不更新 previous_target_state，讓系統下一秒繼續嘗試推播
                        
            except Exception as e:
                print(f"[系統] Dashboard 狀態推播發生異常: {e}", flush=True)
            
            # 暫停 poll_interval 秒後再檢查 (設定為 3 秒以確保即時性)
            time.sleep(poll_interval)
'''