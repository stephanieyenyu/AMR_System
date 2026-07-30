import argparse
import concurrent.futures
import time
from collections import Counter

import requests


DEFAULT_BASE_URL = "http://127.0.0.1:5000"


def _safe_json(response: requests.Response):
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    return response.text


def _print_header(title: str):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def case_assign_quantity(base_url: str, package_id: str, quantity: int, timeout: int):
    _print_header("[Case 1] 多包裹分配：quantity > 1")
    url = f"{base_url}/api/packages/{package_id}/assign"
    payload = {"quantity": quantity}
    print(f"POST {url}")
    print(f"payload={payload}")

    started = time.time()
    resp = requests.post(url, json=payload, timeout=timeout)
    elapsed = time.time() - started
    body = _safe_json(resp)

    print(f"HTTP {resp.status_code} | {elapsed:.3f}s")
    print(body)


def _send_assign(base_url: str, request_id: int, quantity: int, timeout: int):
    package_id = f"LOAD_PKG_{request_id:03d}"
    url = f"{base_url}/api/packages/{package_id}/assign"
    try:
        resp = requests.post(url, json={"quantity": quantity}, timeout=timeout)
        return {
            "req_id": request_id,
            "status": resp.status_code,
            "body": _safe_json(resp),
        }
    except requests.exceptions.RequestException as exc:
        return {
            "req_id": request_id,
            "status": "network_error",
            "body": str(exc),
        }


def case_assign_concurrent(base_url: str, requests_count: int, quantity: int, timeout: int):
    _print_header("[Case 2] 併發分配：concurrent assign")
    print(f"同時送出 {requests_count} 筆 assign，quantity={quantity}")

    started = time.time()
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=requests_count) as executor:
        futures = [
            executor.submit(_send_assign, base_url, idx, quantity, timeout)
            for idx in range(1, requests_count + 1)
        ]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
    elapsed = time.time() - started

    code_counter = Counter(str(item["status"]) for item in results)
    print(f"總耗時: {elapsed:.3f}s")
    print(f"狀態碼統計: {dict(code_counter)}")

    for item in sorted(results, key=lambda x: x["req_id"]):
        print(f"req={item['req_id']:02d} status={item['status']} body={item['body']}")


def case_return_timeout(base_url: str, timeout: int):
    _print_header("[Case 3] 退件逾時：return-timeout")
    url = f"{base_url}/api/doors/return-timeout"
    print(f"POST {url}")

    started = time.time()
    resp = requests.post(url, timeout=timeout)
    elapsed = time.time() - started

    print(f"HTTP {resp.status_code} | {elapsed:.3f}s")
    print(_safe_json(resp))


def case_recall(base_url: str, timeout: int):
    _print_header("[Case 4] 緊急召回：recall")
    url = f"{base_url}/api/robot/recall"
    print(f"POST {url}")

    started = time.time()
    resp = requests.post(url, timeout=timeout)
    elapsed = time.time() - started

    print(f"HTTP {resp.status_code} | {elapsed:.3f}s")
    print(_safe_json(resp))


def main():
    parser = argparse.ArgumentParser(description="Aurobox 0.4.0 load test scenarios")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="API base url")
    parser.add_argument("--timeout", type=int, default=8, help="request timeout seconds")
    parser.add_argument("--quantity", type=int, default=2, help="quantity for quantity test")
    parser.add_argument("--concurrency", type=int, default=20, help="request count for concurrent assign")
    parser.add_argument("--concurrent-quantity", type=int, default=1, help="quantity for each concurrent assign")
    parser.add_argument("--package-id", default=f"QTY_CASE_{int(time.time())}", help="package id for quantity case")
    args = parser.parse_args()

    print("Aurobox 0.4.0 load tests starting...")
    print(f"base_url={args.base_url}")

    case_assign_quantity(args.base_url, args.package_id, args.quantity, args.timeout)
    case_assign_concurrent(args.base_url, args.concurrency, args.concurrent_quantity, args.timeout)
    case_return_timeout(args.base_url, args.timeout)
    case_recall(args.base_url, args.timeout)


if __name__ == "__main__":
    main()