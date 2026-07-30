"""API routes for Robot Hardware & Door management."""

import time
import threading
from flask import Blueprint, request, jsonify, current_app
from . import db
from .models import Door, DoorStatus, RobotState

from .utils import build_custom_call_payload
from .services import update_robot_state, check_and_return_home_if_empty
from .tasks import _return_for_assign, _poll_notify_display_qr, _queue_door_action

api_bp = Blueprint('api', __name__)

def _get_active_doors(app):
    mode = app.config.get('DOOR_MODE', '4_DOORS')
    if mode == '3_DOORS':
        return ("H_01", "H_02", "H_03")
    return ("H_01", "H_02", "H_03", "H_04")

# ==========================================================
# 0. 讓機器人返回充電站 (Recharge)
# ==========================================================
@api_bp.route('/robot/recharge', methods=['POST'])
def robot_recharge():
    controller = current_app.pudu_controller
    sn = current_app.config.get('ROBOT_SN')
    charge_point = current_app.config.get('CHARGE_POINT_NAME')

    if not charge_point:
        print("[系統] 警告：未設定 CHARGE_POINT_NAME，機器人可能無法正確記錄充電站位置", flush=True)
        # 即使沒設定，還是可以嘗試呼叫 recharge，但建議要有
        charge_point = "閃閃充電" # 給個預設值防呆

    try:
        # 1. 檢查艙門是否全空 (加入防呆機制)
        non_empty_doors = Door.query.filter(
            Door.sn == sn, 
            Door.status != DoorStatus.EMPTY.value
        ).all()

        if non_empty_doors:
            # 抓出哪些艙門還有東西，方便回報給前端排查
            busy_door_numbers = [door.door_number for door in non_empty_doors]
            error_msg = f"Cannot recharge. Doors {busy_door_numbers} are not empty."
            print(f"[系統] 拒絕充電請求：{error_msg}", flush=True)
            
            # 回傳 409 Conflict 代表機器人當前狀態與請求發生衝突
            return jsonify({
                'status': 'error',
                'error': error_msg,
                'busy_doors': busy_door_numbers
            }), 409 

        # 2. 確定艙門全空，呼叫機器人回充電站的 API
        print(f"[系統] 艙門檢查完畢 (皆為空)。準備呼叫機器人 {sn} 返回充電站...", flush=True)
        payload = build_custom_call_payload(sn=sn, point=charge_point)
        response = controller.custom_call2(payload=payload)
        
        # 3. 將機器人的最後點位更新為充電站，task_id為移動去充電站的任務ID (若有)
        task_id = response.get('data', {}).get('task_id') if response and response.get('message') == 'SUCCESS' else None
        update_robot_state(sn, point=charge_point, task_id=task_id)
        
        print(f"[系統] 返回充電站指令發送成功，回應: {response}", flush=True)
        
        return jsonify({
            'status': 'success',
            'message': f'Robot is returning to charge station ({charge_point}).',
            'response': response
        })
        
    except Exception as e:
        import traceback
        current_app.logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ==========================================================
# 1. 分派空艙門並為管理員開門 (Assign & Open) - 支援指定門與自動分配雙模式
# ==========================================================
@api_bp.route('/door-tasks/<door_task_id>/assign', methods=['POST'])
def assign_door_for_package(door_task_id):
    data = request.get_json(silent=True) or {}
    print(data, flush=True)

    package_id = data.get('package_id')
    requested_doors = data.get('door_id')
    door_count = int(data.get('quantity', 1))
    task_type = data.get('task_type', 'delivery')
    if isinstance(requested_doors, str):
        requested_doors = [requested_doors]

    controller = current_app.pudu_controller
    home_point = current_app.home_point
    sn = current_app.config.get('ROBOT_SN')
    active_doors = _get_active_doors(current_app)

    # === 防呆：檢查機器人是否已經在外執行任務 ===
    robot_state = RobotState.query.filter_by(sn=sn).first()
    if robot_state and robot_state.last_point and robot_state.last_point != home_point:
        return jsonify({
            'error': 'Robot is currently out for tasks. Please wait until it returns.', 
            'status': 'conflict'
        }), 409
    
    # 檢查該包裹是否已經有被指派的艙門
    existing_doors = Door.query.filter_by(sn=sn, door_task_id=door_task_id).order_by(Door.door_number).all()
    existing_numbers = [d.door_number for d in existing_doors]

    doors_to_assign = []

    # ================= A: 外部明確指定艙門 =================
    if requested_doors is not None:
        if not isinstance(requested_doors, list) or not requested_doors:
            return jsonify({'error': 'door_id must be a non-empty list.', 'status': 'bad_request'}), 400
            
        invalid_doors = [d for d in requested_doors if d not in active_doors]
        if invalid_doors:
            return jsonify({'error': f'Invalid door numbers for current mode: {invalid_doors}'}), 400

        # 鎖定指定的艙門並檢查狀態
        target_doors = Door.query.filter(
            Door.sn == sn, 
            Door.door_number.in_(requested_doors)
        ).with_for_update().all()

        if len(target_doors) != len(requested_doors):
            return jsonify({'error': 'Some requested doors were not found.'}), 400
            
        doors_to_assign = target_doors
    # ================= B: 系統自動尋找空門 =================
    else:
        # 如果已經指派的艙門數量夠了，直接回傳
        if len(existing_doors) >= door_count:
            return jsonify({'status': 'success', 'door_numbers': existing_numbers}), 200
        
        needed_count = door_count - len(existing_doors)
        
        empty_doors = Door.query.filter(
            Door.sn == sn, 
            Door.status == DoorStatus.EMPTY.value,
            Door.door_number.in_(active_doors)
        ).order_by(Door.door_number).with_for_update(skip_locked=True).limit(needed_count).all()
        
        if len(empty_doors) < needed_count:
            return jsonify({'error': f'Not enough empty doors. Requested: {needed_count}, Available: {len(empty_doors)}'}), 400
            
        doors_to_assign = empty_doors
    # ================= 共用邏輯: 呼叫機器人回管理室並更新資料庫 =================
    try:
        already_assigning = Door.query.filter(
            Door.sn == sn,
            Door.status.in_([DoorStatus.ASSIGNED.value, DoorStatus.LOADING.value])
        ).first()
        
        if already_assigning:
            print(f"[系統] 機器人正在裝載中，強制省略導航指令", flush=True)
        else:
            payload = build_custom_call_payload(sn=sn, point=home_point)
            result = controller.custom_call2(payload=payload)
            
            task_id = result.get('data', {}).get('task_id') if result and result.get('message') == 'SUCCESS' else None
            update_robot_state(sn, point=home_point, task_id=task_id)

        assigned_door_numbers = []
        for door in doors_to_assign:
            door.door_task_id = door_task_id
            door.status = DoorStatus.ASSIGNED.value
            assigned_door_numbers.append(door.door_number)
        
        db.session.commit()

        if task_type == 'delivery' or task_type == 'deliver':
            app = current_app._get_current_object()
            for door_num in assigned_door_numbers:
                threading.Thread(target=_queue_door_action, args=(app, controller, sn, door_num, "OPEN"), daemon=True).start()
        else:
            print(f"[系統] 退件任務 {door_task_id} 分配成功，艙門保持關閉，等待 dispatch 前往住戶家", flush=True)
            
        return jsonify({
            'status': 'success', 
            'message': f'Assigned doors {assigned_door_numbers} for {door_task_id}',
            'door_numbers': existing_numbers + assigned_door_numbers
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

# ==========================================================
# 1.5 管理員裝載逾時 (Assign Timeout)
# ==========================================================
@api_bp.route('/door-tasks/<door_task_id>/assign-timeout', methods=['POST'])
def package_assign_timeout(door_task_id):
    controller = current_app.pudu_controller
    sn = current_app.config.get('ROBOT_SN')

    doors = Door.query.filter(
        Door.door_task_id == door_task_id,
        Door.sn == sn,
        Door.status.in_([DoorStatus.ASSIGNED.value, DoorStatus.LOADING.value])
    ).with_for_update().all()
    if not doors: return jsonify({'status': 'success', 'message': 'No ASSIGNED doors found.'}), 200

    try:
        control_states = [{"operation": False, "door_number": d.door_number} for d in doors]
        controller.control_doors(sn=sn, control_states=control_states)
        door_numbers = []
        for d in doors:
            d.status = DoorStatus.EMPTY.value
            d.door_task_id = None
            door_numbers.append(d.door_number)
        db.session.commit()

        app = current_app._get_current_object()
        from .tasks import _queue_door_action
        for d_num in door_numbers:
            threading.Thread(target=_queue_door_action, args=(app, controller, sn, d_num, "CLOSE"), daemon=True).start()

        return jsonify({'status': 'success', 'message': f'Assign timeout handled. Doors {door_numbers} closed.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'error': str(e)}), 500

# ==========================================================
# 2. 管理員將包裹放入艙門並確認 (Load)
# ==========================================================
@api_bp.route('/doors/load', methods=['POST'])
def load_package_to_door():
    controller = current_app.pudu_controller
    sn = current_app.config.get('ROBOT_SN')
    
    # 只找「剛才被打開 (LOADING)」的門，完全略過 ASSIGNED (退貨) 的門
    opened_doors = Door.query.filter_by(sn=sn, status=DoorStatus.LOADING.value).order_by(Door.door_number).with_for_update().all()

    if not opened_doors:
        return jsonify({'status': 'success', 'message': 'No open doors found to load.', 'loaded_doors': []}), 200
    
    loaded_doors_info, control_states = [], []

    try:
        for door in opened_doors:
            control_states.append({"operation": False, "door_number": door.door_number})
            door.status = DoorStatus.FULL.value
            loaded_doors_info.append({'door_number': door.door_number, 'door_task_id': door.door_task_id})

        db.session.commit()

        if control_states:
            controller.control_doors(sn=sn, control_states=control_states)
        
        return jsonify({'status': 'success', 'loaded_doors': loaded_doors_info})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
# ==========================================================
# 3. 指揮機器人移動 (Dispatch)  -- 多包裹維持單一包裹配送
# ==========================================================
@api_bp.route('/robot/dispatch', methods=['POST'])
def robot_dispatch():
    data = request.get_json()
    print(data, flush=True)
    door_task_id = data.get('door_task_id')
    target_point = data.get('unit') or data.get('point')

    if not target_point: return jsonify({'error': 'point is required'}), 400
    controller = current_app.pudu_controller
    sn = current_app.config.get('ROBOT_SN')

    if door_task_id:
        robot_state = RobotState.query.filter_by(sn=sn).first()
        if robot_state and robot_state.current_task_id and robot_state.last_point == target_point:
            app = current_app._get_current_object()
            threading.Thread(target=_poll_notify_display_qr, args=(app, controller, sn, door_task_id, robot_state.current_task_id), daemon=True).start()

            return jsonify({
                'status': 'success',
                'message': f'Robot is already moving to {target_point}',
                'task_id': robot_state.current_task_id,
                'polling': True,
            }), 200
        
    try:
        payload = build_custom_call_payload(sn=sn, point=target_point, call_mode='QR_CODE')
        dispatch_res = controller.custom_call2(payload=payload)
        task_id = None
        if dispatch_res and dispatch_res.get('message') == 'SUCCESS':
            task_id = dispatch_res.get('data', {}).get('task_id')
            update_robot_state(sn, point=target_point, task_id=task_id)
        
        if door_task_id:
            app = current_app._get_current_object()
            threading.Thread(target=_poll_notify_display_qr, args=(app, controller, sn, door_task_id, task_id), daemon=True).start()

        return jsonify({'status': 'success', 'message': f'Robot is moving to {target_point}', 'task_id': task_id, 'polling': door_task_id is not None})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==========================================================
# 4. 掃描 QR 碼完成 (Pickup Complete)
# ==========================================================
@api_bp.route('/door-tasks/<door_task_id>/pickup-complete', methods=['POST'])
def package_pickup_complete(door_task_id):
    controller = current_app.pudu_controller
    sn = current_app.config.get('ROBOT_SN')
    doors = Door.query.filter_by(door_task_id=door_task_id, sn=sn).with_for_update().all()
    
    if not doors: return jsonify({'status': 'success', 'message': 'Door task not found.'}), 200
    if any(d.status in [DoorStatus.PICKING.value, DoorStatus.PUTTING.value] for d in doors): 
        return jsonify({'status': 'success', 'message': 'Operation in progress.'}), 200

    control_states, door_numbers = [], []
    for d in doors:
        if d.status == DoorStatus.FULL.value:
            d.status = DoorStatus.PICKING.value  # 送貨：開始取件
        elif d.status == DoorStatus.ASSIGNED.value:
            d.status = DoorStatus.PUTTING.value  # 退貨：開始置件
        control_states.append({"operation": True, "door_number": d.door_number})
        door_numbers.append(d.door_number)
    
    db.session.commit()

    robot_state = RobotState.query.filter_by(sn=sn).first()
    task_id_to_clear = robot_state.current_task_id if robot_state else None

    if task_id_to_clear:
        try:
            controller.custom_complete({"task_id": task_id_to_clear})
            update_robot_state(sn, clear_task=True)
        except Exception as e:
            print(f"[系統] 消除任務畫面失敗: {e}", flush=True)
    
    try:
        controller.control_doors(sn=sn, control_states=control_states)
        time.sleep(6)
        return jsonify({'status': 'success', 'message': f'Doors {door_numbers} opened successfully.'})
    except Exception as e:
        for d in doors:
            if d.status == DoorStatus.PICKING.value:
                d.status = DoorStatus.FULL.value  # 送貨：回到送件狀態
            elif d.status == DoorStatus.PUTTING.value:
                d.status = DoorStatus.ASSIGNED.value  # 退貨：回到指定狀態

        db.session.commit()

        return jsonify({'error': str(e)}), 500

# ==========================================================
# 5. 住戶取貨完成 (Complete)
# ==========================================================
@api_bp.route('/door-tasks/<door_task_id>/complete', methods=['POST'])
def package_complete(door_task_id):
    controller = current_app.pudu_controller
    sn = current_app.config.get('ROBOT_SN')
    
    doors = Door.query.filter_by(door_task_id=door_task_id, sn=sn).with_for_update().all()
    if not doors: return jsonify({'status': 'success', 'message': 'Door task not found.', 'returning_home': False}), 200

    try:
        robot_state = RobotState.query.filter_by(sn=sn).first()
        task_id_to_clear = robot_state.current_task_id if robot_state else None
        if task_id_to_clear:
            try:
                controller.custom_complete({"task_id": task_id_to_clear})
                update_robot_state(sn, clear_task=True)
                time.sleep(6)
            except Exception: pass

        control_states = []
        for d in doors:
            if d.status == DoorStatus.PICKING.value:
                d.status = DoorStatus.EMPTY.value  # 取件完畢：變回空門
                d.door_task_id = None              # 送貨任務圓滿結束，清空 ID
            elif d.status == DoorStatus.PUTTING.value:
                d.status = DoorStatus.FULL.value   # 置件完畢：門內有退件包裹
            control_states.append({"operation": False, "door_number": d.door_number})

        db.session.commit()

        controller.control_doors(sn=sn, control_states=control_states)
        
        is_returning = check_and_return_home_if_empty()
        return jsonify({'status': 'success', 'message': 'Doors closed and freed.', 'returning_home': is_returning})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==========================================================
# 6. 住戶拒收 / 取消或逾時 (Cancel / Reject)
# ==========================================================
@api_bp.route('/door-tasks/<door_task_id>/cancel', methods=['POST'])
def package_cancel(door_task_id):
    controller = current_app.pudu_controller
    sn = current_app.config.get('ROBOT_SN')

    doors = Door.query.filter_by(door_task_id=door_task_id, sn=sn).with_for_update().all()
    if not doors: 
        time.sleep(6)
        return jsonify({'status': 'success', 'message': 'Ignored.'}), 200
    if any(d.status == DoorStatus.PICKING.value or d.status == DoorStatus.PUTTING.value for d in doors): 
        time.sleep(6)
        return jsonify({'status': 'success', 'message': 'Ignored.'}), 200
    
    try:
        control_states = [{"operation": False, "door_number": d.door_number} for d in doors]
        controller.control_doors(sn=sn, control_states=control_states)
        
        robot_state = RobotState.query.filter_by(sn=sn).first()
        task_id_to_clear = robot_state.current_task_id if robot_state else None
        if task_id_to_clear:
            try:
                controller.custom_complete({"task_id": task_id_to_clear})
                update_robot_state(sn, clear_task=True)
            except Exception: pass
        
        for d in doors:
            if d.status == DoorStatus.ASSIGNED.value:
                # 退貨取消：裡面沒東西，歸還為空門
                d.status = DoorStatus.EMPTY.value
                d.door_task_id = None
            else:
                # 送貨拒收：裡面有原本的包裹，鎖定為 FULL 帶回
                d.status = DoorStatus.FULL.value

        db.session.commit()

        time.sleep(6)
        
        return jsonify({'status': 'success', 'message': f'Package {door_task_id} rejected. Reset to FULL.'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==========================================================
# 7. 管理室：退件返航 (Return Home)
# ========================================================== 
@api_bp.route('/door-tasks/return', methods=['POST'])
def return_packages_to_home():
    controller = current_app.pudu_controller
    home_point = current_app.home_point
    sn = current_app.config.get('ROBOT_SN')

    #full_doors = Door.query.filter_by(sn=sn, status=DoorStatus.FULL.value).all()
    #if not full_doors: return jsonify({'status': 'success', 'message': 'No returned packages.'})
    
    robot_state = RobotState.query.filter_by(sn=sn).first()
    if robot_state and robot_state.last_point == home_point:
        return jsonify({'status': 'success', 'message': f'Robot returning to {home_point}.', 'returning_home': True}), 200
    
    try:
        payload = build_custom_call_payload(sn=sn, point=home_point)
        result = controller.custom_call2(payload=payload)
        
        task_id = result.get('data', {}).get('task_id') if result and result.get('message') == 'SUCCESS' else None
        update_robot_state(sn, point=home_point, task_id=task_id)

        return jsonify({'status': 'success', 'message': f'Robot returning to {home_point}.', 'returning_home': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==========================================================
# 8. 管理室：退件返航後開門 (Open Returned Doors)
# ========================================================== 
@api_bp.route('/doors/return-open', methods=['POST'])
def open_returned_doors():
    controller = current_app.pudu_controller
    sn = current_app.config.get('ROBOT_SN')

    active_doors = _get_active_doors(current_app)
    all_doors = Door.query.filter(Door.sn == sn, Door.door_number.in_(active_doors)).order_by(Door.door_number).all()
    if not all_doors: return jsonify({'status': 'success', 'message': 'No active doors to open.', 'opened_doors': []})
    
    control_states, opened_doors = [], []
    for door in all_doors:
            control_states.append({"operation": True, "door_number": door.door_number})
            opened_doors.append(door.door_number)

    try:
        if control_states:
            controller.control_doors(sn=sn, control_states=control_states)
            print(f"[系統] 正在手動批次開啟所有啟用艙門供檢查: {opened_doors}...", flush=True)
        
        return jsonify({'status': 'success', 'message': f'All active doors: {opened_doors} opened successfully for inspection.', 'returning_home': True})
    except Exception as e:
        import traceback
        current_app.logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ==========================================================
# 9. 管理室：確認取出並關閉艙門 (Return Complete)
# ==========================================================
@api_bp.route('/doors/return-complete', methods=['POST'])
def complete_returned_doors():
    controller = current_app.pudu_controller
    sn = current_app.config.get('ROBOT_SN')
    active_doors = _get_active_doors(current_app)
    all_doors = Door.query.filter(Door.sn == sn, Door.door_number.in_(active_doors)).order_by(Door.door_number).with_for_update().all()
    if not all_doors: return jsonify({'status': 'success', 'message': 'No doors to close.', 'closed_doors': []})

    closed_doors, control_states = [], []
    for door in all_doors:
        control_states.append({"operation": False, "door_number": door.door_number})
        closed_doors.append(door.door_number)
        door.status = DoorStatus.EMPTY.value
        door.door_task_id = None

    try:
        if control_states:
            controller.control_doors(sn=sn, control_states=control_states)
            print(f"[系統] 檢查完畢，正在批次關閉所有艙門: {closed_doors}...", flush=True)
            
        db.session.commit()

        print(f"[系統] 所有艙門已清空並關閉，硬體資源完全釋放", flush=True)

        return jsonify({'status': 'success', 'message': f'All doors: {closed_doors} inspected, closed and freed in a single batch.'})
    except Exception as e:
        import traceback
        current_app.logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ==========================================================
# 9.5 管理室：退件開門逾時強制關門 (Return Open Timeout)
# ==========================================================
@api_bp.route('/doors/return-timeout', methods=['POST'])
def return_doors_timeout():
    controller = current_app.pudu_controller
    sn = current_app.config.get('ROBOT_SN')
    active_doors = _get_active_doors(current_app)
    all_doors = Door.query.filter(Door.sn == sn, Door.door_number.in_(active_doors)).order_by(Door.door_number).with_for_update().all()

    if not all_doors: return jsonify({'status': 'success', 'message': 'No active doors found. Return process might be completed already.', 'closed_doors': []}), 200

    closed_doors, control_states = [], []
    for door in all_doors:
        control_states.append({"operation": False, "door_number": door.door_number})
        closed_doors.append(door.door_number)
        if door.status != DoorStatus.EMPTY.value:
            door.status = DoorStatus.FULL.value

    try:
        if control_states:
            controller.control_doors(sn=sn, control_states=control_states)
            print(f"[系統] 觸發退件檢查逾時，強制批次關閉所有艙門: {closed_doors}...", flush=True)
            
        db.session.commit()

        print(f"[系統] 逾時艙門已強制關閉，硬體資源全面釋放", flush=True)

        return jsonify({'status': 'success', 'message': f'Return timeout handled. All doors {closed_doors} force-closed and reset.', 'closed_doors': closed_doors})
    except Exception as e:
        import traceback
        current_app.logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500

# ==========================================================
# 10. Dashboard 監控 API (直接查實體機器人與艙門資料庫)
# ==========================================================
@api_bp.route('/dashboard/status', methods=['GET'])
def get_dashboard_status():
    controller = current_app.pudu_controller
    sn = current_app.config.get('ROBOT_SN')
    
    try:
        live_status = controller.get_status_summary(sn)
        move_state = live_status.get('move_state') # 會是 MOVING, ARRIVE, 或空值
        
        robot_state = RobotState.query.filter_by(sn=sn).first()
        last_point = robot_state.last_point if robot_state else current_app.config.get('HOME_POINT_NAME')
        
        if move_state == "MOVING" or move_state == "APPROACHING":
            live_status['current_location'] = "MOVING"
        else:
            live_status['current_location'] = last_point
        
        active_doors = _get_active_doors(current_app)
        doors = Door.query.filter(Door.sn == sn, Door.door_number.in_(active_doors)).order_by(Door.door_number).all()
        door_states = [{'door_number': door.door_number, 'status': door.status, 'door_task_id': door.door_task_id} for door in doors]
        
        return jsonify({
            'status': 'success',
            'data': {
                'robot_status': live_status, 
                'door_states': door_states
            }
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==========================================================
# 11. 緊急召回 (Recall)
# ==========================================================
@api_bp.route('/robot/recall', methods=['POST'])
def robot_recall():
    """
    前端按鈕觸發：緊急中斷機器人目前任務，並強制返回管理室。
    【本機動作】：
      1. 找尋 DB 中的 task_id，若無則報錯 (因無法覆寫硬體)
      2. 發送 Cancel 註銷當前任務
      3. 暫停 2 秒讓硬體狀態機重置
      4. 發送新導航指令回管理室
      5. 全面保護艙門狀態為 FULL
    """
    controller = current_app.pudu_controller
    sn = current_app.config.get('ROBOT_SN')
    home_point = current_app.home_point

    # 鎖定所有艙門，準備資料庫保護
    active_doors = Door.query.filter(Door.sn == sn).with_for_update().all()

    # === 狀態防護牆 (保護最後一哩路的送貨流程) ===
    live_status = controller.get_status_summary(sn)
    move_state = live_status.get('move_state')
    
    # 檢查是否有門正在 PICKING (已掃碼，門已開，等待 Complete 關門)
    is_picking = any(
        door.status in [DoorStatus.PICKING.value, DoorStatus.PUTTING.value] 
        for door in active_doors
    )
    
    # 取得唯一任務 ID 與當前點位
    robot_state = RobotState.query.filter_by(sn=sn).first()
    active_task_id = robot_state.current_task_id if robot_state else None
    is_at_door = (robot_state and robot_state.last_point != home_point)

    is_protected = is_picking or (is_at_door and move_state in ['APPROACHING', 'ARRIVE']) or (is_at_door and move_state == 'IDLE' and active_task_id)
    # 拒絕召回的條件：
    # 1. 正在接近或剛抵達 (APPROACHING / ARRIVE)
    # 2. 門已經被打開，住戶正在取件 (PICKING 狀態)
    # 3. 已抵達但任務未結束 (IDLE + 有 task_id，代表還在螢幕顯示 QR Code 等待掃描)
    if is_protected:
        print(f"[系統] 召回進入排隊：機器人正在服務住戶中，等待流程結束...", flush=True)
        
        # 匯入並啟動背景排隊執行緒
        from .tasks import _wait_and_execute_recall
        app = current_app._get_current_object()
        threading.Thread(
            target=_wait_and_execute_recall,
            args=(app, controller, sn, home_point),
            daemon=True
        ).start()

        return jsonify({
            'status': 'success',
            'message': 'Robot is serving a resident. Recall is queued and will execute automatically after delivery completes.',
            'queued': True,
            'returning_home': False
        }), 200

    try:
        # 發送 Cancel 取消任務
        if active_task_id:
            try:
                controller.custom_call_cancel({"task_id": active_task_id})
                print(f"[系統] 成功發送 Cancel 註銷任務 {active_task_id}，等待硬體重置...", flush=True)
            except Exception as e:
                print(f"[系統] 註銷任務 {active_task_id} 發生異常: {e}", flush=True)
            
        update_robot_state(sn, clear_task=True)
        # 物理緩衝：等待硬體完全註銷舊路線，回到 IDLE
        time.sleep(6)

        # 發送常規導航指令回管理室
        print(f"[系統] 硬體重置完畢，發送新導航指令回管理室 {home_point}...", flush=True)
        payload = build_custom_call_payload(sn=sn, point=home_point)
        res = controller.custom_call2(payload=payload)
        
        # 紀錄新的返航 task_id，並更新點位
        new_task = res.get('data', {}).get('task_id') if res and res.get('message') == 'SUCCESS' else None
        update_robot_state(sn, point=home_point, task_id=new_task)

        # 處理本機資料庫的艙門保護
        recalled_doors = []
        for door in active_doors:
            # 遭遇 Recall，只要是處理到一半的狀態，通通強制轉為 FULL 保護起來
            if door.status in [DoorStatus.PICKING.value, DoorStatus.ASSIGNED.value, DoorStatus.PUTTING.value, DoorStatus.LOADING.value]:
                door.status = DoorStatus.FULL.value
                recalled_doors.append(door.door_number)
            elif door.status == DoorStatus.FULL.value:
                recalled_doors.append(door.door_number)

        db.session.commit()

        return jsonify({
            'status': 'success',
            'message': 'Robot recall triggered successfully. Navigating home.',
            'cancelled_task': active_task_id,
            'protected_doors': recalled_doors,
            'returning_home': True
        })

    except Exception as e:
        import traceback
        current_app.logger.error(traceback.format_exc())
        return jsonify({'error': str(e)}), 500
