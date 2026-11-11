#!/usr/bin/env python3
"""
GratisRadar v2 — gratis spullen ophalen om door te verkopen.

Zoekt op Marktplaats naar nieuwe advertenties die duidelijk GRATIS OPHALEN zijn,
schat wat je ervoor kunt krijgen bij doorverkoop en stuurt alleen de interessante
naar je Telegram, met een kant-en-klaar reactiebericht.

Draait gratis op GitHub Actions (zie LEESMIJ) of op je eigen computer.
Alleen standaard Python 3.9+ nodig.

    python3 gratisradar.py                 # draait continu (eigen computer)
    python3 gratisradar.py --eenmalig      # één ronde (GitHub Actions gebruikt dit)
    python3 gratisradar.py --test-melding  # stuurt een testmelding
"""

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
    NL = ZoneInfo("Europe/Amsterdam")
except Exception:                     # oudere Python: dan maar lokale tijd
    NL = None

HIER = os.path.dirname(os.path.abspath(__file__))
CONFIG_PAD = os.path.join(HIER, "config.json")
GEZIEN_PAD = os.path.join(HIER, "gezien.json")

API = "https://www.marktplaats.nl/lrp/api/search"
BASIS = "https://www.marktplaats.nl"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "nl-NL,nl;q=0.9",
    "Referer": "https://www.marktplaats.nl/",
}

GRATIS_RE = re.compile("|".join([
    r"gratis\s+(af\s*te\s*halen|ophalen|op\s*te\s*halen|mee\s*te\s*nemen|weg\s*te\s*geven|af\s*te\s*geven)",
    r"gratis\s+voor\s+(wie\s+)?(het\s+|hem\s+|ze\s+)?(ophaalt|komt\s+halen|haalt)",
    r"(af\s*te\s*halen|ophalen|op\s*te\s*halen)\s*(is\s*)?gratis",
    r"\bgratis\b.{0,40}\b(ophalen|halen|afhalen|meenemen)\b",
    r"\bmag\s+gratis\s+mee\b",
    r"\bweg\s*te\s*geven\b",
    r"\bvoor\s+(niks|niets|nop)\b",
    r"\bweggeef",
]), re.IGNORECASE)
NIET_GRATIS_RE = re.compile(
    r"(\bniet\s+gratis\b|\b2e\s+gratis\b|\btweede\s+gratis\b|gratis\s+verzend|gratis\s+bezorg|"
    r"gratis\s+thuisbezorgd|gratis\s+montage|gratis\s+proef|gratis\s+offerte|gratis\s+advies|"
    r"\bgezocht\b|\bgevraagd\b|\bruilen\b|\bte\s+koop\s+gevraagd\b)",
    re.IGNORECASE,
)
KAPOT_RE = re.compile(r"\b(kapot|defect|stuk|werkt\s+niet|start\s+niet|voor\s+onderdelen|onderdelen|"
                      r"gebarsten|gebroken|niet\s+werkend|reparatie)\b", re.IGNORECASE)

VERVOER_RANG = {"fiets": 0, "auto": 1, "bus": 2}       # wat kun jij meenemen
FORMAAT_RANG = {"klein": 0, "middel": 1, "groot": 2}    # wat heeft het item nodig
FORMAAT_LABEL = {"klein": "past op de fiets", "middel": "past in de auto", "groot": "bus/aanhanger nodig"}


def log(*a):
    print(nu().strftime("[%H:%M:%S]"), *a, flush=True)


def nu():
    return datetime.now(NL) if NL else datetime.now()


def laad_json(pad, standaard):
    try:
        with open(pad, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return standaard


def bewaar_json(pad, data):
    tmp = pad + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, pad)


def laad_config():
    cfg = laad_json(CONFIG_PAD, None)
    if cfg is None:
        sys.exit("config.json niet gevonden naast dit script.")
    # Geheimen mogen uit omgevingsvariabelen komen (GitHub Secrets)
    m = cfg.setdefault("meldingen", {})
    m["telegram_bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN") or m.get("telegram_bot_token", "")
    m["telegram_chat_id"] = os.environ.get("TELEGRAM_CHAT_ID") or m.get("telegram_chat_id", "")
    ik = cfg.setdefault("ik", {})
    if os.environ.get("POSTCODE"):
        ik["postcode"] = os.environ["POSTCODE"]
    return cfg


# ---------------------------------------------------------------- Marktplaats

def haal_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def zoek(woord, ik, aantal):
    if os.environ.get("GRATISRADAR_FIXTURE"):          # offline testen
        return laad_json(os.environ["GRATISRADAR_FIXTURE"], {}).get("listings", [])
    p = {"query": woord, "limit": aantal, "offset": 0,
         "sortBy": "SORT_INDEX", "sortOrder": "DECREASING", "viewOptions": "list-view"}
    if ik.get("postcode"):
        p["postcode"] = ik["postcode"].replace(" ", "")
        if ik.get("max_km"):
            p["distanceMeters"] = int(ik["max_km"] * 1000)
    return haal_json(API + "?" + urllib.parse.urlencode(p)).get("listings", [])


def normaliseer(l):
    prijs = l.get("priceInfo") or {}
    loc = l.get("location") or {}
    foto = (l.get("imageUrls") or [""])[0]
    if not foto and l.get("pictures"):
        p0 = l["pictures"][0]
        foto = p0.get("largeUrl") or p0.get("mediumUrl") or ""
    if foto.startswith("//"):
        foto = "https:" + foto
    vip = l.get("vipUrl") or ""
    afstand = loc.get("distanceMeters")
    return {
        "id": str(l.get("itemId") or l.get("id") or vip),
        "titel": (l.get("title") or "").strip(),
        "tekst": " ".join([l.get("description") or "", l.get("categorySpecificDescription") or ""]).strip(),
        "prijs_type": (prijs.get("priceType") or "").upper(),
        "prijs_cent": prijs.get("priceCents"),
        "plaats": loc.get("cityName") or "",
        "km": round(afstand / 1000, 1) if isinstance(afstand, (int, float)) and afstand >= 0 else None,
        "url": vip if vip.startswith("http") else BASIS + vip,
        "foto": foto,
        "verkoper_type": ((l.get("sellerInformation") or {}).get("sellerType") or "").upper(),
    }


# ---------------------------------------------------------------- beoordelen

def woord_re(w):
    return re.compile(r"\b" + re.escape(w.lower()) + r"(s|en|den|ten|ken|je|jes|tje|tjes)?\b", re.IGNORECASE)


def bevat(tekst, woorden):
    for w in woorden:
        if woord_re(w).search(tekst):
            return w
    return None


def gratis_score(a):
    """0 = niet gratis, 1 = staat op gratis maar vaag, 2 = duidelijk gratis ophalen."""
    tekst = f"{a['titel']} {a['tekst']}"
    if NIET_GRATIS_RE.search(tekst):
        return 0
    prijs_gratis = a["prijs_type"] == "FREE" or (a["prijs_cent"] == 0 and a["prijs_type"] in ("FIXED", ""))
    tekst_gratis = bool(GRATIS_RE.search(tekst))
    if tekst_gratis and (prijs_gratis or a["prijs_type"] in ("SEE_DESCRIPTION", "NOTK")):
        return 2
    if prijs_gratis:
        return 1
    return 0


def categorie_van(a, cats):
    """Eerste categorie waarvan een woord in titel/tekst staat (titel telt het zwaarst)."""
    for veld in (a["titel"], f"{a['titel']} {a['tekst']}"):
        for c in cats:
            if bevat(veld, c.get("woorden", [])):
                return c
    return None


def beoordeel(a, cfg):
    """Geeft (doorlaten?, reden, info)."""
    ik = cfg.get("ik", {})
    f = cfg.get("filters", {})
    cats = [c for c in cfg.get("categorieen", []) if c.get("aan", True)]
    tekst = f"{a['titel']} {a['tekst']}"

    s = gratis_score(a)
    if s == 0:
        return False, "niet echt gratis", {}
    if f.get("alleen_duidelijk_gratis_ophalen", True) and s < 2:
        return False, "niet duidelijk 'gratis ophalen'", {}

    w = bevat(tekst, f.get("nooit_tonen", []))
    if w:
        return False, f"bevat '{w}'", {}

    if ik.get("max_km") and a["km"] is not None and a["km"] > ik["max_km"]:
        return False, f"{a['km']} km te ver", {}

    if f.get("alleen_particulieren", True) and a["verkoper_type"] in ("TRADER", "BUSINESS", "PRO"):
        return False, "bedrijf", {}

    cat = categorie_van(a, cats)
    if cat is None and not f.get("ook_overige_spullen", False):
        return False, "geen van je categorieën", {}

    kapot = bool(KAPOT_RE.search(tekst))
    if kapot and not f.get("kapot_of_onderdelen_ok", True):
        return False, "kapot/onderdelen", {}

    lo, hi = (cat or {}).get("waarde", [5, 30])
    if kapot:
        lo, hi = round(lo * 0.3), round(hi * 0.5)
    if hi < f.get("min_doorverkoopwaarde", 0):
        return False, f"te weinig waard (≈ €{lo}–{hi})", {}

    formaat = (cat or {}).get("formaat", "middel")
    if bevat(tekst, cfg.get("altijd_groot", [])):
        formaat = "groot"
    vervoer = ik.get("vervoer", "auto")
    if FORMAAT_RANG[formaat] > VERVOER_RANG.get(vervoer, 1):
        return False, f"{FORMAAT_LABEL[formaat]}", {}

    return True, "", {"cat": (cat or {}).get("naam", "Overig"), "lo": lo, "hi": hi,
                      "kapot": kapot, "formaat": formaat}


# ---------------------------------------------------------------- meldingen

def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def stil_nu(cfg):
    st = cfg.get("meldingen", {}).get("stille_uren")
    if not st:
        return False
    van, tot = st
    h = nu().hour
    return (van <= h < tot) if van < tot else (h >= van or h < tot)


def reactie_tekst(a, cfg):
    sjabloon = cfg.get("meldingen", {}).get(
        "reactie_sjabloon", "Hoi! Is {wat} nog beschikbaar? Ik kan vandaag nog ophalen. Groetjes, {naam}")
    wat = a["titel"].split(" - ")[0].split(",")[0].strip()[:40]
    return sjabloon.format(wat=wat.lower() if wat.isupper() else wat,
                           naam=cfg.get("ik", {}).get("naam", ""), plaats=a["plaats"]).strip()


def stuur_telegram(cfg, tekst, knop_url=None, stil=False, foto=None):
    m = cfg.get("meldingen", {})
    token, chat = m.get("telegram_bot_token"), m.get("telegram_chat_id")
    if not token or not chat or "PLAK" in token:
        log("(geen Telegram ingesteld, melding alleen in log)")
        return
    data = {"chat_id": chat, "parse_mode": "HTML", "disable_notification": "true" if stil else "false"}
    if knop_url:
        data["reply_markup"] = json.dumps({"inline_keyboard": [[{"text": "📲 Open advertentie", "url": knop_url}]]})
    if foto:
        methode, data["photo"], data["caption"] = "sendPhoto", foto, tekst[:1020]
    else:
        methode, data["text"] = "sendMessage", tekst
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{methode}",
                                 data=urllib.parse.urlencode(data).encode(), method="POST")
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        if foto:                                   # foto mislukt? dan zonder foto
            return stuur_telegram(cfg, tekst, knop_url, stil)
        log("telegram-fout:", e)


def meld(a, info, cfg):
    km = f" · {a['km']} km" if a["km"] is not None else ""
    waarde = f"≈ €{info['lo']}–{info['hi']}"
    regels = [
        f"🎁 <b>GRATIS</b> · {waarde} doorverkoop",
        f"<b>{esc(a['titel'][:100])}</b>",
        f"📍 {esc(a['plaats'])}{km} · {FORMAAT_LABEL[info['formaat']]}",
        f"🏷 {esc(info['cat'])}" + (" · ⚠️ kapot/onderdelen" if info["kapot"] else ""),
        "",
        "Reageer snel, tik om te kopiëren:",
        f"<code>{esc(reactie_tekst(a, cfg))}</code>",
    ]
    stuur_telegram(cfg, "\n".join(regels), a["url"], stil=stil_nu(cfg),
                   foto=a["foto"] if cfg.get("meldingen", {}).get("met_foto", True) else None)
    log("✅", a["titel"], "|", waarde, "|", a["url"])


# ---------------------------------------------------------------- hoofdlus

def zoekwoorden(cfg):
    woorden = list(cfg.get("extra_zoekwoorden", ["gratis ophalen", "gratis af te halen", "weg te geven"]))
    for c in cfg.get("categorieen", []):
        if c.get("aan", True):
            woorden += c.get("zoek", [])
    gezien, uniek = set(), []
    for w in woorden:
        if w.lower() not in gezien:
            gezien.add(w.lower())
            uniek.append(w)
    return uniek


def ronde(cfg, gezien, eerste_keer):
    nieuw = 0
    ik = cfg.get("ik", {})
    for woord in zoekwoorden(cfg):
        try:
            ruwe = zoek(woord, ik, cfg.get("resultaten_per_zoekwoord", 40))
        except Exception as e:
            log(f"⚠️  zoeken mislukt voor '{woord}':", e)
            continue
        for l in ruwe:
            a = normaliseer(l)
            if a["id"] in gezien:
                continue
            gezien[a["id"]] = int(time.time())
            ok, reden, info = beoordeel(a, cfg)
            if not ok:
                continue
            if eerste_keer:
                continue            # startronde: alleen onthouden, geen spam met oude advertenties
            meld(a, info, cfg)
            nieuw += 1
        time.sleep(random.uniform(1.0, 2.5))
    grens = time.time() - 10 * 86400
    for k in [k for k, t in gezien.items() if t < grens]:
        del gezien[k]
    bewaar_json(GEZIEN_PAD, gezien)
    return nieuw


def main():
    ap = argparse.ArgumentParser(description="GratisRadar")
    ap.add_argument("--eenmalig", action="store_true")
    ap.add_argument("--test-melding", action="store_true")
    args = ap.parse_args()
    cfg = laad_config()

    if args.test_melding:
        a = {"titel": "iPhone 11 met gebarsten scherm (TEST)", "plaats": "Haarlem", "km": 12.4,
             "url": BASIS, "foto": ""}
        meld(a, {"cat": "Telefoons & tablets", "lo": 40, "hi": 125, "kapot": True, "formaat": "klein"}, cfg)
        print("Testmelding verstuurd. Zie je hem niet in Telegram? Check token, chat-ID en of je de bot /start hebt gestuurd.")
        return

    gezien = laad_json(GEZIEN_PAD, {})
    eerste = len(gezien) == 0
    interval = max(60, int(cfg.get("interval_seconden", 120)))
    log(f"GratisRadar gestart · {len(zoekwoorden(cfg))} zoekwoorden")
    while True:
        n = ronde(cfg, gezien, eerste)
        log(f"ronde klaar · {n} melding(en)" + (" (startronde: alleen onthouden)" if eerste else ""))
        eerste = False
        if args.eenmalig:
            break
        time.sleep(interval + random.uniform(-15, 15))
        cfg = laad_config()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("gestopt")
