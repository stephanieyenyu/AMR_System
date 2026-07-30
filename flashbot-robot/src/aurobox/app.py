"""Flask app factory."""

import os
from flask import Flask, jsonify
from .models import db, Door, DoorStatus, RobotState
from .services import FlashbotController
from .config import load_config
from .api import api_bp
# from .tasks import _hardware_watchdog
import threading

def ensure_default_doors(app: Flask) -> None:
    """Ensure default doors exist and reset them to empty at startup."""
    sn = app.config.get('ROBOT_SN')
    if not sn:
        return

    # 讀取設定檔中的 DOOR_MODE
    mode = app.config.get('DOOR_MODE', '4_DOORS')
    if mode == '3_DOORS':
        active_door_numbers = ("H_01", "H_02", "H_03")
    else:
        active_door_numbers = ("H_01", "H_02", "H_03", "H_04")

    existing_numbers = {
        row[0]
        for row in db.session.query(Door.door_number).filter_by(sn=sn).all()
    }

    missing_numbers = [
        door_number for door_number in active_door_numbers if door_number not in existing_numbers
    ]

    for door_number in missing_numbers:
        db.session.add(
            Door(
                sn=sn,
                door_number=door_number,
                status=DoorStatus.EMPTY.value,
                door_task_id=None,
            )
        )
    
    # 重置目前的邏輯門狀態
    doors = Door.query.filter_by(sn=sn).all()
    
    for door in doors:
        door.status = DoorStatus.EMPTY.value
        door.door_task_id = None

    robot_state = RobotState.query.filter_by(sn=sn).first()
    if robot_state:
        robot_state.current_task_id = None

    db.session.commit()

    # 新增：系統啟動時，自動對實體硬體下達「關閉所有艙門」指令
    try:
        controller = app.pudu_controller
        control_states = [
            {"operation": False, "door_number": door_number}
            for door_number in active_door_numbers
        ]
        
        # print(f"[系統] 啟動初始化：準備關閉所有實體艙門 {active_door_numbers}...", flush=True)
        controller.control_doors(sn=sn, control_states=control_states)
        print("[系統] 初始化關門指令發送成功，軟硬體狀態已同步為 EMPTY！", flush=True)
        
    except Exception as e:
        print(f"[系統] 初始化關門失敗 (機器人可能未連線): {e}", flush=True)
    
def create_app(config=None, reset_db=True):
    """Create and configure Flask app."""
    app = Flask(__name__)
    app_config = config or load_config()
    
    # 配置資料庫
    db_url = app_config.get('DATABASE_URL')
    if db_url:
        # 如果有設定 PostgreSQL，就用它
        app.config['SQLALCHEMY_DATABASE_URI'] = db_url
    else:
        # 否則退回 SQLite (相容舊版開發環境)
        db_path = os.path.join(os.path.dirname(__file__), '..', '..', 'instance')
        os.makedirs(db_path, exist_ok=True)
        app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}/aurobox.db'
    
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['JSON_AS_ASCII'] = False
    
    # 載入環境配置
    app_config = config or load_config()
    app.config['PUDU_API_KEY'] = app_config.get('APP_KEY')
    app.config['PUDU_API_SECRET'] = app_config.get('APP_SECRET')
    app.config['SHOP_ID'] = app_config.get('SHOP_ID')
    app.config['ROBOT_SN'] = app_config.get('DEFAULT_SN')
    app.config['DEFAULT_MAP_NAME'] = app_config.get('DEFAULT_MAP_NAME')
    app.config['HOME_POINT_NAME'] = app_config.get('HOME_POINT_NAME')
    app.config['CHARGE_POINT_NAME'] = app_config.get('CHARGE_POINT_NAME')
    app.config['CENTRAL_API_BASE_URL'] = app_config.get('CENTRAL_API_BASE_URL')

    app.pudu_controller = FlashbotController(app_config)
    app.home_point = app_config.get('HOME_POINT_NAME')
    app.charge_point = app_config.get('CHARGE_POINT_NAME')

    # 初始化資料庫
    db.init_app(app)
    
    # 建立表單 (只會建立 Door)
    with app.app_context():
        db.create_all()
        if reset_db:
            ensure_default_doors(app)
    
    # 註冊 API 藍圖 (不再註冊 webhooks)
    app.register_blueprint(api_bp, url_prefix='/api')

    controller = app.pudu_controller
    sn = app.config.get('ROBOT_SN')
    '''
    if controller and sn:
        watchdog_thread = threading.Thread(
            target=_hardware_watchdog,
            args=(app, controller, sn), # 傳入 tasks.py 定義的三個參數
            daemon=True # 設定 daemon=True，這樣 Flask 關閉時執行緒也會跟著乾淨關閉
        )
        watchdog_thread.start()
        print("[系統] Watchdog 執行緒已隨 App 啟動，負責監控硬體 STUCK 等異常", flush=True)
    '''
    '''
    push_thread = threading.Thread(
        target=_push_dashboard_status_loop,
        args=(app,), 
        daemon=True # 設定 daemon=True，這樣 Flask 關閉時執行緒也會跟著乾淨關閉
    )
    push_thread.start()
    '''
    @app.get('/')
    def index():
        return jsonify({
            'service': 'aurobox-flashbot-hardware',
            'status': 'ok',
            'endpoints': {
                'healthz': '/healthz',
                'dashboard_status': '/api/dashboard/status'
            }
        })

    @app.get('/healthz')
    def healthz():
        return jsonify({'status': 'ok'})

    @app.get('/favicon.ico')
    def favicon():
        return '', 204
    
    return app