"""Business logic and database services."""
from flask import current_app
from . import db
from .models import Door, DoorStatus, RobotState
from .robot import FlashbotController
from .config import load_config
from .utils import build_custom_call_payload

def get_controller():
    """Get FlashbotController instance."""
    return FlashbotController(load_config())

def update_robot_state(sn: str, point: str = None, task_id: str = None, clear_task: bool = False):
    """輔助函式：將機器人最新點位與任務 ID 寫入資料庫"""
    state = RobotState.query.filter_by(sn=sn).first()
    if not state:
        state = RobotState(sn=sn)
        db.session.add(state)
        
    if point is not None:
        state.last_point = point
        
    if clear_task:
        state.current_task_id = None
    elif task_id is not None:
        state.current_task_id = task_id
        
    db.session.commit()

def check_and_return_home_if_empty():
    """檢查是否所有艙門都為 EMPTY，若是則命令機器人返回管理室"""
    controller = current_app.pudu_controller
    home_point = current_app.home_point
    sn = current_app.config.get('ROBOT_SN')
    
    # 尋找還有貨(不等於 EMPTY)的門
    non_empty_doors = Door.query.filter(
        Door.sn == sn, 
        Door.status != DoorStatus.EMPTY.value
    ).count()
    
    if non_empty_doors == 0:

        # 加上狀態檢查，避免原地轉圈
        live_status = controller.get_status_summary(sn)
        is_already_home = (
            live_status.get('current_location') == home_point and 
            live_status.get('move_state') in ['IDLE', 'ARRIVE']
        )

        task = None
        
        if not is_already_home:
            payload = build_custom_call_payload(sn=sn, point=home_point)
            res = controller.custom_call2(payload=payload)
            if res and res.get('message') == 'SUCCESS':
                task = res.get('data', {}).get('task_id')
            
        update_robot_state(sn, point=home_point, task_id=task)
        return True
    return False