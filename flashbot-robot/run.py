#!/usr/bin/env python
"""Run the Aurobox Flashbot application."""

import os
import sys
import argparse
import logging
from pathlib import Path

# 把 src 目錄加入系統路徑，這樣程式才能認得 aurobox 這個套件
src_path = Path(__file__).parent / 'src'
sys.path.insert(0, str(src_path))

os.environ['WERKZEUG_COLOR'] = '0'

from aurobox.app import create_app
from aurobox.config import load_config, require_config

# =========================================================
# 新增這段：建立過濾器，並套用到 Flask 預設的 werkzeug 伺服器
class NoFaviconFilter(logging.Filter):
    def filter(self, record):
        return record.getMessage().find("favicon.ico") == -1

logging.getLogger("werkzeug").addFilter(NoFaviconFilter())
# =========================================================

def main():
    parser = argparse.ArgumentParser(description='Run Aurobox Flashbot Hardware Server')
    
    # 【最關鍵的修改】將預設 host 從 127.0.0.1 改為 0.0.0.0
    # 這樣 Ngrok、Cloudflare 或同一台 WiFi 下的手機與雲端大腦，才能順利連進來！
    parser.add_argument('--host', default='0.0.0.0', help='Server host')
    parser.add_argument('--port', type=int, default=6000, help='Server port')
    # 開發期間，建議啟動時加上 --debug 參數
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    
    args = parser.parse_args()
    
    # 讀取並驗證 .env 環境變數 (檢查是否有漏填 APP_KEY 等資訊)
    config = load_config()
    require_config(config)
    
    # 建立 Flask 應用程式小腦
    app = create_app(config)
    
    print(f"===================================================")
    print(f"Flashbot 硬體控制伺服器啟動中...")
    print(f"監聽位址: http://{args.host}:{args.port}")
    print(f"===================================================")
    
    # 正式啟動伺服器
    app.run(
        host=args.host,
        port=args.port,
        debug=args.debug,
        use_reloader=False
    )

if __name__ == '__main__':
    main()