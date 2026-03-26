import json
import os
import sys
import urllib.error
import urllib.request


BASE_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")


def request_json(method: str, path: str, payload: dict | None = None):
    url = f"{BASE_URL}{path}"
    data = None
    headers = {"Accept": "application/json"}

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url=url, data=data, method=method, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            body = response.read().decode("utf-8", errors="replace")
            status = response.getcode()
    except urllib.error.HTTPError as error:
        status = error.code
        body = error.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as error:
        print(f"❌ Could not reach backend at {BASE_URL}: {error}")
        sys.exit(1)

    try:
        parsed = json.loads(body) if body else None
    except json.JSONDecodeError:
        parsed = body

    return status, parsed


def print_result(name: str, status: int, body):
    icon = "✅" if 200 <= status < 300 else "⚠️"
    print(f"{icon} {name} -> HTTP {status}")
    if isinstance(body, (dict, list)):
        print(json.dumps(body, indent=2))
    else:
        print(body)
    print("-" * 60)


def main():
    seat_id = "A12"
    student_id = "ucl123456"
    rfid_uid = "866-865-866"

    print(f"Testing backend: {BASE_URL}")
    print("=" * 60)

    steps = [
        ("GET /", "GET", "/", None),
        ("GET /students", "GET", "/students", None),
        (
            "POST /reserve",
            "POST",
            "/reserve",
            {"seat_id": seat_id, "student_id": student_id},
        ),
        (
            "POST /tap checkin",
            "POST",
            "/tap",
            {
                "seat_id": seat_id,
                "rfid_uid": rfid_uid,
                "user_id": f"user_{rfid_uid}",
                "action": "checkin",
            },
        ),
        ("GET /seat/{seat_id}", "GET", f"/seat/{seat_id}", None),
        (
            "POST /tap checkout",
            "POST",
            "/tap",
            {
                "seat_id": seat_id,
                "rfid_uid": rfid_uid,
                "user_id": f"user_{rfid_uid}",
                "action": "checkout",
            },
        ),
        ("GET /seat/{seat_id} (after checkout)", "GET", f"/seat/{seat_id}", None),
        ("GET /sessions", "GET", "/sessions", None),
    ]

    for name, method, path, payload in steps:
        status, body = request_json(method, path, payload)
        print_result(name, status, body)


if __name__ == "__main__":
    main()
