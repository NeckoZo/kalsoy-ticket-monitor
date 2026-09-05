#!/usr/bin/env python3
"""Monitor SSL Klaksvík <-> Kalsoy vehicle availability and notify via ServerChan."""

from __future__ import annotations

import html
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


STATE_PATH = Path(os.getenv("STATE_PATH", ".monitor-state.json"))
TIMEOUT_SECONDS = 30
MONITOR_START_DAYS = 8
STATE_SLOT_SECONDS = 5 * 60


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def state_digest(value: str) -> str:
    sendkey = required_env("SERVERCHAN_SENDKEY").encode("utf-8")
    state_key = hmac.new(sendkey, b"kalsoy-state-v1", hashlib.sha256).digest()
    return hmac.new(state_key, value.encode("utf-8"), hashlib.sha256).hexdigest()


def configured_target_date() -> date:
    raw_date = required_env("TARGET_DATE_ISO")
    try:
        return date.fromisoformat(raw_date)
    except ValueError:
        raise RuntimeError("TARGET_DATE_ISO must use YYYY-MM-DD format") from None


def booking_date_values(target_day: date) -> tuple[str, str]:
    month_names = (
        "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
    )
    display_date = f"{target_day.day:02d}. {month_names[target_day.month - 1]}. {target_day.year}"
    api_date = target_day.strftime("%d%m%Y")
    return display_date, api_date


BOOKING_URL = required_env("BOOKING_URL")
TARGET_DAY = configured_target_date()
TARGET_DATE, TARGET_DATE_API = booking_date_values(TARGET_DAY)


def target_times(env_name: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in required_env(env_name).split(",") if item.strip())
    if not values:
        raise RuntimeError(f"{env_name} must contain at least one HH:MM value")
    if any(re.fullmatch(r"\d{2}:\d{2}", value) is None for value in values):
        raise RuntimeError(f"{env_name} must be a comma-separated list of HH:MM values")
    return values


ROUTES = (
    {
        "key": "outbound",
        "selected_route_id": "10002",
        "label": "Klaksvík → Kalsoy",
        "target_times": target_times("OUTBOUND_TARGET_TIMES"),
    },
    {
        "key": "return",
        "selected_route_id": "10003",
        "label": "Kalsoy → Klaksvík",
        "target_times": target_times("RETURN_TARGET_TIMES"),
    },
)


def schedule_allows(now: datetime, state: dict) -> tuple[bool, str]:
    days_until_target = (TARGET_DAY - now.date()).days
    if days_until_target > MONITOR_START_DAYS:
        return False, "monitoring window has not started"
    if days_until_target < 0:
        return False, "monitoring window ended"

    if days_until_target >= 4:
        cadence = timedelta(hours=2)
        cadence_name = "two-hour cadence"
    elif days_until_target >= 1:
        cadence = timedelta(hours=1)
        cadence_name = "hourly cadence"
    else:
        cadence = timedelta(minutes=30)
        cadence_name = "half-hour cadence"

    previous_check = state.get("last_check_mac")
    if isinstance(previous_check, str):
        current_slot = int(now.timestamp()) // STATE_SLOT_SECONDS
        search_slots = int(timedelta(hours=3).total_seconds()) // STATE_SLOT_SECONDS
        for steps_back in range(search_slots + 1):
            candidate_slot = current_slot - steps_back
            expected = state_digest(f"check:{candidate_slot}")
            if hmac.compare_digest(previous_check, expected):
                elapsed = timedelta(seconds=steps_back * STATE_SLOT_SECONDS)
                if elapsed < cadence - timedelta(minutes=5):
                    return False, f"waiting for {cadence_name}"
                break

    return True, cadence_name


def should_check_now(state: dict) -> tuple[bool, str, datetime]:
    """Apply the requested stepped schedule in Faroe Islands local time."""
    if os.getenv("GITHUB_EVENT_NAME") != "schedule":
        now = datetime.now(timezone.utc)
        return True, "manual/local run", now

    # The monitored period is in June/July, when the Faroe Islands use WEST
    # (UTC+1). Using a fixed offset avoids requiring the optional tzdata package.
    faroe_now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=1)))
    allowed, reason = schedule_allows(faroe_now, state)
    return allowed, reason, faroe_now


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
        raise RuntimeError(
            f"Unexpected GetTrips response type: {type(decoded).__name__}"
        )
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

    if not isinstance(state, dict):
        return {"routes": {}}
    if "routes" in state and isinstance(state["routes"], dict):
        return state

    # Migration from the original single-direction state file.
    return {
        "routes": {
            "outbound": {
                "available": bool(state.get("available", False)),
            }
        }
    }


def decode_route_states(state: dict) -> dict[str, bool]:
    decoded: dict[str, bool] = {}
    stored_routes = state.get("routes", {})
    if not isinstance(stored_routes, dict):
        return decoded

    for route in ROUTES:
        route_key = route["key"]
        stored = stored_routes.get(route_key)
        if isinstance(stored, dict) and isinstance(stored.get("available"), bool):
            decoded[route_key] = stored["available"]
            continue
        if isinstance(stored, str):
            for available in (False, True):
                expected = state_digest(f"route:{route_key}:{int(available)}")
                if hmac.compare_digest(stored, expected):
                    decoded[route_key] = available
                    break
    return decoded


def save_state(
    route_states: dict[str, bool],
    checked_at: datetime | None,
    previous_check_mac: str | None = None,
) -> None:
    protected_routes = {
        route_key: state_digest(f"route:{route_key}:{int(available)}")
        for route_key, available in route_states.items()
    }
    if checked_at is not None:
        check_slot = int(checked_at.timestamp()) // STATE_SLOT_SECONDS
        last_check_mac = state_digest(f"check:{check_slot}")
    elif isinstance(previous_check_mac, str):
        last_check_mac = previous_check_mac
    else:
        last_check_mac = state_digest("check:none")

    STATE_PATH.write_text(
        json.dumps(
            {
                "version": 2,
                "routes": protected_routes,
                "last_check_mac": last_check_mac,
            },
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
            f"日期：**{TARGET_DATE}**",
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
        raise RuntimeError(
            f"ServerChan rejected notification (code={result.get('code')!r})"
        )


def main() -> int:
    previous_state = load_state()
    previous_routes = decode_route_states(previous_state)
    allowed, _schedule_reason, checked_at = should_check_now(previous_state)
    if not allowed:
        save_state(
            previous_routes,
            None,
            previous_state.get("last_check_mac"),
        )
        print("Scheduled check skipped.")
        return 0

    print("Scheduled check started.")
    route_states: dict[str, bool] = {}
    newly_available: dict[str, dict[str, int]] = {}

    for route in ROUTES:
        page = request_page(route["selected_route_id"])
        all_counts = parse_availability(page)
        missing = [time for time in route["target_times"] if time not in all_counts]
        if missing:
            returned_date = re.findall(
                r'name="SelectedDate"[^>]*value="([^"]*)"', page, flags=re.I
            )
            returned_stored_values = re.findall(
                r'name="_storedValues"[^>]*value="([^"]*)"', page, flags=re.I
            )
            raise RuntimeError(
                f"Could not find {len(missing)} configured target trip(s) for "
                f"{route['key']}; parsed trip count: {len(all_counts)}; "
                f"returned date present: {bool(returned_date)}; "
                f"stored values present: {bool(returned_stored_values)}"
            )

        counts = {time: all_counts[time] for time in route["target_times"]}
        currently_available = any(count > 0 for count in counts.values())
        previously_available = previous_routes.get(route["key"], False)
        route_states[route["key"]] = currently_available
        if currently_available and not previously_available:
            newly_available[route["key"]] = counts

        print(f"{route['key']}: check completed")

    if newly_available:
        notify_serverchan(newly_available)
    print("Ticket check completed.")

    save_state(route_states, checked_at)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Upstream exceptions can embed URLs, query parameters, or credentials.
        print(f"ERROR: check failed ({type(exc).__name__}).", file=sys.stderr)
        raise SystemExit(1)
