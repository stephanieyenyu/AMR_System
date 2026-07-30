#!/usr/bin/env python3
import os
import json
from dotenv import load_dotenv

load_dotenv()
from aurobox.pudu_client import PuduApiClient

key = os.getenv('Pd_key')
secret = os.getenv('Pd_secret')
sn = os.getenv('FLASHBOT_SN')
shop = os.getenv('Aurotek_id')

client = PuduApiClient(app_key=key, app_secret=secret)
'''
print('Calling get_map_list...')
try:
    maps = client.get_map_list(sn)
    print(json.dumps(maps, ensure_ascii=False, indent=2))
except Exception as e:
    print('get_map_list failed:', e)
'''
print('\nCalling get_position...')
try:
    pos = client.get_position(sn)
    print(json.dumps(pos, ensure_ascii=False, indent=2))
except Exception as e:
    print('get_position failed:', e)
