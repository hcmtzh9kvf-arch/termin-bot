#!/usr/bin/env python3
"""
Ausländeramt Aachen (StädteRegion) – Termin-Wächter

Folgt demselben Weg wie im Browser:
  Funktionseinheit -> Anliegen (z. B. "Infostelle | Beratungs- und Antragsservice")
  -> Standortauswahl ("Nächster Termin ab ...") -> Terminvorschläge
und schickt eine Push-Benachrichtigung (ntfy und/oder Telegram), sobald ein
Termin VOR dem Datum ALERT_BEFORE frei ist.

Es wird NICHTS gebucht – nur geprüft und benachrichtigt.

Umgebungsvariablen:
  TARGETS        Eine Zeile pro Anliegen: "Kategorie | Anliegen" (Anliegen darf auch
                 eine Zahl sein = n-tes Anliegen der Kategorie). Zeilen mit # werden ignoriert.
  ALERT_BEFORE   Benachrichtigen, wenn ein Termin vor diesem Datum frei ist (TT.MM.JJJJ).
  NTFY_TOPIC     ntfy-Topic (App "ntfy" auf dem iPhone/iPad abonnieren).
  NTFY_SERVER    Standard: https://ntfy.sh
  TG_TOKEN, TG_CHAT_ID   Optional: Telegram-Bot statt/zusätzlich zu ntfy.
  HEARTBEAT_HOUR Uhrzeit (Berlin) für die tägliche "Bot läuft"-Nachricht. Leer = aus.
  STATE_FILE     Standard: state.json

Aufruf:
  python termin_bot.py                # einmal prüfen (für GitHub Actions)
  python termin_bot.py --list         # alle Kategorien/Anliegen anzeigen
  python termin_bot.py --test-notify  # Test-Benachrichtigung senden
  python termin_bot.py --loop 300     # lokal alle 300 s prüfen
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlencode, urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag

BASE = os.getenv("TERMIN_BASE", "https://termine.staedteregion-aachen.de/auslaenderamt/")
TZ = ZoneInfo("Europe/Berlin")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
TIMEOUT = 30
DEFAULT_TARGETS = "Infostelle | Beratungs- und Antragsservice"
DEFAULT_ALERT_BEFORE = "03.03.2027"
ERROR_ALERT_AFTER = 3  # so viele Fehlversuche hintereinander, bevor gewarnt wird

DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s*,?\s*(?:um\s*)?(\d{1,2}):(\d{2}))?")
TIME_RE = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
HEADING_TAGS = ["h1", "h2", "h3", "h4", "h5", "h6", "summary", "button", "legend", "a"]


# --------------------------------------------------------------------------- helpers
def norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip().lower()


def parse_day(s: str) -> date:
    s = s.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise SystemExit(f"ALERT_BEFORE hat ein ungültiges Datum: {s!r} (erwartet TT.MM.JJJJ)")


@dataclass(order=True)
class Slot:
    day: date
    time: str = ""  # "HH:MM" oder ""

    def __str__(self) -> str:
        return self.day.strftime("%d.%m.%Y") + (f" {self.time}" if self.time else "")


def slots_in_text(text: str) -> list[Slot]:
    out = []
    for d, m, y, hh, mm in DATE_RE.findall(text or ""):
        try:
            day = date(int(y), int(m), int(d))
        except ValueError:
            continue
        out.append(Slot(day, f"{int(hh):02d}:{mm}" if hh else ""))
    return out


def earliest(slots: list[Slot]) -> Slot | None:
    today = datetime.now(TZ).date()
    valid = [s for s in slots if s.day >= today]
    if not valid:
        return None
    first_day = min(s.day for s in valid)
    times = sorted(s.time for s in valid if s.day == first_day and s.time)
    return Slot(first_day, times[0] if times else "")


# --------------------------------------------------------------------------- page parsing
CNC_NAME = re.compile(r"^cnc-\d+$")


@dataclass
class Concern:
    cnc_id: str
    label: str
    el: Tag    # das Eingabefeld (bzw. <li> im alten Layout)
    box: Tag   # größtes Elternelement, das nur dieses eine Anliegen enthält


def item_box(el: Tag) -> Tag:
    box = el
    while box.parent is not None and box.parent.name not in ("form", "body", "[document]"):
        if len(box.parent.find_all(attrs={"name": CNC_NAME})) > 1:
            break
        box = box.parent
    return box


def concern_items(soup: BeautifulSoup) -> list[Concern]:
    """Alle wählbaren Anliegen (cnc-<id>) in Dokumentreihenfolge."""
    items: list[Concern] = []
    seen: set[str] = set()
    for inp in soup.find_all(attrs={"name": CNC_NAME}):
        cid = inp["name"].split("-")[-1]
        if cid in seen:
            continue
        seen.add(cid)
        box = item_box(inp)
        label = ""
        if inp.get("id"):
            lab = soup.find("label", attrs={"for": inp["id"]})
            if lab:
                label = lab.get_text(" ", strip=True)
        items.append(Concern(cid, label or box.get_text(" ", strip=True), inp, box))
    if items:
        return items
    # Fallback (älteres Layout): <li id="...-<id>">
    for li in soup.find_all("li", id=re.compile(r"-\d+$")):
        cid = li["id"].split("-")[-1]
        if cid not in seen:
            seen.add(cid)
            items.append(Concern(cid, li.get_text(" ", strip=True), li, li))
    return items


def sections(soup: BeautifulSoup, items: list[Concern]) -> dict[str, list[Concern]]:
    """Ordnet jedes Anliegen der davorstehenden Überschrift (Kategorie) zu."""
    order = {id(el): i for i, el in enumerate(soup.find_all(True))}
    box_ids = {id(c.box) for c in items}
    heads = []
    for h in soup.find_all(HEADING_TAGS):
        txt = h.get_text(" ", strip=True)
        if not txt or len(txt) > 100:
            continue
        # Überschriften innerhalb eines Anliegens (Hilfe-, +/−-Buttons) ignorieren
        if id(h) in box_ids or any(id(p) in box_ids for p in h.parents):
            continue
        if h.find(attrs={"name": CNC_NAME}):
            continue
        heads.append((order[id(h)], txt))
    result: dict[str, list[Concern]] = {}
    for c in items:
        pos = order.get(id(c.box), -1)
        before = [t for p, t in heads if p < pos]
        result.setdefault(before[-1] if before else "", []).append(c)
    return result


def pick_concern(soup: BeautifulSoup, category: str | None, concern: str) -> Concern:
    items = concern_items(soup)
    if not items:
        raise RuntimeError("Auf der Anliegen-Seite wurden keine Anliegen gefunden (Layout geändert?).")
    secs = sections(soup, items)
    pool = items
    if category:
        matching = [k for k in secs if norm(category) in norm(k)]
        if not matching:
            raise RuntimeError(f"Kategorie '{category}' nicht gefunden. Vorhanden: "
                               + "; ".join(k or "(ohne)" for k in secs))
        pool = [c for k in matching for c in secs[k]]
    if concern.isdigit():
        idx = int(concern) - 1
        if not 0 <= idx < len(pool):
            raise RuntimeError(f"Anliegen #{concern} existiert nicht (nur {len(pool)} vorhanden).")
        return pool[idx]
    hits = [c for c in pool if norm(concern) in norm(c.label)]
    if not hits:
        raise RuntimeError(f"Anliegen '{concern}' nicht gefunden. Vorhanden: "
                           + "; ".join(c.label[:60] for c in pool))
    return min(hits, key=lambda c: len(c.label))


def location_url(soup: BeautifulSoup, page_url: str, html: str, c: Concern) -> str:
    form = c.el.find_parent("form")
    params: dict[str, str] = {}
    action = "location"
    if form:
        action = form.get("action") or action
        for inp in form.find_all("input", attrs={"type": "hidden"}):
            if inp.get("name"):
                params[inp["name"]] = inp.get("value", "")
    if "mdt" not in params:
        m = re.search(r"mdt=(\d+)", html)
        params["mdt"] = m.group(1) if m else "89"
    params.setdefault("select_cnc", "1")
    params[f"cnc-{c.cnc_id}"] = "1"
    return urljoin(page_url, action.split("?")[0]) + "?" + urlencode(params)


@dataclass
class Location:
    name: str
    next_slot: Slot | None
    form: Tag | None


def next_termin(text: str) -> Slot | None:
    m = re.search(r"nächster\s+termin\s*:?\s*(?:ab\s*)?(.{0,40})", text, re.I)
    if not m:
        return None
    s = slots_in_text(m.group(1))
    return s[0] if s else None


def parse_locations(soup: BeautifulSoup) -> list[Location]:
    page_text = soup.get_text(" ", strip=True)
    locs = []
    forms = [i.find_parent("form") for i in soup.find_all("input", attrs={"name": "loc"})]
    forms = [f for i, f in enumerate(forms) if f is not None and f not in forms[:i]]
    for f in forms:
        block = f
        # Den Bereich mit den Standortdetails finden (Form selbst oder ein Elternelement)
        for _ in range(4):
            if "nächster termin" in norm(block.get_text(" ")) or block.parent is None:
                break
            block = block.parent
        text = block.get_text(" ", strip=True)
        if len(forms) == 1 and "nächster termin" not in norm(text):
            text = page_text
        m = re.search(r"Name\s+(.+?)\s+Anschrift", text)
        name = m.group(1) if m else ""
        sub = f.find(attrs={"name": "select_location"})
        if not name and sub is not None:
            name = re.sub(r"\s*auswählen\s*$", "", sub.get("value") or sub.get_text(" ", strip=True))
        locs.append(Location(name.strip() or "Standort", next_termin(text), f))
    if not locs and "nächster termin" in norm(page_text):
        locs.append(Location("Standort", next_termin(page_text), None))
    return locs


def form_data(form: Tag, loc_name: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for inp in form.find_all(["input", "select", "textarea"]):
        name = inp.get("name")
        typ = (inp.get("type") or "").lower()
        if not name or typ in ("submit", "button", "image", "reset"):
            continue
        if typ in ("checkbox", "radio") and not inp.has_attr("checked"):
            continue
        data[name] = inp.get("value", "")
    sub = form.find(attrs={"name": "select_location"})
    if sub is not None:
        data["select_location"] = sub.get("value") or sub.get_text(" ", strip=True)
    else:
        named = [s for s in form.find_all(["input", "button"])
                 if (s.get("type") or "").lower() == "submit" and s.get("name")]
        if named:
            data[named[0]["name"]] = named[0].get("value", "")
        else:
            data["select_location"] = f"{loc_name} auswählen"
    if not data.get("gps_lat"):
        data["gps_lat"], data["gps_long"] = "50.7753", "6.0839"
    return data


def parse_suggest(html: str) -> tuple[bool, list[Slot]]:
    """(keine_termine, slots)"""
    if "kein freier termin" in norm(html):
        return True, []
    soup = BeautifulSoup(html, "html.parser")
    slots: list[Slot] = []
    acc = soup.find(id="sugg_accordion")
    if acc:
        for h in acc.find_all(["h3", "h4", "summary"]):
            ds = slots_in_text(h.get_text(" ", strip=True))
            if not ds:
                continue
            panel = h.find_next_sibling()
            times = sorted(f"{int(a):02d}:{b}" for a, b in TIME_RE.findall(
                panel.get_text(" ", strip=True) if panel else ""))
            slots.append(Slot(ds[0].day, ds[0].time or (times[0] if times else "")))
    summ = soup.find(id="suggest_details_summary")
    if summ:
        slots += slots_in_text(summ.get_text(" ", strip=True))
    if not slots:
        # Fallback: nur "Datum, Uhrzeit"-Angaben zählen (keine Footer-/Stand-Daten)
        main = soup.find("main") or soup.body or soup
        slots = [x for x in slots_in_text(main.get_text(" ", strip=True)) if x.time]
    return False, slots


# --------------------------------------------------------------------------- check
@dataclass
class Result:
    label: str
    location: str
    slot: Slot | None
    url: str


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9"})
    return s


def get(s: requests.Session, url: str) -> requests.Response:
    r = s.get(url, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def open_concern_page(s: requests.Session) -> requests.Response:
    r0 = get(s, BASE)
    m = re.search(r"select2\?md=(\d+)", r0.text)
    return get(s, urljoin(BASE, f"select2?md={m.group(1) if m else 1}"))


def check_target(s: requests.Session, category: str | None, concern: str, label: str) -> list[Result]:
    r1 = open_concern_page(s)
    soup1 = BeautifulSoup(r1.text, "html.parser")
    c = pick_concern(soup1, category, concern)
    loc_url = location_url(soup1, r1.url, r1.text, c)
    r2 = get(s, loc_url)
    soup2 = BeautifulSoup(r2.text, "html.parser")
    locs = parse_locations(soup2)
    if not locs:
        if "kein freier termin" in norm(r2.text):
            return [Result(label, "", None, loc_url)]
        raise RuntimeError("Keine Standorte auf der Standort-Seite gefunden (Layout geändert?).")
    results = []
    for i, loc in enumerate(locs):
        candidates = [loc.next_slot] if loc.next_slot else []
        if loc.form is not None:
            try:
                if i > 0:  # Sitzung für den nächsten Standort zurücksetzen
                    get(s, loc_url)
                action = urljoin(r2.url, loc.form.get("action") or r2.url)
                method = (loc.form.get("method") or "post").lower()
                data = form_data(loc.form, loc.name)
                r3 = (s.post(action, data=data, timeout=TIMEOUT) if method == "post"
                      else s.get(action, params=data, timeout=TIMEOUT))
                r3.raise_for_status()
                r4 = r3 if "suggest" in r3.url else get(s, urljoin(BASE, "suggest"))
                candidates += parse_suggest(r4.text)[1]
            except requests.RequestException as e:
                if not candidates:
                    raise
                print(f"  (Terminvorschläge nicht ladbar, nutze 'Nächster Termin': {e})")
        results.append(Result(label, loc.name, earliest(candidates), loc_url))
    return results


# --------------------------------------------------------------------------- notify
def notify(title: str, message: str, priority: int = 3, tags: list[str] | None = None,
           click: str = BASE) -> bool:
    sent = False
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if topic:
        server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        try:
            r = requests.post(server, json={"topic": topic, "title": title, "message": message,
                                            "priority": priority, "tags": tags or [], "click": click},
                              timeout=TIMEOUT)
            r.raise_for_status()
            sent = True
        except requests.RequestException as e:
            print(f"ntfy-Fehler: {e}", file=sys.stderr)
    token, chat = os.getenv("TG_TOKEN", "").strip(), os.getenv("TG_CHAT_ID", "").strip()
    if token and chat:
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat, "text": f"{title}\n\n{message}\n{click}",
                                    "disable_notification": priority <= 2},
                              timeout=TIMEOUT)
            r.raise_for_status()
            sent = True
        except requests.RequestException as e:
            print(f"Telegram-Fehler: {e}", file=sys.stderr)
    if not sent:
        print(f"[keine Benachrichtigung konfiguriert] {title}: {message}")
    return sent


# --------------------------------------------------------------------------- state / main
def parse_targets(raw: str) -> list[tuple[str | None, str, str]]:
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cat, _, con = line.rpartition("|")
        out.append((cat.strip() or None, con.strip(), line))
    return out


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def run_once(state: dict) -> int:
    targets = parse_targets(os.getenv("TARGETS") or DEFAULT_TARGETS)
    limit = parse_day(os.getenv("ALERT_BEFORE") or DEFAULT_ALERT_BEFORE)
    now = datetime.now(TZ)
    print(f"{now:%d.%m.%Y %H:%M} – Alarm für Termine vor {limit:%d.%m.%Y}")

    alerted: dict = state.setdefault("alerted", {})
    results: list[Result] = []
    errors: list[str] = []
    for cat, con, label in targets:
        try:
            res = check_target(new_session(), cat, con, label)
            results += res
            for r in res:
                print(f"  {label} @ {r.location or '-'}: frühester Termin = {r.slot or 'keiner'}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{label}: {e}")
            print(f"  FEHLER {label}: {e}", file=sys.stderr)

    seen_keys = set()
    for r in results:
        key = f"{r.label} @ {r.location}"
        seen_keys.add(key)
        if r.slot and r.slot.day < limit:
            if alerted.get(key) != str(r.slot):
                notify("🎉 Daha erken randevu var!",
                       f"{r.slot} – {r.location}\n({r.label})\nHemen rezervasyon yap! "
                       f"Şu anki eşik: {limit:%d.%m.%Y}",
                       priority=5, tags=["rotating_light"])
                alerted[key] = str(r.slot)
        else:
            alerted.pop(key, None)
    for k in [k for k in alerted if k not in seen_keys and not errors]:
        alerted.pop(k)

    if errors:
        streak = min(state.get("error_streak", 0) + 1, ERROR_ALERT_AFTER)
        state["error_streak"] = streak
        if streak >= ERROR_ALERT_AFTER and not state.get("error_notified"):
            notify("⚠️ Termin botu hata veriyor", "\n".join(errors)[:900], priority=3,
                   tags=["warning"])
            state["error_notified"] = True
    else:
        if state.get("error_notified"):
            notify("✅ Termin botu tekrar çalışıyor", "Kontroller yine başarılı.", priority=2)
        state["error_streak"], state["error_notified"] = 0, False

    hb = os.getenv("HEARTBEAT_HOUR", "9").strip()
    today = now.date().isoformat()
    if state.get("heartbeat") != today and (not hb or now.hour >= int(hb)):
        if hb:
            best = ", ".join(f"{r.slot or 'yok'} ({r.location or r.label})" for r in results) or "-"
            notify("🤖 Termin botu çalışıyor", f"Şu an en erken: {best}", priority=2)
        state["heartbeat"] = today  # tägliche Änderung hält das Repo "aktiv"

    return 1 if errors and not results else 0


def list_concerns() -> None:
    s = new_session()
    r1 = open_concern_page(s)
    soup = BeautifulSoup(r1.text, "html.parser")
    for cat, items in sections(soup, concern_items(soup)).items():
        print(f"\n[{cat or 'ohne Kategorie'}]")
        for i, c in enumerate(items, 1):
            print(f"  {i}. {c.label}  (cnc-{c.cnc_id})")


def main() -> int:
    ap = argparse.ArgumentParser(description="Ausländeramt Aachen Termin-Wächter")
    ap.add_argument("--list", action="store_true", help="Kategorien und Anliegen anzeigen")
    ap.add_argument("--test-notify", action="store_true", help="Test-Benachrichtigung senden")
    ap.add_argument("--loop", type=int, metavar="SEK", help="lokal endlos prüfen (min. 120 s)")
    a = ap.parse_args()

    if a.list:
        list_concerns()
        return 0
    if a.test_notify:
        ok = notify("✅ Test", "Termin botu bildirimleri çalışıyor.", priority=4)
        return 0 if ok else 1

    path = Path(os.getenv("STATE_FILE", "state.json"))
    while True:
        state = load_state(path)
        try:
            code = run_once(state)
        finally:
            save_state(path, state)
        if not a.loop:
            return code
        time.sleep(max(120, a.loop) + random.randint(0, 30))


if __name__ == "__main__":
    sys.exit(main())
