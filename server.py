"""Hum: an MCP server that gives an AI assistant ears (and careful hands) for home appliances.

Read tools run freely. Write tools (create_reminder, draft_technician_message) are
preview-first: they return a draft and only write when called with confirm=True,
which the assistant must do ONLY after the user explicitly says yes.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

try:
    from mcp.server.mcpserver import MCPServer as Server  # mcp >= 2
except ImportError:
    from mcp.server.fastmcp import FastMCP as Server  # mcp 1.x

from audio_features import AudioError, build_profile, score_against

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("HUM_DATA_DIR", ROOT / "data"))
OUTBOX_DIR = Path(os.environ.get("HUM_OUTBOX_DIR", ROOT / "outbox"))
APPLIANCES_FILE = DATA_DIR / "appliances.json"
HISTORY_FILE = DATA_DIR / "history.json"
BASELINE_DIR = DATA_DIR / "baselines"
REFERENCE_DIR = DATA_DIR / "references"

DISCLAIMER = "Possible causes, not a diagnosis. Hum is a stethoscope, not an X-ray."

# appliance_type x changed region -> common, non-alarming possible causes.
POSSIBLE_CAUSES: dict[str, dict[str, list[str]]] = {
    "washing_machine": {
        "low": ["unbalanced or overloaded drum", "machine not level / feet loose", "worn shock absorbers"],
        "mid": ["drain pump straining (lint or debris)", "loose panel or hose vibrating", "worn drive belt"],
        "high": ["drum bearing wear", "coin or hairpin stuck in the drum", "dry or worn motor brushes"],
    },
    "mixer": {
        "low": ["jar not seated firmly", "base not on a flat surface", "overloaded jar"],
        "mid": ["blade assembly loose", "worn coupler between jar and motor", "lid not locked properly"],
        "high": ["blade or coupler rubbing", "worn motor bearings", "worn carbon brushes"],
    },
    "ac": {
        "low": ["compressor working harder (possible low refrigerant or dirty coils)", "loose mounting bracket", "outdoor unit vibrating on its stand"],
        "mid": ["dirty or bent fan blades", "clogged air filter", "loose panel or screw"],
        "high": ["fan motor bearing wear", "debris touching the blower", "refrigerant hiss (needs a technician)"],
    },
    "fridge": {
        "low": ["compressor starting harder than usual", "fridge not level", "back panel touching the wall"],
        "mid": ["condenser fan dusty or obstructed", "loose drain pan", "ice build-up near the evaporator fan"],
        "high": ["evaporator or condenser fan bearing wear", "ice touching a fan blade", "relay clicking repeatedly"],
    },
    "fan": {
        "low": ["blades unbalanced (dust build-up)", "loose canopy or mounting", "wobbling down-rod"],
        "mid": ["loose blade screws", "regulator hum", "guard or cover rattling"],
        "high": ["bearing needs lubrication or replacement", "blade touching the guard", "worn capacitor causing whine"],
    },
    "water_pump": {
        "low": ["air trapped in the line (needs priming)", "loose foundation bolts", "low water level at the inlet"],
        "mid": ["partially blocked foot valve or strainer", "impeller wear", "loose pipe clamp"],
        "high": ["dry running or cavitation", "motor bearing wear", "debris in the impeller"],
    },
}
GENERIC_CAUSES = {
    "low": ["unit not level or loosely mounted", "unbalanced load", "loose mounting"],
    "mid": ["loose panel, screw or cover", "dust or debris build-up", "worn belt or coupling"],
    "high": ["bearing wear", "something rubbing a moving part", "worn motor brushes"],
}

mcp = Server("hum")


# ---------- storage helpers ----------

def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_") or "appliance"


def _appliances() -> list[dict]:
    return _read_json(APPLIANCES_FILE, [])


def _find(appliance: str) -> dict:
    key = _slug(appliance)
    items = _appliances()
    for a in items:
        if a["id"] == key or _slug(a["name"]) == key:
            return a
    matches = [a for a in items if key in a["id"]]
    if len(matches) == 1:
        return matches[0]
    names = ", ".join(a["name"] for a in items) or "none yet"
    raise ValueError(f"No appliance called '{appliance}'. Registered appliances: {names}.")


def _history() -> list[dict]:
    return _read_json(HISTORY_FILE, [])


def _latest_reading(appliance_id: str) -> dict | None:
    readings = [h for h in _history() if h["appliance_id"] == appliance_id]
    return readings[-1] if readings else None


def _resolve_audio(audio_path: str) -> Path:
    p = Path(audio_path).expanduser()
    if p.is_absolute():
        return p
    for base in (Path.cwd(), ROOT):
        if (base / p).exists():
            return base / p
    return ROOT / p


def _level(health: float) -> str:
    if health >= 75:
        return "Healthy"
    if health >= 45:
        return "Watch"
    return "Get it checked"


def _action(level: str) -> str:
    return {
        "Healthy": "No action needed. Scan again in a month, or whenever it sounds different.",
        "Watch": "Sounds different from its healthy self. Get it checked within a week; check warranty first.",
        "Get it checked": "Clear change from its healthy sound. Stop heavy use and get it checked in the next day or two; check warranty first.",
    }[level]


def _add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y += d.year
    m += 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def _warranty(a: dict) -> dict:
    start = date.fromisoformat(a["purchase_date"])
    end = _add_months(start, int(a["warranty_months"]))
    days_left = (end - date.today()).days
    return {"purchase_date": start.isoformat(), "warranty_until": end.isoformat(), "days_left": days_left, "under_warranty": days_left >= 0}


# ---------- tools ----------

@mcp.tool()
def register_appliance(
    name: str,
    appliance_type: str,
    brand: str = "",
    model: str = "",
    purchase_date: str = "",
    warranty_months: int = 12,
    service_number: str = "",
    technician_name: str = "",
    technician_phone: str = "",
) -> dict:
    """Register (or update) a home appliance Hum should look after.

    appliance_type: one of washing_machine, mixer, ac, fridge, fan, water_pump (others allowed).
    purchase_date: ISO date YYYY-MM-DD. warranty_months: total warranty length in months.
    technician_phone: digits with country code, e.g. 91XXXXXXXXXX (used for a WhatsApp draft link).
    """
    if purchase_date:
        date.fromisoformat(purchase_date)  # validate
    else:
        purchase_date = date.today().isoformat()
    entry = {
        "id": _slug(name),
        "name": name.strip(),
        "appliance_type": _slug(appliance_type),
        "brand": brand,
        "model": model,
        "purchase_date": purchase_date,
        "warranty_months": int(warranty_months),
        "service_number": service_number,
        "technician": {"name": technician_name, "phone": re.sub(r"\D", "", technician_phone)},
    }
    items = [a for a in _appliances() if a["id"] != entry["id"]]
    items.append(entry)
    _write_json(APPLIANCES_FILE, items)
    return {"saved": entry, "next_step": f"Record ~10 s of the {name} running normally and call set_baseline."}


@mcp.tool()
def list_appliances() -> list[dict]:
    """List all registered appliances with baseline status, latest health reading and warranty status."""
    out = []
    for a in _appliances():
        latest = _latest_reading(a["id"])
        w = _warranty(a)
        out.append({
            "name": a["name"],
            "appliance_type": a["appliance_type"],
            "brand": a.get("brand", ""),
            "has_baseline": (BASELINE_DIR / f"{a['id']}.json").exists(),
            "latest_health": latest["health"] if latest else None,
            "latest_level": latest["level"] if latest else None,
            "last_scanned": latest["timestamp"] if latest else None,
            "under_warranty": w["under_warranty"],
            "warranty_until": w["warranty_until"],
        })
    return out


@mcp.tool()
def set_baseline(audio_path: str, appliance: str) -> dict:
    """Save a HEALTHY reference sound for an appliance (best when it is new or just serviced).

    audio_path: a WAV recording (~10 s) of the appliance running normally. Relative paths are
    resolved against the Hum folder, e.g. samples/mixer_healthy.wav.
    """
    a = _find(appliance)
    try:
        profile = build_profile(_resolve_audio(audio_path))
    except AudioError as exc:
        return {"ok": False, "error": str(exc)}
    _write_json(BASELINE_DIR / f"{a['id']}.json", profile)
    return {"ok": True, "appliance": a["name"], "baseline_frames": profile["n_frames"],
            "message": f"Healthy baseline saved for {a['name']}. Future scans compare against this sound."}


@mcp.tool()
def scan_appliance(audio_path: str, appliance: str) -> dict:
    """Listen to a new recording and compare it with the appliance's own healthy baseline.

    Returns health 0-100, level (Healthy >=75 / Watch 45-74 / Get it checked <45), which frequency
    region changed (low <300 Hz / mid 300 Hz-2 kHz / high >2 kHz), POSSIBLE causes for this appliance
    type (not a diagnosis), and a recommended action. The reading is logged to history.
    Never tell the user an exact remaining life or name a broken part with certainty.
    """
    a = _find(appliance)
    baseline_file = BASELINE_DIR / f"{a['id']}.json"
    warning = None
    if baseline_file.exists():
        profile, compared_to = _read_json(baseline_file, None), "own healthy baseline"
    elif (REFERENCE_DIR / f"{a['appliance_type']}.json").exists():
        profile = _read_json(REFERENCE_DIR / f"{a['appliance_type']}.json", None)
        compared_to = f"generic {a['appliance_type']} reference"
        warning = ("No baseline for this appliance yet, so Hum compared it with a generic reference for its type. "
                   "This is less accurate. Record a baseline when it is running well (set_baseline).")
    else:
        return {"ok": False, "error": f"No healthy baseline for {a['name']} and no generic reference for "
                f"type '{a['appliance_type']}'. Record ~10 s while it runs normally and call set_baseline first."}
    try:
        result = score_against(profile, _resolve_audio(audio_path))
    except AudioError as exc:
        return {"ok": False, "error": str(exc)}

    level = _level(result["health"])
    causes = POSSIBLE_CAUSES.get(a["appliance_type"], GENERIC_CAUSES)[result["changed_region"]]
    reading = {
        "appliance_id": a["id"],
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "file": Path(audio_path).name,
        "health": result["health"],
        "level": level,
        "changed_region": result["changed_region"],
        "compared_to": compared_to,
    }
    hist = _history()
    hist.append(reading)
    _write_json(HISTORY_FILE, hist)

    out = {
        "ok": True,
        "appliance": a["name"],
        "health": result["health"],
        "level": level,
        "compared_to": compared_to,
        "changed_region": result["changed_region"],
        "region_change_scores": result["region_change_scores"],
        "loudness_change_db": result["loudness_change_db"],
        "centroid_change_hz": result["centroid_change_hz"],
        "possible_causes": causes if level != "Healthy" else [],
        "possible_causes_note": DISCLAIMER,
        "recommended_action": _action(level),
    }
    if warning:
        out["warning"] = warning
    return out


@mcp.tool()
def get_history(appliance: str) -> dict:
    """Past health readings for an appliance with dates, plus a trend (improving / stable / worsening)."""
    a = _find(appliance)
    readings = [h for h in _history() if h["appliance_id"] == a["id"]]
    trend = "not enough readings"
    if len(readings) >= 2:
        recent = readings[-5:]
        delta = recent[-1]["health"] - recent[0]["health"]
        trend = "worsening" if delta <= -5 else "improving" if delta >= 5 else "stable"
    return {"appliance": a["name"], "trend": trend, "readings": readings}


@mcp.tool()
def check_warranty(appliance: str) -> dict:
    """Check whether an appliance is still under warranty, how many days are left, and who to call first."""
    a = _find(appliance)
    w = _warranty(a)
    if w["under_warranty"]:
        advice = (f"Still under warranty until {w['warranty_until']} ({w['days_left']} days left). "
                  f"Call the brand's service line first ({a.get('service_number') or 'number not saved'}); "
                  "the repair is likely free. Avoid opening it or using a local technician, which can void the warranty.")
    else:
        advice = (f"Warranty ended on {w['warranty_until']}. A trusted local technician is reasonable; "
                  "ask for a quote before work starts. Check if you bought an extended warranty.")
    return {"appliance": a["name"], "brand": a.get("brand", ""), "service_number": a.get("service_number", ""), **w, "advice": advice}


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n")


def _ics_fold(line: str) -> str:
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not parts else 74):
            parts.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    parts.append(cur.decode("utf-8"))
    return "\r\n ".join(parts)


@mcp.tool()
def create_reminder(title: str, date_iso: str, notes: str = "", confirm: bool = False) -> dict:
    """Create a calendar reminder (.ics file + Google Calendar link).

    PREVIEW-FIRST: call with confirm=False to show the user a preview. Only call again with
    confirm=True after the user explicitly says yes. date_iso: YYYY-MM-DD (all-day, alert at 9:00)
    or YYYY-MM-DDTHH:MM (30-minute event).
    """
    if "T" in date_iso:
        start = datetime.fromisoformat(date_iso)
        end = start + timedelta(minutes=30)
        dt_lines = [f"DTSTART:{start.strftime('%Y%m%dT%H%M%S')}", f"DTEND:{end.strftime('%Y%m%dT%H%M%S')}"]
        alarm = "TRIGGER:-PT30M"
        gcal_dates = f"{start.strftime('%Y%m%dT%H%M%S')}/{end.strftime('%Y%m%dT%H%M%S')}"
        when = start.strftime("%d %b %Y, %H:%M")
    else:
        d = date.fromisoformat(date_iso)
        dt_lines = [f"DTSTART;VALUE=DATE:{d.strftime('%Y%m%d')}", f"DTEND;VALUE=DATE:{(d + timedelta(days=1)).strftime('%Y%m%d')}"]
        alarm = "TRIGGER:PT9H"
        gcal_dates = f"{d.strftime('%Y%m%d')}/{(d + timedelta(days=1)).strftime('%Y%m%d')}"
        when = d.strftime("%d %b %Y (all day)")
    gcal = ("https://calendar.google.com/calendar/render?action=TEMPLATE"
            f"&text={quote(title)}&dates={gcal_dates}&details={quote(notes)}")
    preview = {"title": title, "when": when, "notes": notes}
    if not confirm:
        return {"status": "preview", "reminder": preview,
                "ask_user": "Shall I create this reminder? (Nothing has been saved yet.)"}

    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Hum//Appliance Health//EN", "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uuid.uuid4()}@hum.local",
        f"DTSTAMP:{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
        *dt_lines,
        f"SUMMARY:{_ics_escape(title)}",
        f"DESCRIPTION:{_ics_escape(notes)}",
        "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_escape(title)}", alarm, "END:VALARM",
        "END:VEVENT", "END:VCALENDAR",
    ]
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTBOX_DIR / f"reminder_{_slug(title)[:40]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.ics"
    path.write_bytes(("\r\n".join(_ics_fold(l) for l in lines) + "\r\n").encode("utf-8"))
    return {"status": "created", "reminder": preview, "ics_path": str(path), "google_calendar_link": gcal,
            "how_to_use": "Open the .ics file to add it to your calendar, or open the Google Calendar link."}


@mcp.tool()
def draft_technician_message(appliance: str, confirm: bool = False) -> dict:
    """Draft a polite WhatsApp message to the appliance's technician (or brand service if under warranty).

    PREVIEW-FIRST: confirm=False returns the draft and a wa.me link for the user to review. Only call with
    confirm=True after the user explicitly approves; that saves the approved draft to outbox/.
    Hum NEVER sends messages: the user taps Send in WhatsApp themselves.
    """
    a = _find(appliance)
    latest = _latest_reading(a["id"])
    w = _warranty(a)
    tech = a.get("technician", {}) or {}
    who = tech.get("name") or "there"
    lines = [f"Hello {who}, this is about my {a.get('brand', '')} {a['name']} ({a['appliance_type'].replace('_', ' ')}).".replace("  ", " ")]
    if latest:
        causes = POSSIBLE_CAUSES.get(a["appliance_type"], GENERIC_CAUSES)[latest["changed_region"]]
        lines.append(f"It has started sounding different. A sound check on {latest['timestamp'][:10]} gave health "
                     f"{latest['health']:.0f}/100 ({latest['level']}), with most of the change in the "
                     f"{latest['changed_region']}-frequency range.")
        lines.append(f"Possible causes (not a diagnosis): {', '.join(causes)}.")
    else:
        lines.append("It has started sounding different from usual.")
    if w["under_warranty"]:
        lines.append(f"It is still under warranty until {w['warranty_until']}.")
    else:
        lines.append("It is out of warranty, so could you share an estimate before starting work?")
    lines.append("Could you please take a look this week? Please let me know a convenient time. Thank you!")
    text = "\n".join(lines)

    phone = tech.get("phone", "")
    link = f"https://wa.me/{phone}?text={quote(text)}" if phone else None
    result = {"to": tech.get("name") or "(no technician saved)", "phone": phone, "message": text, "whatsapp_link": link}
    if not phone:
        result["note"] = "No technician phone saved; register_appliance with technician_phone to get a WhatsApp link."
    if not confirm:
        return {"status": "preview", **result,
                "ask_user": "Does this message look right? Say yes to approve it. You will still tap Send in WhatsApp yourself."}
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTBOX_DIR / f"message_{a['id']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    path.write_text(f"APPROVED DRAFT (not sent)\nTo: {result['to']} ({phone})\n\n{text}\n\nWhatsApp link: {link}\n", encoding="utf-8")
    return {"status": "approved", **result, "saved_to": str(path),
            "next_step": "Open the WhatsApp link and tap Send yourself. Hum never sends messages."}


@mcp.tool()
def speak_summary(text: str, language: str = "en") -> dict:
    """Optional: turn a short summary into speech with ElevenLabs and save an mp3 to outbox/.

    Write `text` in the language you want spoken (language: en, hi, ta, ...). Needs the ELEVENLABS_API_KEY
    environment variable; without it this returns 'voice disabled'. Optional ELEVENLABS_VOICE_ID overrides the voice.
    """
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        return {"status": "voice disabled", "reason": "ELEVENLABS_API_KEY is not set."}
    import urllib.error
    import urllib.request

    voice = os.environ.get("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
        data=json.dumps({"text": text[:2500], "model_id": "eleven_multilingual_v2"}).encode("utf-8"),
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            audio = resp.read()
    except urllib.error.HTTPError as exc:
        return {"status": "error", "reason": f"ElevenLabs returned HTTP {exc.code}: {exc.read()[:300].decode('utf-8', 'replace')}"}
    except urllib.error.URLError as exc:
        return {"status": "error", "reason": f"Could not reach ElevenLabs: {exc.reason}"}
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTBOX_DIR / f"summary_{_slug(language)}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp3"
    path.write_bytes(audio)
    return {"status": "saved", "mp3_path": str(path), "language": language}


@mcp.prompt()
def appliance_checkup(appliance: str, audio_path: str) -> str:
    """Guided check-up: scan, check warranty, propose actions, and ask before any write."""
    return f"""Please give my {appliance} a check-up using the Hum tools, recording: {audio_path}

1. Call scan_appliance. Explain the health score, level and changed region in plain words.
   Present possible causes as possibilities, not a diagnosis. Never predict exact remaining life.
2. If the level is Watch or Get it checked, call check_warranty and tell me who to call first.
3. Propose next steps: a reminder (create_reminder) and/or a technician message (draft_technician_message).
   Call them with confirm=False first and show me the preview.
4. Only call confirm=True after I clearly say yes. Never send anything on my behalf.
5. Keep the answer short and calm."""


if __name__ == "__main__":
    mcp.run()  # stdio
