"""End-to-end check of every Hum tool, using a throwaway copy of data/ (your real data is untouched).

Run:  python verify_tools.py          (direct tool calls + stdio MCP tools list)
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse, parse_qs

ROOT = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="hum_verify_"))
shutil.copytree(ROOT / "data", TMP / "data", ignore=shutil.ignore_patterns("history.json", "baselines"))
os.environ["HUM_DATA_DIR"] = str(TMP / "data")
os.environ["HUM_OUTBOX_DIR"] = str(TMP / "outbox")
os.environ.pop("ELEVENLABS_API_KEY", None)
sys.path.insert(0, str(ROOT))

import server as hum  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str, detail: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + (f"  ->  {detail}" if detail != "" else ""))
    if not cond:
        failures.append(label)


def parse_ics(text: str) -> dict:
    assert "\r\n" in text, "ICS must use CRLF line endings"
    unfolded = text.replace("\r\n ", "").split("\r\n")
    stack, props = [], {}
    for line in filter(None, unfolded):
        name, _, value = line.partition(":")
        if name == "BEGIN":
            stack.append(value)
        elif name == "END":
            assert stack and stack.pop() == value, f"unbalanced END:{value}"
        else:
            props.setdefault(name.split(";")[0], value)
    assert not stack, f"unclosed components {stack}"
    for req in ("VERSION", "PRODID", "UID", "DTSTAMP", "DTSTART", "SUMMARY"):
        assert req in props, f"missing {req}"
    return props


def direct_checks() -> None:
    apps = hum.list_appliances()
    check({a["name"] for a in apps} >= {"Kitchen Mixer", "Washing Machine"}, "seeded appliances listed", [a["name"] for a in apps])

    cold = hum.scan_appliance("samples/mixer_healthy_2.wav", "Kitchen Mixer")
    check(cold["ok"] and "warning" in cold and cold["compared_to"].startswith("generic"), "cold start uses generic reference + warning", cold["health"])
    shutil.rmtree(TMP / "data" / "history.json", ignore_errors=True)
    (TMP / "data" / "history.json").unlink(missing_ok=True)

    b = hum.set_baseline("samples/mixer_healthy.wav", "Kitchen Mixer")
    check(b["ok"], "set_baseline", b.get("baseline_frames"))

    h = hum.scan_appliance("samples/mixer_healthy_2.wav", "kitchen mixer")
    check(h["ok"] and h["health"] >= 85 and h["level"] == "Healthy", "healthy scan >= 85", h["health"])

    f = hum.scan_appliance("samples/mixer_faulty.wav", "Kitchen Mixer")
    check(f["ok"] and 45 <= f["health"] <= 70, "faulty scan in 45-70", f"{f['health']} {f['level']} region={f['changed_region']}")
    check(f["changed_region"] == "high", "faulty change is in the high band", f["region_change_scores"])
    check(len(f["possible_causes"]) >= 2 and "not a diagnosis" in f["possible_causes_note"], "possible causes labelled", f["possible_causes"])

    hist = hum.get_history("Kitchen Mixer")
    check(hist["trend"] == "worsening" and len(hist["readings"]) == 2, "history shows worsening", hist["trend"])

    w = hum.check_warranty("Kitchen Mixer")
    check(w["under_warranty"] and w["days_left"] > 0, "mixer under warranty", f"{w['warranty_until']} ({w['days_left']} days)")
    w2 = hum.check_warranty("Washing Machine")
    check(not w2["under_warranty"], "washing machine warranty expired", w2["warranty_until"])

    p = hum.create_reminder("Get Kitchen Mixer checked", "2026-11-28", "Health 58/100, high-frequency change; call brand service", confirm=False)
    outbox = TMP / "outbox"
    check(p["status"] == "preview" and not any(outbox.glob("*.ics")), "reminder preview writes nothing")
    c = hum.create_reminder("Get Kitchen Mixer checked", "2026-11-28", "Health 58/100, high-frequency change; call brand service", confirm=True)
    props = parse_ics(Path(c["ics_path"]).read_bytes().decode("utf-8"))
    check(c["status"] == "created" and props["DTSTART"] == "20261128", "reminder .ics created and parses", Path(c["ics_path"]).name)
    c2 = hum.create_reminder("Technician visit", "2026-11-28T17:30", "x" * 200, confirm=True)
    parse_ics(Path(c2["ics_path"]).read_bytes().decode("utf-8"))
    check(True, "timed reminder with long (folded) notes parses")

    m = hum.draft_technician_message("Kitchen Mixer", confirm=False)
    url = urlparse(m["whatsapp_link"])
    text = parse_qs(url.query)["text"][0]
    check(m["status"] == "preview" and url.netloc == "wa.me" and url.path == "/910000000000" and text == m["message"],
          "WhatsApp draft link valid", m["whatsapp_link"][:70] + "...")
    check(not any(outbox.glob("message_*")), "message preview writes nothing")
    ma = hum.draft_technician_message("Kitchen Mixer", confirm=True)
    check(ma["status"] == "approved" and Path(ma["saved_to"]).exists(), "approved draft saved (not sent)")

    s = hum.speak_summary("Your mixer needs a check this week.", "en")
    check(s["status"] == "voice disabled", "speak_summary without key -> voice disabled")

    bad = hum.scan_appliance("samples/does_not_exist.wav", "Kitchen Mixer")
    check(not bad["ok"], "missing file handled", bad.get("error"))
    try:
        hum.check_warranty("Toaster")
        check(False, "unknown appliance raises")
    except ValueError as exc:
        check(True, "unknown appliance gives helpful error", exc)

    print("\nSample faulty scan result:")
    for k in ("health", "level", "changed_region", "possible_causes", "recommended_action"):
        print(f"  {k}: {f[k]}")
    print(f"  warranty advice: {w['advice']}")


async def stdio_check() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")],
                                   env={**os.environ}, cwd=str(Path.home()))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            prompts = [p.name for p in (await s.list_prompts()).prompts]
            res = await s.call_tool("check_warranty", {"appliance": "Kitchen Mixer"})
    expected = sorted(["register_appliance", "list_appliances", "set_baseline", "scan_appliance", "get_history",
                       "check_warranty", "create_reminder", "draft_technician_message", "speak_summary"])
    check(tools == expected, "stdio server lists all 9 tools (launched from a different cwd)", tools)
    check(prompts == ["appliance_checkup"], "stdio server lists appliance_checkup prompt", prompts)
    check(not res.isError, "stdio tool call works")


if __name__ == "__main__":
    print("== Direct tool calls ==")
    direct_checks()
    print("\n== MCP over stdio ==")
    asyncio.run(stdio_check())
    shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{len(failures)} FAILED: {failures}'}")
    sys.exit(1 if failures else 0)
