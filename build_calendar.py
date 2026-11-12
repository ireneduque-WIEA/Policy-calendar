#!/usr/bin/env python3
"""
POLICY CALENDAR BUILDER
=======================
Reads EU policy sources, keeps the items relevant to the files listed in
config.yaml, and writes:

  events.ics    -> the calendar Outlook subscribes to
  status.html   -> a small page showing which sources worked on the last run
  data/         -> memory between runs (so a source that fails one day
                   doesn't wipe its events from the calendar)

Runs every morning on GitHub (see .github/workflows/update-calendar.yml).
Each source is independent: if one website changes or is down, the others
still update and the calendar is still published.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import re
import sys
import traceback
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
TODAY = dt.date.today()
NOW_UTC = dt.datetime.now(dt.timezone.utc)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 policy-calendar",
    "Accept-Language": "en",
}

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

MONTHS = {
    name: i
    for i, names in enumerate(
        [("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
         ("may",), ("june", "jun"), ("july", "jul"), ("august", "aug"),
         ("september", "sep", "sept"), ("october", "oct"), ("november", "nov"),
         ("december", "dec")], start=1)
    for name in names
}
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
DATE_PATTERNS = [
    # 2026-11-23  or 2026/11/23
    (re.compile(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b"), ("y", "m", "d")),
    # 23/11/2026  or 23.11.2026
    (re.compile(r"\b(\d{1,2})[./](\d{1,2})[./](\d{4})\b"), ("d", "m", "y")),
    # 23 November 2026, 23rd Nov. 2026
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_RE})\.?,?\s+(\d{{4}})\b", re.I), ("d", "M", "y")),
    # November 23, 2026
    (re.compile(rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I), ("M", "d", "y")),
]


def find_dates(text: str) -> list[dt.date]:
    """Return every calendar date found in a piece of text, in order of appearance."""
    found: list[tuple[int, dt.date]] = []
    for rx, order in DATE_PATTERNS:
        for m in rx.finditer(text or ""):
            parts = dict(zip(order, m.groups()))
            try:
                month = MONTHS[parts["M"].lower()] if "M" in parts else int(parts["m"])
                found.append((m.start(), dt.date(int(parts["y"]), month, int(parts["d"]))))
            except (ValueError, KeyError):
                continue
    found.sort(key=lambda x: x[0])
    return [d for _, d in found]


def clean(html_or_text: str) -> str:
    text = BeautifulSoup(html_or_text or "", "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def fetch(url: str, **kw) -> requests.Response:
    r = requests.get(url, headers={**HEADERS, **kw.pop("headers", {})}, timeout=60, **kw)
    r.raise_for_status()
    return r


def render(urls: list[str], wait_ms: int = 4000) -> dict[str, tuple[str, list[str]]]:
    """Open pages in a headless browser (for sites that load content with
    JavaScript). Returns {url: (visible_text, [links])}."""
    from playwright.sync_api import sync_playwright

    out = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(user_agent=HEADERS["User-Agent"], locale="en-GB")
        for url in urls:
            page.goto(url, wait_until="networkidle", timeout=90_000)
            page.wait_for_timeout(wait_ms)
            text = page.inner_text("body")
            links = page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
            out[url] = (text, links)
        browser.close()
    return out


class Matcher:
    """Decides whether a text is about one of your files."""

    def __init__(self, cfg: dict):
        self.files = {name: [k.lower() for k in kws] for name, kws in cfg["files"].items()}
        self.related = [k.lower() for k in cfg.get("related_keywords", [])]

    def match(self, text: str) -> tuple[list[str], list[str]]:
        t = (text or "").lower()
        files = [name for name, kws in self.files.items() if any(k in t for k in kws)]
        related = [k for k in self.related if re.search(rf"\b{re.escape(k)}", t)]
        return files, related

    @staticmethod
    def label(files: list[str], related: list[str]) -> str:
        if files:
            return "[" + ", ".join(files) + "] "
        if related:
            return "[related] "
        return ""

    def snippets(self, text: str, limit: int = 6) -> list[str]:
        """Lines of text that contain a keyword (for event descriptions)."""
        kws = [k for v in self.files.values() for k in v] + self.related
        out = []
        for line in re.split(r"[\n\r]+|(?<=[.;])\s+", text or ""):
            line = line.strip()
            if 8 < len(line) < 400 and any(k in line.lower() for k in kws):
                if line not in out:
                    out.append(line)
            if len(out) >= limit:
                break
        return out


def make_event(source: str, title: str, start: dt.date, url: str = "",
               description: str = "", end: dt.date | None = None) -> dict:
    uid = hashlib.sha1(f"{source}|{url}|{start}|{title}".encode()).hexdigest()[:20]
    return {
        "uid": uid, "source": source, "title": title.strip(), "start": start.isoformat(),
        "end": (end or start).isoformat(), "url": url, "description": description.strip(),
    }


# --------------------------------------------------------------------------
# Sources  (each returns a list of events)
# --------------------------------------------------------------------------

def rss_events(url: str, m: Matcher, source: str, prefix: str,
               always_include: list[str] | None = None) -> list[dict]:
    soup = BeautifulSoup(fetch(url).content, "xml")
    events = []
    for item in soup.find_all("item"):
        title = clean(item.title.get_text() if item.title else "")
        desc = clean(item.description.get_text() if item.description else "")
        link = (item.link.get_text() if item.link else "").strip()
        text = f"{title} {desc}"
        files, related = m.match(text)
        person = any(p.lower() in text.lower() for p in (always_include or []))
        if not (files or related or person):
            continue
        dates = find_dates(text)
        if not dates and item.pubDate:
            try:
                from email.utils import parsedate_to_datetime
                dates = [parsedate_to_datetime(item.pubDate.get_text()).date()]
            except (TypeError, ValueError):
                pass
        if not dates:
            continue
        start = min(dates)
        end = max(dates) if (max(dates) - start).days <= 7 else start
        events.append(make_event(source, f"{m.label(files, related)}{prefix}{title}", start,
                                 link, desc[:600], end))
    return events


def src_commission_calendar(cfg, m):
    return rss_events(cfg["rss"], m, "commission_calendar", "EC: ",
                      cfg.get("always_include_people"))


def src_ep_thinktank_events(cfg, m):
    return rss_events(cfg["rss"], m, "ep_thinktank_events", "EP event: ")


def src_ep_plenary(cfg, m):
    """Plenary sitting days from the EP Open Data Portal, grouped into sessions."""
    days: set[dt.date] = set()
    for year in sorted({TODAY.year, (TODAY + dt.timedelta(days=60)).year}):
        r = fetch(cfg["api"], params={"year": year, "format": "application/ld+json",
                                      "offset": 0, "limit": 500},
                  headers={"Accept": "application/ld+json"})
        blob = json.dumps(r.json())
        for y, mo, d in re.findall(r"MTG-PL-(\d{4})-(\d{2})-(\d{2})", blob):
            days.add(dt.date(int(y), int(mo), int(d)))
        for y, mo, d in re.findall(r'"activity_date"\s*:\s*"(\d{4})-(\d{2})-(\d{2})', blob):
            days.add(dt.date(int(y), int(mo), int(d)))
    days = sorted(d for d in days if d >= TODAY - dt.timedelta(days=30))
    if not days:
        raise RuntimeError("no plenary dates found in API response")
    events, block = [], [days[0]]
    for d in days[1:] + [None]:
        if d is not None and (d - block[-1]).days <= 3:
            block.append(d)
            continue
        where = "Strasbourg" if len(block) >= 3 else "Brussels"
        events.append(make_event("ep_plenary", f"EP plenary session ({where})", block[0],
                                 "https://www.europarl.europa.eu/plenary/en/home.html",
                                 "Check the agenda for votes on your files.", block[-1]))
        if d is not None:
            block = [d]
    return events


def src_envi_agendas(cfg, m):
    months, d = [], TODAY.replace(day=1)
    for _ in range(int(cfg.get("months_ahead", 2))):
        months.append(d.strftime("%Y%m"))
        d = (d + dt.timedelta(days=32)).replace(day=1)
    month_urls = [cfg["url_template"].format(yyyymm=mm) for mm in months]
    pages = render(month_urls)
    meeting_urls = sorted({link for _, (_, links) in pages.items() for link in links
                           if "meeting=ENVI" in link})[:12]
    if meeting_urls:
        pages.update(render(meeting_urls))

    per_day: dict[dt.date, dict] = {}
    for url, (text, _) in pages.items():
        current = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            ds = find_dates(line)
            if ds and len(line) < 120:
                current = ds[0]
                continue
            if current is None:
                continue
            files, related = m.match(line)
            if files or related:
                slot = per_day.setdefault(current, {"files": set(), "related": set(),
                                                    "lines": [], "url": url})
                slot["files"].update(files)
                slot["related"].update(related)
                if line not in slot["lines"] and len(slot["lines"]) < 8:
                    slot["lines"].append(line[:300])
    events = []
    for day, s in per_day.items():
        label = m.label(sorted(s["files"]), sorted(s["related"]))
        events.append(make_event("envi_agendas", f"{label}ENVI committee", day, s["url"],
                                 "On the agenda:\n- " + "\n- ".join(s["lines"])))
    return events


COUNCIL_NAMES = {
    "env": "Environment Council", "agrifish": "Agriculture and Fisheries Council",
    "gac": "General Affairs Council", "compet": "Competitiveness Council",
    "euco": "European Council", "tte": "Transport, Telecommunications and Energy Council",
    "ecofin": "Economic and Financial Affairs Council",
}
COUNCIL_LINK = re.compile(r"/en/meetings/([a-z0-9-]+)/(\d{4})/(\d{1,2})/(\d{1,2})/?")


def src_council_meetings(cfg, m):
    confs = set(cfg.get("configurations", []))
    url = cfg["calendar_url"]
    links: list[tuple[str, str]] = []
    try:
        soup = BeautifulSoup(fetch(url).text, "html.parser")
        links = [(a["href"], a.get_text(" ", strip=True)) for a in soup.find_all("a", href=True)]
    except requests.RequestException:
        pass
    if not any(COUNCIL_LINK.search(h) for h, _ in links):  # page loads via JS -> use browser
        _, hrefs = render([url])[url]
        links = [(h, "") for h in hrefs]

    seen, events = set(), []
    for href, text in links:
        mt = COUNCIL_LINK.search(href)
        if not mt:
            continue
        conf, y, mo, d = mt.group(1), *map(int, mt.groups()[1:])
        try:
            day = dt.date(y, mo, d)
        except ValueError:
            continue
        files, related = m.match(text)
        if (conf not in confs and not files) or day < TODAY - dt.timedelta(days=30):
            continue
        full = requests.compat.urljoin("https://www.consilium.europa.eu", mt.group(0))
        if full in seen:
            continue
        seen.add(full)
        title = COUNCIL_NAMES.get(conf, text or conf.upper())
        desc = ""
        # Look inside the meeting page for your files (agenda appears ~1 week before)
        if TODAY <= day <= TODAY + dt.timedelta(days=21):
            try:
                page = clean(fetch(full).text)
                files, related = m.match(page)
                hits = m.snippets(page, 5)
                if hits:
                    desc = "Mentions on meeting page:\n- " + "\n- ".join(hits)
            except requests.RequestException:
                pass
        events.append(make_event("council_meetings", f"{m.label(files, related)}{title}",
                                 day, full, desc))
    if not seen:
        raise RuntimeError("no meeting links found on Council calendar page")
    return events


def src_coreper1_agendas(cfg, m):
    from pypdf import PdfReader

    soup = BeautifulSoup(fetch(cfg["register_url"]).text, "html.parser")
    by_day: dict[dt.date, dict] = {}
    count = 0
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "data.consilium.europa.eu/doc/document/" not in href or not href.endswith("/pdf"):
            continue
        row = a
        for _ in range(4):  # climb to the table row / list item holding the dates
            if row.parent is None:
                break
            row = row.parent
            if row.name in ("tr", "li", "article"):
                break
        row_text = row.get_text(" ", strip=True)
        if "agenda" not in row_text.lower():
            continue
        dates = find_dates(row_text)
        if not dates:
            continue
        meeting = max(dates)
        if meeting < TODAY - dt.timedelta(days=7):
            continue
        count += 1
        if count > int(cfg.get("max_documents", 15)):
            break
        try:
            pdf = PdfReader(io.BytesIO(fetch(href).content))
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)
        except Exception:  # noqa: BLE001 - a single bad PDF must not stop the run
            continue
        files, related = m.match(text)
        slot = by_day.setdefault(meeting, {"files": set(), "related": set(), "hits": [], "url": href})
        slot["files"].update(files)
        slot["related"].update(related)
        slot["url"] = href  # newest listed version
        for h in m.snippets(text, 6):
            if h not in slot["hits"] and len(slot["hits"]) < 8:
                slot["hits"].append(h)
    events = []
    for day, s in by_day.items():
        label = m.label(sorted(s["files"]), sorted(s["related"]))
        desc = ("Agenda items matching your files:\n- " + "\n- ".join(s["hits"])) if s["hits"] \
            else "No item matching your keywords on the provisional agenda."
        events.append(make_event("coreper1_agendas", f"{label}Coreper I", day, s["url"], desc))
    return events


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def src_have_your_say(cfg, m):
    events, seen = [], set()
    for term in cfg.get("search_terms", []):
        r = fetch(cfg["api"], params={"text": term, "page": 0, "size": 50, "language": "EN"},
                  headers={"Accept": "application/json"})
        for item in _walk(r.json()):
            if "id" not in item or not any(k in item for k in ("shortTitle", "title")):
                continue
            title = clean(str(item.get("shortTitle") or item.get("title")))
            ends = [find_dates(str(v)) for k, v in _flatten(item) if "enddate" in k.lower()]
            ends = [d for ds in ends for d in ds if d >= TODAY]
            if not ends or (item["id"], min(ends)) in seen:
                continue
            files, related = m.match(title)
            if not (files or related or term.lower() in title.lower()):
                continue
            seen.add((item["id"], min(ends)))
            url = f"https://ec.europa.eu/info/law/better-regulation/have-your-say/initiatives/{item['id']}_en"
            events.append(make_event("have_your_say",
                                     f"{m.label(files, related)}Have your say DEADLINE: {title}",
                                     min(ends), url, "Feedback / consultation closes today."))
    return events


def _flatten(obj, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _flatten(v, f"{prefix}.{k}")
    elif isinstance(obj, list):
        for v in obj:
            yield from _flatten(v, prefix)
    else:
        yield prefix, obj


def src_legislative_train(cfg, m, state):
    """Creates an event when a tracked Legislative Train page gets a newer update."""
    memory = state.setdefault("legislative_train", {})
    events = []
    for name, url in cfg["files"].items():
        text = clean(fetch(url).text)
        dates = [d for d in find_dates(text) if d <= TODAY]
        if not dates:
            continue
        latest = max(dates)
        status = re.search(r"\b(Announced|Tabled|Blocked|Close to adoption|"
                           r"Adopted\s*/\s*Completed|Withdrawn)\b", text)
        status = status.group(1) if status else "see page"
        before = memory.get(name, {}).get("latest")
        if before is None or latest.isoformat() > before:
            what = "Last update" if before is None else "NEW update"
            events.append(make_event("legislative_train",
                                     f"Legislative Train – {what}: {name}",
                                     latest if before is None else TODAY, url,
                                     f"Page shows an entry dated {latest}. Status: {status}."))
        memory[name] = {"latest": latest.isoformat(), "status": status}
    return events


SOURCES = {
    "commission_calendar": src_commission_calendar,
    "ep_thinktank_events": src_ep_thinktank_events,
    "ep_plenary": src_ep_plenary,
    "envi_agendas": src_envi_agendas,
    "council_meetings": src_council_meetings,
    "coreper1_agendas": src_coreper1_agendas,
    "have_your_say": src_have_your_say,
    "legislative_train": src_legislative_train,
}
APPEND_ONLY = {"legislative_train"}  # change-based: keep earlier events


# --------------------------------------------------------------------------
# Calendar file
# --------------------------------------------------------------------------

def ics_escape(s: str) -> str:
    return (s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
             .replace("\r\n", "\n").replace("\n", "\\n"))


def fold(line: str) -> str:
    """Wrap lines at 75 bytes as the iCalendar standard requires."""
    out, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > (75 if not out else 74):
            out.append(cur.decode("utf-8"))
            cur = b""
        cur += b
    out.append(cur.decode("utf-8"))
    return "\r\n ".join(out)


def write_ics(events: list[dict], name: str, path: Path) -> None:
    stamp = NOW_UTC.strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//policy-calendar//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{ics_escape(name)}",
        "X-WR-TIMEZONE:Europe/Brussels", "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
        "X-PUBLISHED-TTL:PT6H",
    ]
    for e in sorted(events, key=lambda x: (x["start"], x["title"])):
        start = dt.date.fromisoformat(e["start"])
        end = dt.date.fromisoformat(e["end"]) + dt.timedelta(days=1)  # all-day: exclusive end
        desc = e["description"]
        if e["url"]:
            desc = f"{desc}\n\n{e['url']}" if desc else e["url"]
        lines += [
            "BEGIN:VEVENT",
            f"UID:{e['uid']}@policy-calendar",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{start:%Y%m%d}",
            f"DTEND;VALUE=DATE:{end:%Y%m%d}",
            f"SUMMARY:{ics_escape(e['title'])}",
            f"DESCRIPTION:{ics_escape(desc)}",
            "TRANSP:TRANSPARENT",  # shows as 'free', doesn't block your agenda
        ]
        if e["url"]:
            lines.append(f"URL:{e['url']}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    path.write_text("\r\n".join(fold(l) for l in lines) + "\r\n", encoding="utf-8")


def write_status(results: dict, total: int, path: Path) -> None:
    rows = "".join(
        f"<tr><td>{n}</td><td>{'✅ OK' if r['ok'] else '❌ FAILED'}</td>"
        f"<td>{r['count']}</td><td><code>{r.get('error', '')[:300]}</code></td></tr>"
        for n, r in results.items())
    path.write_text(f"""<!doctype html><meta charset="utf-8"><title>Policy calendar status</title>
<style>body{{font-family:sans-serif;max-width:900px;margin:2em auto}}td,th{{border:1px solid #ccc;padding:6px}}
table{{border-collapse:collapse}}</style>
<h1>Policy calendar – last run</h1>
<p>Run at {NOW_UTC:%Y-%m-%d %H:%M} UTC · {total} events in calendar ·
<a href="events.ics">events.ics</a></p>
<table><tr><th>Source</th><th>Result</th><th>Events</th><th>Error</th></tr>{rows}</table>
""", encoding="utf-8")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def load_manual(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for e in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
        start = dt.date.fromisoformat(str(e["date"]))
        end = dt.date.fromisoformat(str(e.get("end", e["date"])))
        out.append(make_event("manual", e["title"], start, e.get("url", ""),
                              e.get("description", ""), end))
    return out


def dedupe(events: list[dict]) -> list[dict]:
    """Merge events that describe the same thing on the same day."""
    merged: dict[tuple, dict] = {}
    for e in events:
        key = (e["start"], re.sub(r"^\[[^\]]*\]\s*", "", e["title"]).lower())
        if key not in merged:
            merged[key] = dict(e)
            continue
        kept = merged[key]
        if len(e["title"]) > len(kept["title"]):  # keep the more informative title
            kept["title"] = e["title"]
        if e["description"] and e["description"] not in kept["description"]:
            kept["description"] = (kept["description"] + "\n\n" + e["description"]).strip()
        kept["url"] = kept["url"] or e["url"]
    return list(merged.values())


def main() -> int:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    m = Matcher(cfg)
    DATA.mkdir(exist_ok=True)
    store_path, state_path = DATA / "events.json", DATA / "state.json"
    store = json.loads(store_path.read_text()) if store_path.exists() else {}
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    results = {}
    for name, func in SOURCES.items():
        scfg = cfg["sources"].get(name, {})
        if not scfg.get("enabled", False):
            continue
        try:
            new = func(scfg, m, state) if name in APPEND_ONLY else func(scfg, m)
            if name in APPEND_ONLY:
                old = {e["uid"]: e for e in store.get(name, [])}
                old.update({e["uid"]: e for e in new})
                new = list(old.values())
            store[name] = new
            results[name] = {"ok": True, "count": len(new)}
            print(f"OK    {name}: {len(new)} events")
        except Exception as exc:  # noqa: BLE001 - keep yesterday's events for this source
            results[name] = {"ok": False, "count": len(store.get(name, [])),
                             "error": f"{type(exc).__name__}: {exc}"}
            print(f"FAIL  {name}: {exc}")
            traceback.print_exc()

    # drop old events, add manual ones, merge duplicates
    cutoff = (TODAY - dt.timedelta(days=int(cfg.get("keep_past_days", 45)))).isoformat()
    for name in store:
        store[name] = [e for e in store[name] if e["end"] >= cutoff]
    events = [e for evs in store.values() for e in evs]
    events += [e for e in load_manual(ROOT / "manual_events.yaml") if e["end"] >= cutoff]
    events = dedupe(events)

    write_ics(events, cfg.get("calendar_name", "Policy calendar"), ROOT / "events.ics")
    write_status(results, len(events), ROOT / "status.html")
    store_path.write_text(json.dumps(store, indent=1, ensure_ascii=False), encoding="utf-8")
    state_path.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"\nCalendar written with {len(events)} events.")

    # Only report failure to GitHub if *every* source failed
    return 1 if results and not any(r["ok"] for r in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
