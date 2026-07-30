import argparse
import json

from .config import load_config, require_config
from .robot import FlashbotController


def print_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Aurobox Flashbot CLI")
    parser.add_argument("--sn", help="Robot serial number (SN)")
    parser.add_argument("--shop-id", help="Shop ID for map-related commands")

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("status", help="Get robot status")
    subparsers.add_parser("position", help="Get robot position")
    subparsers.add_parser("recharge", help="Request robot recharge")
    subparsers.add_parser("map-list", help="Get robot map list")
    subparsers.add_parser("door-state", help="Get robot door state")

    open_map_parser = subparsers.add_parser("open-map", help="Open a map")
    open_map_parser.add_argument("--map-name", required=True, help="Map name to open")

    call_parser = subparsers.add_parser("call", help="Send a custom call to robot")
    call_parser.add_argument("--map-name", required=True, help="Map name")
    call_parser.add_argument("--point", required=True, help="Point name")
    call_parser.add_argument("--point-type", default="table", help="Point type")
    call_parser.add_argument("--call-mode", default="CALL", help="Call mode")
    call_parser.add_argument("--priority", type=int, default=1, help="Call priority")

    args = parser.parse_args(argv)
    config = require_config(load_config())
    controller = FlashbotController(config)

    if args.command == "status":
        result = controller.get_status(args.sn)
    elif args.command == "position":
        result = controller.get_position(args.sn)
    elif args.command == "recharge":
        result = controller.recharge(args.sn)
    elif args.command == "map-list":
        result = controller.get_map_list(args.sn)
    elif args.command == "door-state":
        result = controller.get_door_state(args.sn)
    elif args.command == "open-map":
        result = controller.open_map(args.shop_id, args.map_name)
    elif args.command == "call":
        result = controller.custom_call(
            args.sn,
            args.shop_id,
            args.map_name,
            args.point,
            point_type=args.point_type,
            call_mode=args.call_mode,
            priority=args.priority,
        )
    else:
        parser.error("Unknown command")

    print_json(result)


if __name__ == "__main__":
    main()
