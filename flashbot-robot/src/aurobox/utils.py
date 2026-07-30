"""Utility functions for Aurobox."""
from flask import current_app

def build_custom_call_payload(
    sn: str,
    *,
    point: str | None = None,
    map_name: str | None = None,
    point_type: str = 'table',
    call_device_name: str = 'dashboard',
    call_mode: str = 'CALL',
    task_id: str | None = None,
    mode_data: dict | None = None,
    do_not_queue: bool = True,
    robot_group_ids: list | None = None,
    filter_category_ids: list | None = None,
    priority: int = 1,
) -> dict:
    """Build a standard custom_call2 payload with app defaults."""
    payload = {
        'sn': sn,
        'shop_id': current_app.config.get('SHOP_ID'),
        'call_device_name': call_device_name,
        'call_mode': call_mode,
        "task_id": task_id,
        'mode_data': mode_data or {},
        'do_not_queue': do_not_queue,
        'robot_group_ids': robot_group_ids or [],
        'filter_category_ids': filter_category_ids or [],
        'priority': priority,
    }

    resolved_map_name = map_name if map_name is not None else current_app.config.get('DEFAULT_MAP_NAME')
    if resolved_map_name:
        payload['map_name'] = resolved_map_name

    if point is not None:
        payload['point'] = point
        payload['point_type'] = point_type

    return payload