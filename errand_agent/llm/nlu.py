"""Deterministic language understanding used ONLY by the offline RuleModel (the
stand-in for a real LLM in automated evals). The real model does its own NLU."""
from __future__ import annotations

import re
from datetime import date, timedelta

SERVICE_WORDS = [
    (r"\b(plumb\w*|tap|leak\w*|pipe|sink|drain|faucet)\b", "plumbing"),
    (r"\b(electric\w*|wiring|switch|fan|socket|mcb)\b", "electrical"),
    (r"\b(ac|a\.c\.|air ?con\w*)\b", "ac_service"),
    (r"\b(carpent\w*|furniture|cupboard|door hinge)\b", "carpentry"),
    (r"\b(clean\w*|maid)\b", "cleaning"),
    (r"\b(pest|cockroach\w*|termite\w*)\b", "pest_control"),
]
AREAS = {"indiranagar": "Indiranagar", "koramangala": "Koramangala", "hsr layout": "HSR Layout",
         "hsr": "HSR Layout", "whitefield": "Whitefield", "jayanagar": "Jayanagar",
         "malleshwaram": "Malleshwaram"}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3}


def find_service(t: str) -> str | None:
    for pat, svc in SERVICE_WORDS:
        if re.search(pat, t, re.I):
            return svc
    return None


def find_area(t: str) -> str | None:
    tl = t.lower()
    for k in sorted(AREAS, key=len, reverse=True):
        if re.search(r"\b" + re.escape(k) + r"\b", tl):
            return AREAS[k]
    return None


def _next_weekday(today: date, idx: int) -> date:
    return today + timedelta(days=(idx - today.weekday()) % 7)


def find_dates(t: str, today: date) -> tuple[str, str] | None:
    tl = t.lower()
    if "this week" in tl:
        return today.isoformat(), (today + timedelta(days=6 - today.weekday())).isoformat()
    if "next week" in tl:
        start = today + timedelta(days=7 - today.weekday())
        return start.isoformat(), (start + timedelta(days=6)).isoformat()
    if "weekend" in tl:
        sat = _next_weekday(today, 5)
        return sat.isoformat(), (sat + timedelta(days=1)).isoformat()
    if "day after tomorrow" in tl:
        d = today + timedelta(days=2)
        return d.isoformat(), d.isoformat()
    if "tomorrow" in tl:
        d = today + timedelta(days=1)
        return d.isoformat(), d.isoformat()
    if re.search(r"\btoday\b", tl):
        return today.isoformat(), today.isoformat()
    m = re.search(r"\bby (" + "|".join(WEEKDAYS) + r")\b", tl)
    if m:
        return today.isoformat(), _next_weekday(today, WEEKDAYS.index(m.group(1))).isoformat()
    days = [WEEKDAYS.index(w) for w in re.findall(r"\b(" + "|".join(WEEKDAYS) + r")\b", tl)]
    if days:
        ds = sorted(_next_weekday(today, i) for i in days)
        return ds[0].isoformat(), ds[-1].isoformat()
    m = re.search(r"\b(20\d\d-\d\d-\d\d)\b", tl)
    if m:
        return m.group(1), m.group(1)
    return None


def _hhmm(h: str, ampm: str | None) -> str:
    hour = int(h)
    if ampm and ampm.startswith("p") and hour < 12:
        hour += 12
    if ampm and ampm.startswith("a") and hour == 12:
        hour = 0
    return f"{hour:02d}:00"


def find_times(t: str) -> dict:
    tl = t.lower().replace("a.m.", "am").replace("p.m.", "pm")
    out: dict = {}
    if re.search(r"\bmornings?\b", tl):
        out.update(earliest_time="08:00", latest_time="12:00")
    if re.search(r"\bafternoons?\b", tl):
        out.update(earliest_time="12:00", latest_time="17:00")
    if re.search(r"\bevenings?\b", tl):
        out.update(earliest_time="17:00", latest_time="21:00")
    for m in re.finditer(r"(not before|no earlier than|after|from|before|by)\s+(\d{1,2})(?::00)?\s*(am|pm)?", tl):
        kind, h, ap = m.groups()
        if not ap and kind in ("by",):
            continue
        val = _hhmm(h, ap or ("am" if int(h) >= 7 and int(h) <= 11 else "pm" if int(h) < 7 else None))
        if kind == "before" or kind == "by":
            out["latest_time"] = val
        else:
            out["earliest_time"] = val
    return out


def find_budget(t: str) -> dict:
    tl = t.lower().replace(",", "")
    if re.search(r"\b(no (budget|limit)|any (price|budget)|budget doesn'?t matter|whatever it costs)\b", tl):
        return {"no_budget": True}
    m = re.search(r"(?:under|below|less than|max(?:imum)?|up ?to|within|budget(?: of| is)?|upto)\s*"
                  r"(?:rs\.?|inr|₹)?\s*(\d{3,6})", tl)
    if m:
        return {"budget_max": int(m.group(1))}
    m = re.search(r"₹\s*(\d{3,6})", tl)
    if m:
        return {"budget_max": int(m.group(1))}
    return {}


def find_option(t: str) -> int | None:
    tl = t.lower()
    m = re.search(r"\boption\s*(\d)\b", tl) or re.search(r"\b#(\d)\b", tl)
    if m:
        return int(m.group(1))
    for w, n in ORDINALS.items():
        if re.search(rf"\b{w}\b( one| option)?", tl) and re.search(r"\b(go|book|take|pick|choose|want|with)\b", tl):
            return n
    if re.search(r"\b(cheapest|cheaper one)\b", tl):
        return -1
    return None


MEMORY_KEY_ALIASES = [
    (r"\b(area|locality|neighbou?rhood|where i live)\b", "area.preferred"),
    (r"\b(earliest|timing|time preference|before \d)\b", "time.earliest"),
    (r"\b(address|flat|apartment)\b", "home.address"),
    (r"\belectrician\b", "technician.electrical"),
    (r"\bplumber\b", "technician.plumbing"),
    (r"\blanguage\b", "language"),
    (r"\b(dog|pet)s?\b", "home.pets"),
]


def memory_key_for(t: str) -> str | None:
    for pat, key in MEMORY_KEY_ALIASES:
        if re.search(pat, t, re.I):
            return key
    return None
