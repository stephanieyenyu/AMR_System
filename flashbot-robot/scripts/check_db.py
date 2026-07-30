#!/usr/bin/env python
import sys
from pathlib import Path
from datetime import timezone

# 將 src 加入系統路徑，這樣 Python 才能找到 aurobox 這個套件
src_path = Path(__file__).parent.parent / 'src'
sys.path.insert(0, str(src_path))

from aurobox.app import create_app
from aurobox.config import load_config
from aurobox.models import db, Door, DoorStatus, RobotState

def main():
    # 初始化 Flask 環境
    config = load_config()
    app = create_app(config, reset_db=False)
    
    with app.app_context():
        print("="*40)
        print("正在查詢資料庫中的艙門狀態...")
        # 加上 order_by 讓輸出結果照 H_01, H_02, H_03 排序
        doors = Door.query.order_by(Door.door_number).all()
        
        if not doors:
            print("警告：資料庫裡面完全沒有艙門資料！請確認 app.py 是否有正常啟動並補齊艙門。")
        else:
            print(f"目前共有 {len(doors)} 個艙門：")
            for door in doors:
                print(f" - 門號: {door.door_number} | 狀態: {door.status} | 綁定任務: {door.door_task_id}")
        
        print("-" * 50)

        print("正在查詢資料庫中的【機器人狀態】...")
        robot_states = RobotState.query.all()
        
        if not robot_states:
            print("目前沒有機器人狀態記錄 (可能是尚未發送過任何導航指令)。")
        else:
            for state in robot_states:
                # 修改：將資料庫取出的 UTC 時間轉換為本機當地時間
                if state.updated_at:
                    local_time = state.updated_at.replace(tzinfo=timezone.utc).astimezone()
                    time_str = local_time.strftime('%Y-%m-%d %H:%M:%S')
                else:
                    time_str = 'N/A'
                    
                print(f" - 機器人 SN: {state.sn}")
                print(f" - 最後紀錄點位: {state.last_point}")
                print(f" - 當前任務 ID: {state.current_task_id}")
                print(f" - 資料更新時間: {time_str}")
                
        print("="*50)

if __name__ == '__main__':
    main()