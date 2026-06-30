#!/usr/bin/env python3
"""Monitor SSL Klaksvík <-> Kalsoy vehicle availability and notify via ServerChan."""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


BOOKING_URL = (
    "https://booking.ssl.fo/Booking/SelectDate?"
    "&hideHelpText=False&routeIdOut=10002&isReturn=True&routeIdReturn=10003"
    "&isPaxOnly=False&isStandBy=False&ADT=1&isUnknowRegnum=False"
    "&shipVehicleType=33&propSysId=ICEV&length=4.6&height=1.8"
    "&isWide=False&useCookie=False&isMonthCard=False"
    "&IsBusinessBooking=False&IsBusinessBookingAnswered=False"
    "&IsUseStoredCreditCardAnswered=False"
)
TARGET_DATE = os.getenv("TARGET_DATE", "02. Jul. 2026")
TARGET_DATE_API = os.getenv("TARGET_DATE_API", "02072026")
STATE_PATH = Path(os.getenv("STATE_PATH", ".monitor-state.json"))
TIMEOUT_SECONDS = 30


def target_times(env_name: str, default: str) -> tuple[str, ...]:
    return tuple(
        item.strip() for item in os.getenv(env_name, default).split(",") if item.strip()
    )


ROUTES = (
    {
        "key": "outbound",
        "selected_route_id": "10002",
        "label": "Klaksvík → Kalsoy",
        "target_times": target_times("OUTBOUND_TARGET_TIMES", "08:00,09:00"),
    },
    {
        "key": "return",
        "selected_route_id": "10003",
        "label": "Kalsoy → Klaksvík",
        "target_times": target_times("RETURN_TARGET_TIMES", "15:10,16:30,17:35"),
    },
)


def schedule_allows(now: datetime) -> tuple[bool, str]:
    target_day = now.date().isoformat()

    if target_day < "2026-06-29":
        allowed = True
        cadence = "every 2 hours"
    elif target_day < "2026-07-02":
        allowed = True
        cadence = "every hour"
    elif target_day == "2026-07-02":
        allowed = True
        cadence = "every 30 minutes"
    else:
        allowed = False
        cadence = "monitoring window ended"

    return allowed, f"{now.isoformat(timespec='minutes')} ({cadence})"


def should_check_now() -> tuple[bool, str]:
    """Apply the requested stepped schedule in Faroe Islands local time."""
    if os.getenv("GITHUB_EVENT_NAME") != "schedule":
        return True, "manual/local run"

    # The monitored period is in June/July, when the Faroe Islands use WEST
    # (UTC+1). Using a fixed offset avoids requiring the optional tzdata package.
    # GitHub scheduled workflows can start tens of minutes late, so the script
    # must not reject a scheduled run based on the actual start minute. The
    # segmented cron already controls the cadence; this guard only prevents the
    # yearly cron expression from running outside the 2026 monitoring window.
    faroe_now = datetime.now(timezone.utc) + timedelta(hours=1)
    return schedule_allows(faroe_now)


def request_page(selected_route_id: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    stored_values = urllib.parse.urlsplit(BOOKING_URL).query.lstrip("&")
    route_data = f"?&SelectedRouteId={selected_route_id}&{stored_values}"
    api_url = "https://booking.ssl.fo/Booking/GetTrips?" + urllib.parse.urlencode(
        {"data": route_data, "date": TARGET_DATE_API}
    )
    request = urllib.request.Request(
        api_url, headers={**headers, "Referer": BOOKING_URL}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        raw = response.read().decode(response.headers.get_content_charset() or "utf-8")
    decoded = json.loads(raw)
    if not isinstance(decoded, str):
        raise RuntimeError(f"Unexpected GetTrips response: {decoded!r}")
    return decoded


def parse_availability(page: str) -> dict[str, int]:
    labels = re.findall(r"<label\b[^>]*>(.*?)</label>", page, flags=re.I | re.S)
    availability: dict[str, int] = {}
    for raw_label in labels:
        text = re.sub(r"<[^>]+>", "", raw_label)
        text = html.unescape(text).strip()
        match = re.fullmatch(r"(\d{2}:\d{2})\s*\((\d+)\)", text)
        if match:
            availability[match.group(1)] = int(match.group(2))
    return availability


def load_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"routes": {}}

    if "routes" in state and isinstance(state["routes"], dict):
        return state

    # Migration from the original single-direction state file.
    return {
        "routes": {
            "outbound": {
                "available": bool(state.get("available", False)),
                "counts": state.get("counts", {}),
            }
        }
    }


def save_state(route_states: dict[str, dict]) -> None:
    STATE_PATH.write_text(
        json.dumps(
            {"routes": route_states},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def notify_serverchan(newly_available: dict[str, dict[str, int]]) -> None:
    sendkey = os.getenv("SERVERCHAN_SENDKEY", "").strip()
    if not sendkey:
        raise RuntimeError("Missing SERVERCHAN_SENDKEY")

    available_lines: list[str] = []
    for route in ROUTES:
        route_counts = newly_available.get(route["key"], {})
        route_lines = [
            f"- **{time}**：剩余 {count} 个车位"
            for time, count in route_counts.items()
            if count > 0
        ]
        if route_lines:
            available_lines.extend([f"### {route['label']}", *route_lines, ""])

    title = "Klaksvík ↔ Kalsoy 有船票了"
    description = "\n".join(
        [
            f"日期：**2026-07-02**",
            "",
            "本次发现以下目标班次有余票：",
            "",
            *available_lines,
            "请尽快打开订票页面完成预订：",
            "",
            BOOKING_URL,
        ]
    )
    endpoint = f"https://sctapi.ftqq.com/{urllib.parse.quote(sendkey)}.send"
    payload = urllib.parse.urlencode({"title": title, "desp": description}).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=payload,
        headers={"User-Agent": "kalsoy-ticket-monitor/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        result = json.loads(response.read().decode("utf-8"))

    if result.get("code") != 0:
        raise RuntimeError(f"ServerChan rejected notification: {result}")


def main() -> int:
    allowed, schedule_reason = should_check_now()
    if not allowed:
        print(f"Schedule gate skipped this run: {schedule_reason}")
        return 0

    print(f"Schedule gate accepted this run: {schedule_reason}")
    previous_state = load_state()
    previous_routes = previous_state.get("routes", {})
    route_states: dict[str, dict] = {}
    newly_available: dict[str, dict[str, int]] = {}

    for route in ROUTES:
        page = request_page(route["selected_route_id"])
        all_counts = parse_availability(page)
        missing = [time for time in route["target_times"] if time not in all_counts]
        if missing:
            visible_times = re.findall(r"\b\d{2}:\d{2}\b", page)
            returned_date = re.findall(
                r'name="SelectedDate"[^>]*value="([^"]*)"', page, flags=re.I
            )
            returned_stored_values = re.findall(
                r'name="_storedValues"[^>]*value="([^"]*)"', page, flags=re.I
            )
            cleaned_page = re.sub(
                r"<(?:script|style)\b.*?</(?:script|style)>",
                " ",
                page,
                flags=re.I | re.S,
            )
            diagnostic_text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", cleaned_page))
            raise RuntimeError(
                f"Could not find target trips {missing} for {route['label']}; "
                f"parsed trips: {all_counts}; "
                f"times visible in response: {visible_times[:20]}; "
                f"returned date: {returned_date[:1]}; "
                f"stored values present: {bool(returned_stored_values)}; "
                f"page text: {html.unescape(diagnostic_text[:800]).strip()}"
            )

        counts = {time: all_counts[time] for time in route["target_times"]}
        currently_available = any(count > 0 for count in counts.values())
        previously_available = bool(
            previous_routes.get(route["key"], {}).get("available", False)
        )
        route_states[route["key"]] = {
            "available": currently_available,
            "counts": counts,
        }
        if currently_available and not previously_available:
            newly_available[route["key"]] = counts

        print(f"{TARGET_DATE} {route['label']}: {counts}")

    if newly_available:
        notify_serverchan(newly_available)
        print("Availability detected; ServerChan notification sent.")
    elif any(route_state["available"] for route_state in route_states.values()):
        print("Still available; notification already sent.")
    else:
        print("No target availability.")

    save_state(route_states)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, urllib.error.URLError, TimeoutError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
