#!/usr/bin/env python3
"""Standalone diagnostic for the MyWaterAdvisor / Harmony Encore API.

Run this directly (not through Home Assistant) to find out exactly why the
hourly-consumption endpoint returns 400. Prompts for credentials with
getpass, so the password is never echoed or left in shell history.

    python3 mwa_diag.py
"""
import getpass
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    sys.exit("This script needs the 'requests' package: pip3 install requests")

BASE_URL = "https://customerportal-api.harmonyencoremdm.com"
PORTAL_ROOT_URL = "https://mywateradvisor2.com"
KNOWN_APP_ID_FALLBACK = "3a869241-d476-40f6-a923-d789d63db11d"


def discover_app_id(session):
    try:
        html = session.get(PORTAL_ROOT_URL, timeout=15).text
    except requests.RequestException:
        return None
    for src in re.findall(r'<script[^>]+src="([^"]+\.js)"', html):
        url = src if src.startswith("http") else f"{PORTAL_ROOT_URL}{src if src.startswith('/') else '/' + src}"
        try:
            text = session.get(url, timeout=15).text
        except requests.RequestException:
            continue
        m = re.search(r'app:\s*"([0-9a-fA-F-]{36})"', text)
        if m:
            print(f"[info] discovered live app id from {url}: {m.group(1)}")
            return m.group(1)
    return None


def main():
    email = input("Email: ").strip()
    password = getpass.getpass("Password: ")

    session = requests.Session()
    app_id = discover_app_id(session) or KNOWN_APP_ID_FALLBACK
    print(f"[info] using app id: {app_id}")

    payload = {
        "email": email,
        "pw": password,
        "type": 1,
        "app": app_id,
        "deviceId": str(uuid.uuid4()),
        "osType": 3,
    }
    headers = {"x-app-id": app_id, "Content-Type": "application/json"}
    resp = session.post(f"{BASE_URL}/consumer/login", json=payload, headers=headers, timeout=15)
    print(f"[login] status={resp.status_code}")
    if resp.status_code >= 400:
        print(f"[login] body={resp.text}")
        return
    data = resp.json()
    token = data.get("token")
    if not token:
        print(f"[login] no token in response: {data}")
        return
    print("[login] OK, got token")

    auth_headers = {"x-app-id": app_id, "x-access-token": token}
    meters_resp = session.get(f"{BASE_URL}/consumer/meters", headers=auth_headers, timeout=15)
    print(f"\n[meters] status={meters_resp.status_code}")
    print(f"[meters] body={meters_resp.text}")
    if meters_resp.status_code >= 400:
        return

    meters = meters_resp.json()
    items = meters if isinstance(meters, list) else meters.get("meters") or meters.get("data") or []
    if not items:
        print("[meters] no meters in response")
        return
    meter = items[0]
    print(f"[meters] first meter object: {meter}")
    meter_id = meter.get("meterSn") or meter.get("id") or meter.get("meterId")
    print(f"[meters] using meter id: {meter_id}")

    today = datetime.now(timezone.utc).date()
    date_variants = {
        "MM-DD-YYYY (today as end)": lambda s, e: (s.strftime("%m-%d-%Y"), e.strftime("%m-%d-%Y")),
        "MM-DD-YYYY (yesterday as end)": lambda s, e: (s.strftime("%m-%d-%Y"), (e - timedelta(days=1)).strftime("%m-%d-%Y")),
        "YYYY-MM-DD": lambda s, e: (s.strftime("%Y-%m-%d"), e.strftime("%Y-%m-%d")),
        "M-D-YYYY (no leading zeros)": lambda s, e: (
            f"{s.month}-{s.day}-{s.year}",
            f"{e.month}-{e.day}-{e.year}",
        ),
    }

    start = today - timedelta(days=3)
    end = today

    for label, fmt in date_variants.items():
        start_str, end_str = fmt(datetime.combine(start, datetime.min.time()), datetime.combine(end, datetime.min.time()))
        for granularity in ("hourly", "daily"):
            url = f"{BASE_URL}/consumption/{granularity}/{meter_id}/{start_str}/{end_str}"
            r = session.get(url, headers=auth_headers, timeout=15)
            status = r.status_code
            body_preview = r.text[:300]
            print(f"\n[{granularity}] {label}: {url}")
            print(f"[{granularity}] status={status} body={body_preview}")


if __name__ == "__main__":
    main()
