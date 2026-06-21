from __future__ import annotations

import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, time as dt_time, timedelta
from email.utils import formatdate
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

CONFIG_FILE = Path("channels.json")
EPG_FILE = Path("epg.xml")
STATUS_FILE = Path("status.json")

TIME_PATTERN = re.compile(r"^(\d{1,2}):(\d{2})\s*(AM|PM)$", re.IGNORECASE)
DATE_SUFFIX_PATTERN = re.compile(r"/\d{4}-\d{2}-\d{2}/?$")
DATE_IN_URL_PATTERN = re.compile(r"/(\d{4}-\d{2}-\d{2})(?:/)?$")
NOISE_PREFIXES = (
    "Sign In",
    "Email address",
    "Password",
    "Remember Me",
    "Or sign in",
    "Don't have an account",
    "TV Listings",
    "Menu",
    "Sports",
    "Highlights",
    "Quick Links",
    "About TV Passport",
    "Follow Us",
    "Privacy Manager",
    "Stay connected",
    "Do not share",
)
STOP_LINES = (
    "Do not share my Personal Information.",
    "Quick Links",
    "About TV Passport",
)
TRAILING_FLAGS = ("New", "Live", "HD", "CC", "Repeat")


@dataclass(frozen=True)
class Programme:
    channel_id: str
    start: datetime
    stop: datetime
    title: str
    subtitle: str = ""
    desc: str = ""
    is_new: bool = False
    is_live: bool = False
    source_url: str = ""


def load_config() -> dict[str, Any]:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Missing {CONFIG_FILE}")

    with CONFIG_FILE.open("r", encoding="utf-8") as file:
        config = json.load(file)

    required_top_level = ["timezone", "guide_hours", "channels"]
    for key in required_top_level:
        if key not in config:
            raise ValueError(f"channels.json is missing required key: {key}")

    if not config["channels"]:
        raise ValueError("channels.json must include at least one channel")

    return config


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_base_url(url: str) -> str:
    return DATE_SUFFIX_PATTERN.sub("", url.rstrip("/"))


def dated_url(base_url: str, day: date) -> str:
    return f"{normalize_base_url(base_url)}/{day.isoformat()}"


def fetch_html(session: requests.Session, url: str, attempts: int = 3) -> str:
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, timeout=30)
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            wait_seconds = attempt * 3
            print(f"Fetch failed for {url} on attempt {attempt}/{attempts}: {exc}", flush=True)
            if attempt < attempts:
                time.sleep(wait_seconds)

    raise RuntimeError(f"Could not fetch {url}: {last_error}")


def html_to_lines(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    lines = []
    for raw_line in soup.get_text("\n").splitlines():
        line = clean_text(raw_line)
        if line:
            lines.append(line)

    return lines


def find_schedule_start(lines: list[str]) -> int:
    for index, line in enumerate(lines):
        if line.startswith("Your Time Zone:"):
            return index + 1
    for index, line in enumerate(lines):
        if TIME_PATTERN.match(line):
            return index
    raise ValueError("Could not find the TV Passport schedule section")


def find_schedule_end(lines: list[str], start_index: int) -> int:
    for index in range(start_index, len(lines)):
        if any(lines[index].startswith(stop_line) for stop_line in STOP_LINES):
            return index
    return len(lines)


def is_noise_line(line: str) -> bool:
    if any(line.startswith(prefix) for prefix in NOISE_PREFIXES):
        return True
    if line in {"Today", "Tomorrow"}:
        return True
    if re.match(r"^[A-Z][a-z]+,\s+[A-Z][a-z]{2}\s+\d{1,2}$", line):
        return True
    return False


def parse_clock_time(value: str) -> dt_time | None:
    match = TIME_PATTERN.match(value.strip())
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))
    ampm = match.group(3).upper()

    if ampm == "AM" and hour == 12:
        hour = 0
    elif ampm == "PM" and hour != 12:
        hour += 12

    return dt_time(hour, minute)


def remove_trailing_flags(title_line: str) -> tuple[str, bool, bool]:
    parts = title_line.split()
    is_new = False
    is_live = False

    changed = True
    while changed and parts:
        changed = False
        last = parts[-1]
        if last in TRAILING_FLAGS:
            is_new = is_new or last == "New"
            is_live = is_live or last == "Live"
            parts.pop()
            changed = True

    title = " ".join(parts).strip() or title_line.strip()
    return title, is_new, is_live


def parse_title_and_description(block_lines: list[str]) -> tuple[str, str, str, bool, bool]:
    useful_lines = [line for line in block_lines if line and not is_noise_line(line)]
    if not useful_lines:
        return "", "", "", False, False

    title_line = useful_lines[0]
    title, is_new, is_live = remove_trailing_flags(title_line)

    remaining = useful_lines[1:]
    subtitle = ""
    desc_lines = remaining

    # TV Passport often places a linked show title first, then an episode title immediately after it.
    # This conservative rule only promotes a short second line to subtitle when there is more text after it.
    if len(remaining) >= 2 and len(remaining[0]) <= 100 and not remaining[0].endswith("."):
        subtitle = remaining[0]
        desc_lines = remaining[1:]

    desc = " ".join(desc_lines).strip()
    return title, subtitle, desc, is_new, is_live


def parse_date_from_url(url: str, fallback_day: date) -> date:
    match = DATE_IN_URL_PATTERN.search(urlparse(url).path)
    if match:
        return date.fromisoformat(match.group(1))
    return fallback_day


def parse_tvpassport_day(channel: dict[str, Any], html: str, url: str, fallback_day: date, timezone: ZoneInfo) -> list[dict[str, Any]]:
    lines = html_to_lines(html)
    start_index = find_schedule_start(lines)
    end_index = find_schedule_end(lines, start_index)
    schedule_lines = lines[start_index:end_index]

    base_day = parse_date_from_url(url, fallback_day)
    current_day = base_day
    previous_minutes: int | None = None
    slots: list[dict[str, Any]] = []

    index = 0
    while index < len(schedule_lines):
        line = schedule_lines[index]
        clock = parse_clock_time(line)
        if clock is None:
            index += 1
            continue

        next_index = index + 1
        while next_index < len(schedule_lines) and parse_clock_time(schedule_lines[next_index]) is None:
            next_index += 1

        block = schedule_lines[index + 1:next_index]
        title, subtitle, desc, is_new, is_live = parse_title_and_description(block)

        if title:
            minutes = clock.hour * 60 + clock.minute
            if previous_minutes is not None and minutes < previous_minutes:
                current_day = current_day + timedelta(days=1)
            previous_minutes = minutes

            start = datetime.combine(current_day, clock, tzinfo=timezone)
            slots.append({
                "channel_id": channel["id"],
                "start": start,
                "title": title,
                "subtitle": subtitle,
                "desc": desc,
                "is_new": is_new,
                "is_live": is_live,
                "source_url": url,
            })

        index = next_index

    return slots


def slots_to_programmes(slots: list[dict[str, Any]], guide_start: datetime, guide_stop: datetime) -> list[Programme]:
    programmes: list[Programme] = []
    slots.sort(key=lambda item: item["start"])

    for index, slot in enumerate(slots):
        start = slot["start"]
        if index + 1 < len(slots):
            stop = slots[index + 1]["start"]
        else:
            stop = start + timedelta(minutes=30)

        if stop <= guide_start or start >= guide_stop:
            continue

        if start < guide_start:
            start = guide_start
        if stop > guide_stop:
            stop = guide_stop

        if stop <= start:
            continue

        programmes.append(Programme(
            channel_id=slot["channel_id"],
            start=start,
            stop=stop,
            title=slot["title"],
            subtitle=slot.get("subtitle", ""),
            desc=slot.get("desc", ""),
            is_new=slot.get("is_new", False),
            is_live=slot.get("is_live", False),
            source_url=slot.get("source_url", ""),
        ))

    return programmes


def xmltv_datetime(value: datetime) -> str:
    return value.strftime("%Y%m%d%H%M%S %z")


def build_xmltv(config: dict[str, Any], programmes: list[Programme]) -> ET.ElementTree:
    tv = ET.Element("tv", {
        "generator-info-name": "kc-local-tvpassport-epg",
        "generator-info-url": config.get("public_base_url", "").rstrip("/") + "/epg.xml",
    })

    for channel in config["channels"]:
        channel_el = ET.SubElement(tv, "channel", {"id": channel["id"]})
        display_names = channel.get("display_names") or [channel["name"]]
        for display_name in display_names:
            ET.SubElement(channel_el, "display-name", {"lang": "en"}).text = display_name
        if channel.get("logo"):
            ET.SubElement(channel_el, "icon", {"src": channel["logo"]})

    for programme in sorted(programmes, key=lambda item: (item.channel_id, item.start)):
        programme_el = ET.SubElement(tv, "programme", {
            "start": xmltv_datetime(programme.start),
            "stop": xmltv_datetime(programme.stop),
            "channel": programme.channel_id,
        })
        ET.SubElement(programme_el, "title", {"lang": "en"}).text = programme.title
        if programme.subtitle:
            ET.SubElement(programme_el, "sub-title", {"lang": "en"}).text = programme.subtitle
        if programme.desc:
            ET.SubElement(programme_el, "desc", {"lang": "en"}).text = programme.desc
        if programme.is_new:
            ET.SubElement(programme_el, "new")
        if programme.is_live:
            ET.SubElement(programme_el, "live")

    return ET.ElementTree(tv)


def write_status(status: dict[str, Any]) -> None:
    STATUS_FILE.write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")


def validate_output(config: dict[str, Any], programmes: list[Programme], per_channel_counts: dict[str, int]) -> None:
    errors = []
    minimum = int(config.get("minimum_programmes_per_channel", 8))

    for channel in config["channels"]:
        count = per_channel_counts.get(channel["id"], 0)
        if count < minimum:
            errors.append(f"{channel['id']} produced only {count} programmes; minimum is {minimum}")

    if not programmes:
        errors.append("No programmes were generated")

    if errors:
        raise RuntimeError("EPG validation failed: " + "; ".join(errors))


def build_epg() -> int:
    config = load_config()
    timezone = ZoneInfo(config["timezone"])
    guide_hours = int(config.get("guide_hours", 72))
    time_shift = int(config.get("time_shift_hours", 0))

    now = datetime.now(timezone)
    guide_start = now
    guide_stop = now + timedelta(hours=guide_hours)

    session = requests.Session()
    session.headers.update({
        "User-Agent": "kc-local-epg-builder/1.0 (+https://github.com/cjrudermedia-1/kc-local-epg)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })

    all_slots: list[dict[str, Any]] = []
    fetch_errors: list[str] = []

    # Fetch yesterday through the guide window, plus one extra day for overnight rollover.
    days_needed = { (now + timedelta(days=offset)).date() for offset in range(-1, int(guide_hours / 24) + 3) }

    for channel in config["channels"]:
        base_url = channel["tvpassport_url"]
        for day in sorted(days_needed):
            url = dated_url(base_url, day)
            try:
                html = fetch_html(session, url)
                day_slots = parse_tvpassport_day(channel, html, url, day, timezone)
                for slot in day_slots:
                    slot["start"] = slot["start"] + timedelta(hours=time_shift)
                all_slots.extend(day_slots)
                print(f"{channel['id']} {day}: {len(day_slots)} listings", flush=True)
            except Exception as exc:  # noqa: BLE001
                message = f"{channel['id']} {day} failed: {exc}"
                print(message, flush=True)
                fetch_errors.append(message)

    programmes_by_channel: dict[str, list[Programme]] = {}
    for channel in config["channels"]:
        channel_slots = [slot for slot in all_slots if slot["channel_id"] == channel["id"]]
        programmes_by_channel[channel["id"]] = slots_to_programmes(channel_slots, guide_start, guide_stop)

    programmes = [programme for items in programmes_by_channel.values() for programme in items]
    per_channel_counts = {channel_id: len(items) for channel_id, items in programmes_by_channel.items()}

    status = {
        "ok": False,
        "generated_at": datetime.now(timezone).isoformat(),
        "generated_at_rfc2822": formatdate(localtime=True),
        "timezone": config["timezone"],
        "guide_hours": guide_hours,
        "time_shift_hours": time_shift,
        "programme_count": len(programmes),
        "per_channel_counts": per_channel_counts,
        "source": "TV Passport",
        "errors": fetch_errors,
    }

    validate_output(config, programmes, per_channel_counts)

    tree = build_xmltv(config, programmes)
    ET.indent(tree, space="  ", level=0)
    tree.write(EPG_FILE, encoding="utf-8", xml_declaration=True)

    status["ok"] = True
    write_status(status)
    print(json.dumps(status, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(build_epg())
    except Exception as exc:  # noqa: BLE001
        error_status = {
            "ok": False,
            "generated_at": datetime.now().isoformat(),
            "error": str(exc),
        }
        write_status(error_status)
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise
