#!/usr/bin/env python3
"""
Prospecting + personalized outreach engine (human-in-the-loop). Any city, any country,
many business segments. Run the dashboard with:  streamlit run app.py

CLI:
  python outreach.py go "Lagos, Nigeria" restaurants 40
  python outreach.py source "Paris, France" restaurants 40
  python outreach.py generate
  python outreach.py today | sent <id> | mark <id> <status> | stats | segments
"""
import csv, json, os, re, sys, time, datetime as dt
from pathlib import Path
from urllib.parse import quote, urlparse
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from groq import Groq

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")
(BASE / "data").mkdir(exist_ok=True)
(BASE / "offers").mkdir(exist_ok=True)

MODEL = os.getenv("MODEL", "llama-3.3-70b-versatile")
DB = BASE / "data" / "outreach.csv"
PROSPECTS = BASE / "data" / "prospects.csv"
OFFER_FILE = BASE / "offer.txt"
OFFERS_DIR = BASE / "offers"
DAILY_CAP = int(os.getenv("DAILY_CAP", "15"))
SENDER = os.getenv("SENDER_NAME", "Krish")
FOLLOWUP_DAYS = (3, 8)

SOURCE_FIELDS = ["firm_name", "website", "contact_name", "contact_role", "email", "phone",
                 "country", "address", "segment", "info"]
FIELDS = ["id", "email", "phone", "firm_name", "segment", "contact_name", "contact_role",
          "country", "website", "info", "fit_score", "hook", "subject", "email_body",
          "whatsapp_msg", "followup1", "followup2", "mailto_link", "whatsapp_link", "status",
          "sent_date", "followups_sent", "next_action"]

UA = {"User-Agent": "OutreachApp/1.0 (small-business prospect research; personal use)"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BAD_EMAIL = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", "sentry", "wixpress",
             "example.", "yourdomain", "domain.com")

# ------------------------------------------------------------------ OFFERS (per segment)
DEFAULT_OFFER = """AI automation that removes manual admin work for small businesses
(receipts, invoices, customer follow-up, reporting). Fixed-price pilot: USD 300, one month,
results or no charge for the second month. (Edit this for each segment in the app.)"""


def slug(text):
    return re.sub(r"\W+", "-", (text or "").lower()).strip("-") or "general"


def get_offer(segment=""):
    f = OFFERS_DIR / f"{slug(segment)}.txt"
    if segment and f.exists():
        return f.read_text(encoding="utf-8").strip()
    if OFFER_FILE.exists():
        return OFFER_FILE.read_text(encoding="utf-8").strip()
    return DEFAULT_OFFER


def has_segment_offer(segment):
    return (OFFERS_DIR / f"{slug(segment)}.txt").exists()


def save_offer(text, segment=""):
    f = OFFERS_DIR / f"{slug(segment)}.txt" if segment else OFFER_FILE
    f.write_text(text.strip(), encoding="utf-8")


# ------------------------------------------------------------------ SEGMENTS
# name: (keywords, [(osm_key, "value1|value2")])
SEGMENTS = {
    "restaurants": (["restaurant", "food", "eatery", "eateries", "cafe", "cafes", "fast food", "dining", "canteen"],
                    [("amenity", "restaurant|fast_food|cafe|food_court")]),
    "accountants": (["accountant", "accounting", "bookkeeping", "bookkeeper", "tax"],
                    [("office", "accountant|tax_advisor")]),
    "hotels": (["hotel", "guest house", "guesthouse", "hostel", "lodging", "motel"],
               [("tourism", "hotel|guest_house|hostel|motel")]),
    "pharmacies": (["pharmacy", "chemist"], [("amenity", "pharmacy")]),
    "clinics": (["clinic", "dentist", "dental", "doctor", "hospital", "medical"],
                [("amenity", "clinic|dentist|doctors|hospital")]),
    "salons": (["salon", "barber", "beauty", "hair"], [("shop", "hairdresser|beauty")]),
    "supermarkets": (["supermarket", "grocery", "groceries", "provisions", "convenience"],
                     [("shop", "supermarket|convenience|greengrocer")]),
    "retail": (["retail", "shop", "boutique", "clothes", "fashion", "clothing"],
               [("shop", "clothes|boutique|shoes|fashion_accessories")]),
    "electronics": (["electronics", "phone shop", "gadgets", "computer"],
                    [("shop", "electronics|mobile_phone|computer")]),
    "schools": (["school", "college", "academy", "training", "kindergarten"],
                [("amenity", "school|college|kindergarten|driving_school")]),
    "law": (["lawyer", "law firm", "attorney", "legal", "law"], [("office", "lawyer")]),
    "real estate": (["estate agent", "property", "realtor", "real estate"], [("office", "estate_agent")]),
    "gyms": (["gym", "fitness"], [("leisure", "fitness_centre")]),
    "auto": (["car repair", "mechanic", "garage", "car dealer", "auto"], [("shop", "car_repair|car")]),
    "logistics": (["logistics", "courier", "freight", "shipping"], [("office", "logistics|courier|freight_forwarder")]),
    "construction": (["construction", "builder", "contractor", "architect", "engineering"],
                     [("office", "construction_company|architect|engineer")]),
    "insurance": (["insurance"], [("office", "insurance")]),
    "travel agencies": (["travel agency", "travel", "tours"], [("shop", "travel_agency"), ("office", "travel_agent")]),
    "bakeries": (["bakery", "baker"], [("shop", "bakery")]),
    "hardware": (["hardware", "building materials"], [("shop", "hardware|doityourself|trade")]),
}


def resolve_segment(text):
    """Return (segment_name, osm_clauses). Supports 'tag:amenity=bar|pub' for anything else."""
    t = (text or "").strip().lower()
    m = re.match(r"tag:(\w+)=([\w|]+)$", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}", [(m.group(1), m.group(2))]
    for name, (kws, clauses) in SEGMENTS.items():
        if t == name or t.rstrip("s") == name.rstrip("s") or t in kws:
            return name, clauses
    for name, (kws, clauses) in SEGMENTS.items():
        if any(k in t for k in kws):
            return name, clauses
    raise ValueError(f"Unknown segment '{text}'. Available: {', '.join(SEGMENTS)}. "
                     "Or use a raw OpenStreetMap tag, e.g. tag:amenity=bar")


# ------------------------------------------------------------------ COUNTRY / PHONE / LANGUAGE
DIAL = {"NG": "234", "GH": "233", "KE": "254", "ZA": "27", "EG": "20", "MA": "212", "TZ": "255",
        "UG": "256", "RW": "250", "ET": "251", "SN": "221", "CI": "225", "CM": "237", "ZM": "260",
        "ZW": "263", "BW": "267", "NA": "264", "MU": "230", "DZ": "213", "TN": "216", "AO": "244",
        "US": "1", "CA": "1", "GB": "44", "IE": "353", "QA": "974", "AE": "971", "SA": "966",
        "KW": "965", "BH": "973", "OM": "968", "FR": "33", "DE": "49", "ES": "34", "IT": "39",
        "NL": "31", "PT": "351", "IN": "91", "AU": "61", "NZ": "64", "SG": "65", "MY": "60"}
LANG = {"FR": "French", "SN": "French", "CI": "French", "CM": "French", "DZ": "French",
        "TN": "French", "MA": "French", "DE": "German", "ES": "Spanish", "PT": "Portuguese",
        "AO": "Portuguese", "IT": "Italian"}


def normalize_phone(phone, cc):
    """Return +<international digits> for WhatsApp, or '' if unusable."""
    if not phone:
        return ""
    raw = phone.strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("+"):
        return "+" + digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    dial = DIAL.get((cc or "").upper())
    if not dial:
        return "+" + digits if len(digits) >= 10 else ""
    if digits.startswith("0"):
        return "+" + dial + digits.lstrip("0")
    if digits.startswith(dial) and len(digits) >= 10:
        return "+" + digits
    return "+" + dial + digits


def wa_link(phone, text):
    digits = re.sub(r"\D", "", phone or "")
    return f"https://wa.me/{digits}?text={quote(text)}" if text and len(digits) >= 8 else ""


def mailto(email, subject, body):
    return f"mailto:{email}?subject={quote(subject)}&body={quote(body)}" if email else ""


def domain_of(url):
    if not url:
        return ""
    if not url.startswith("http"):
        url = "https://" + url
    return urlparse(url).netloc.lower().removeprefix("www.")


# ------------------------------------------------------------------ SOURCING
OVERPASS_MIRRORS = ["https://overpass-api.de/api/interpreter",
                    "https://overpass.kumi.systems/api/interpreter",
                    "https://overpass.private.coffee/api/interpreter"]


def http_json(method, urls, retries=2, timeout=40, **kw):
    """Try each URL (mirror) with retries; only accept real JSON responses."""
    last = "no response"
    for attempt in range(retries):
        for url in urls:
            try:
                r = requests.request(method, url, headers=UA, timeout=timeout, **kw)
                if r.status_code == 200:
                    try:
                        return r.json()
                    except ValueError:
                        last = f"{url} returned non-JSON (server busy): {r.text[:120]!r}"
                else:
                    last = f"{url} returned HTTP {r.status_code}"
            except requests.RequestException as e:
                last = f"{url} failed: {e}"
        time.sleep(3)
    raise RuntimeError("Map data servers are busy or unreachable. Wait a minute and try again. "
                       f"Last error: {last}")


def cmd_source(place, segment, country="", limit=40):
    """Find businesses of a segment in a place via OpenStreetMap (free, no API key)."""
    seg_name, clauses = resolve_segment(segment)
    g = http_json("GET", ["https://nominatim.openstreetmap.org/search"],
                  params={"q": place, "format": "json", "limit": 1, "addressdetails": 1})
    if not g:
        raise RuntimeError(f"Could not find '{place}'. Try 'Lagos, Nigeria'.")
    s, n, w, e = g[0]["boundingbox"]
    cc = (country or g[0].get("address", {}).get("country_code", "")).upper()
    body = "".join(f'nwr["{k}"~"^({v})$"]({s},{w},{n},{e});' for k, v in clauses)
    q = f"[out:json][timeout:30];({body});out tags center 400;"
    els = http_json("POST", OVERPASS_MIRRORS, data={"data": q}).get("elements", [])
    have = set()
    if PROSPECTS.exists():
        with open(PROSPECTS, newline="", encoding="utf-8") as f:
            have = {(r["firm_name"].lower(), domain_of(r["website"])) for r in csv.DictReader(f)}
    new = []
    for el in els:
        t = el.get("tags", {})
        name = t.get("name")
        site = t.get("website") or t.get("contact:website") or ""
        phone = t.get("phone") or t.get("contact:phone") or t.get("contact:mobile") or ""
        mail = t.get("email") or t.get("contact:email") or ""
        if not name or not (site or phone or mail):
            continue
        if (name.lower(), domain_of(site)) in have:
            continue
        have.add((name.lower(), domain_of(site)))
        addr = " ".join(filter(None, [t.get("addr:housenumber"), t.get("addr:street"), t.get("addr:city")]))
        info = "; ".join(f"{k}={t[k]}" for k in ("cuisine", "description", "brand", "delivery", "takeaway",
                                                  "contact:instagram", "contact:facebook") if k in t)
        new.append({"firm_name": name, "website": site, "contact_name": "", "contact_role": "",
                    "email": mail, "phone": phone.split(";")[0].strip(), "country": cc,
                    "address": addr, "segment": seg_name, "info": info})
        if len(new) >= int(limit):
            break
    first = not PROSPECTS.exists()
    with open(PROSPECTS, "a", newline="", encoding="utf-8") as f:
        w_ = csv.DictWriter(f, fieldnames=SOURCE_FIELDS)
        if first:
            w_.writeheader()
        w_.writerows(new)
    print(f"{len(new)} new {seg_name} from {place} ({cc}) -> {PROSPECTS.name}")
    return len(new)


# ------------------------------------------------------------------ RESEARCH + DRAFT
def fetch_site(url):
    """Return (readable_text, emails_found) from homepage + contact/about/team pages."""
    if not url:
        return "", []
    if not url.startswith("http"):
        url = "https://" + url
    base = url.rstrip("/")
    texts, emails = [], []
    for i, path in enumerate(("", "/contact", "/about")):
        try:
            r = requests.get(base + path, headers=UA, timeout=6)
            if not (r.ok and "text/html" in r.headers.get("content-type", "")):
                continue
        except requests.RequestException:
            if i == 0:
                break  # homepage unreachable: don't waste time on other pages
            continue
        html = r.text
        emails += EMAIL_RE.findall(html)
        t = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
        t = re.sub(r"(?s)<[^>]+>", " ", t)
        texts.append(re.sub(r"\s+", " ", t).strip()[:800])
    d = domain_of(url)
    clean = [m.lower() for m in emails if not any(b in m.lower() for b in BAD_EMAIL)]
    clean.sort(key=lambda m: (d not in m, m.startswith(("info@", "contact@", "hello@", "office@"))))
    return " ".join(texts)[:2200], list(dict.fromkeys(clean))


PROMPT = """You write cold outreach for {sender}. Offer:
{offer}

PROSPECT: {firm} ({segment}), {place}. Known contact: {name} {role}
SITE_TEXT (only source of truth; may be thin or empty): {site}
Language: write all messages in {lang}, unless SITE_TEXT is clearly in another language, then use that one.

Rules:
- Use ONLY facts present in SITE_TEXT. Never invent clients, tools, numbers, or claim you saw things you did not.
- contact_name/contact_role: if no known contact, give an owner/manager/founder ONLY if explicitly named in SITE_TEXT, else "".
- hook = one specific, observable fact from SITE_TEXT. If SITE_TEXT is thin, hook = what kind of business it is and where (e.g. "a restaurant in Lagos"), and fit_score is at most 3.
- fit_score 1-5: how likely this business has the problem the offer solves, given its type and size (5 = clear recurring pain; 1 = irrelevant).
- email_body: plain text, <=90 words, greeting "Hi <first name>," or just "Hi," if no name, opens with the hook (no flattery, no "hope this finds you well"), states the offer in one sentence, ends with ONE low-friction yes/no question. Sign off "{sender}".
- subject: <=6 words, lowercase, no hype.
- whatsapp_msg: <=45 words, conversational, names the business, one question, no links.
- followup1 (<=45 words): adds one new angle, not "just checking in". followup2 (<=35 words): polite close-the-loop.
Return ONLY JSON with keys: contact_name, contact_role, fit_score, hook, subject, email_body, whatsapp_msg, followup1, followup2."""


def has_key():
    k = os.getenv("GROQ_API_KEY", "")
    return bool(k) and "paste" not in k.lower()


def get_client():
    return Groq(api_key=os.getenv("GROQ_API_KEY", ""))


def draft(p, site):
    cc = (p.get("country") or "").upper()
    prompt = PROMPT.format(sender=SENDER, offer=get_offer(p.get("segment", "")), firm=p["firm_name"],
                           segment=p.get("segment") or "business", place=p.get("address") or cc,
                           name=p.get("contact_name", ""), role=p.get("contact_role", ""),
                           site=site or "(none)", lang=LANG.get(cc, "English"))
    for attempt in range(3):
        try:
            r = get_client().chat.completions.create(
                model=MODEL, temperature=0.4, response_format={"type": "json_object"},
                messages=[{"role": "user", "content": prompt}])
            return json.loads(r.choices[0].message.content)
        except Exception as e:
            msg = str(e).lower()
            wait = 10 * (attempt + 1) if ("429" in msg or "rate" in msg) else 2
            print("  retry:", str(e)[:100])
            time.sleep(wait)
    return None


# ------------------------------------------------------------------ DB
def load():
    if not DB.exists():
        return []
    with open(DB, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save(rows):
    with open(DB, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def today():
    return dt.date.today()


def _work(item):
    """Research + draft one prospect (runs in a worker thread). Returns a DB row or None."""
    pid, phone, cc, p = item
    site, found = fetch_site(p.get("website", ""))
    email = (p.get("email") or "").strip() or (found[0] if found else "")
    info = p.get("info", "")
    base = {"id": pid, "email": email, "phone": phone, "firm_name": p["firm_name"],
            "segment": p.get("segment", ""), "contact_name": p.get("contact_name", ""),
            "contact_role": p.get("contact_role", ""), "country": cc,
            "website": p.get("website", ""), "info": info,
            "sent_date": "", "followups_sent": "0", "next_action": ""}
    if not (email or phone):
        return {**base, "fit_score": "1", "status": "nofit"}
    d = draft(p, (site + (" INFO: " + info if info else "")).strip())
    if not d:
        return None
    subj, body, wa = d.get("subject", ""), d.get("email_body", ""), d.get("whatsapp_msg", "")
    fit = int(d.get("fit_score", 1) or 1)
    base.update(contact_name=d.get("contact_name") or base["contact_name"],
                contact_role=d.get("contact_role") or base["contact_role"])
    return {**base, "fit_score": str(fit), "hook": d.get("hook", ""), "subject": subj,
            "email_body": body, "whatsapp_msg": wa,
            "followup1": d.get("followup1", ""), "followup2": d.get("followup2", ""),
            "mailto_link": mailto(email, subj, body), "whatsapp_link": wa_link(phone, wa),
            "status": "new" if fit >= 3 else "nofit"}


def cmd_generate(path=PROSPECTS, progress=None, workers=4):
    rows = load()
    seen = {r["id"] for r in rows}
    with open(path, newline="", encoding="utf-8") as f:
        prospects = list(csv.DictReader(f))
    todo = []
    for p in prospects:
        cc = (p.get("country") or "").upper()
        phone = normalize_phone(p.get("phone", ""), cc)
        pid = ((p.get("email") or "").lower() or domain_of(p.get("website", ""))
               or re.sub(r"\D", "", phone) or p["firm_name"].lower())
        if pid in seen:
            continue
        seen.add(pid)
        todo.append((pid, phone, cc, p))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_work, it): it for it in todo}
        for fut in as_completed(futs):
            done += 1
            try:
                row = fut.result()
            except Exception as e:
                print("  skipped:", futs[fut][3]["firm_name"], str(e)[:80])
                row = None
            if row:
                rows.append(row)
                save(rows)  # saved as each finishes, so nothing is lost on a crash
            if progress:
                progress(done, len(todo))
            print(f"[{done}/{len(todo)}] {futs[fut][3]['firm_name']}")
    print("Done.")


def cmd_today():
    rows = load()
    sent_today = sum(1 for r in rows if r["sent_date"] == str(today()))
    room = max(0, DAILY_CAP - sent_today)
    new = sorted([r for r in rows if r["status"] == "new"],
                 key=lambda r: -int(r["fit_score"] or 0))[:room]
    due = [r for r in rows if r["status"] == "sent" and r["next_action"]
           and r["next_action"] <= str(today())]
    print(f"=== FOLLOW-UPS DUE: {len(due)} ===")
    for r in due:
        n = int(r["followups_sent"] or 0)
        print(f"\n[{r['id']}] {r['firm_name']}\n{r['followup1'] if n == 0 else r['followup2']}")
    print(f"\n=== NEW SENDS (cap {DAILY_CAP}, {room} left): {len(new)} ===")
    for r in new:
        print(f"\n[{r['id']}] {r['firm_name']} | fit {r['fit_score']} | hook: {r['hook']}")
        if r["mailto_link"]:
            print("EMAIL tap:", r["mailto_link"])
        if r["whatsapp_link"]:
            print("WHATSAPP tap:", r["whatsapp_link"])


def cmd_sent(pid):
    rows = load()
    for r in rows:
        if pid.lower() not in (r["id"].lower(), r["email"].lower()):
            continue
        if r["status"] == "new":
            r.update(status="sent", sent_date=str(today()), followups_sent="0",
                     next_action=str(today() + dt.timedelta(days=FOLLOWUP_DAYS[0])))
        elif r["status"] == "sent":
            n = int(r["followups_sent"] or 0) + 1
            r["followups_sent"] = str(n)
            base = dt.date.fromisoformat(r["sent_date"])
            if n < len(FOLLOWUP_DAYS):
                r["next_action"] = str(base + dt.timedelta(days=FOLLOWUP_DAYS[n]))
            else:
                r.update(status="closed_no_reply", next_action="")
        save(rows)
        print("logged.")
        return
    print("id not found")


def cmd_mark(pid, status):
    rows = load()
    for r in rows:
        if pid.lower() in (r["id"].lower(), r["email"].lower()):
            r.update(status=status, next_action="")
            save(rows)
            print("marked.")
            return
    print("id not found")


def cmd_stats():
    rows = load()
    touched = [r for r in rows if r["sent_date"]]
    replied = [r for r in touched if r["status"] in ("replied", "call", "won", "lost")]
    calls = [r for r in touched if r["status"] in ("call", "won")]
    won = [r for r in touched if r["status"] == "won"]
    n = len(touched) or 1
    print(f"prospects {len(rows)} | contacted {len(touched)} | replies {len(replied)} "
          f"({100 * len(replied) / n:.1f}%) | calls {len(calls)} | won {len(won)}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["go"] and len(a) >= 3:
        cmd_source(a[1], a[2], "", *(a[3:4])); cmd_generate(); cmd_today()
    elif a[:1] == ["source"] and len(a) >= 3: cmd_source(a[1], a[2], "", *(a[3:4]))
    elif a[:1] == ["generate"]: cmd_generate(*(a[1:2]))
    elif a[:1] == ["today"]: cmd_today()
    elif a[:1] == ["sent"] and len(a) == 2: cmd_sent(a[1])
    elif a[:1] == ["mark"] and len(a) == 3: cmd_mark(a[1], a[2])
    elif a[:1] == ["stats"]: cmd_stats()
    elif a[:1] == ["segments"]: print("\n".join(SEGMENTS))
    else: print(__doc__)
