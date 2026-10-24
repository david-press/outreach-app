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
import asyncio, csv, hashlib, json, os, re, sys, threading, time, datetime as dt
from pathlib import Path
from urllib.parse import quote, urlparse
import httpx
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv
from groq import AsyncGroq, Groq

BASE = Path(__file__).parent
load_dotenv(BASE / ".env", override=True)  # the .env file wins over stray system variables


def _env_key(name):
    """Read a key and clean accidental spaces/quotes (a common cause of 'invalid key')."""
    return os.getenv(name, "").strip().strip("'\"").strip()
(BASE / "data").mkdir(exist_ok=True)
(BASE / "offers").mkdir(exist_ok=True)

# Groq changes its model catalog often, so the app asks Groq which models this key can use (live_models).
PREFERRED = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.6-27b"]
MODELS = [m.strip() for m in os.getenv("OUTREACH_MODELS", "").split(",") if m.strip()] or list(PREFERRED)
MODEL = MODELS[0]
LAST_AI_ERROR = {"msg": ""}
_LIVE = {"done": False}
DB = BASE / "data" / "outreach.csv"
PROSPECTS = BASE / "data" / "prospects.csv"
OFFER_FILE = BASE / "offer.txt"
OFFERS_DIR = BASE / "offers"
DAILY_CAP = int(os.getenv("DAILY_CAP", "15"))
SENDER = os.getenv("SENDER_NAME", "Krish")
FOLLOWUP_DAYS = (3, 8)
FETCH_CONC = int(os.getenv("FETCH_CONCURRENCY", "30"))   # website fetches in flight
LLM_CONC = int(os.getenv("LLM_CONCURRENCY", "5"))        # AI calls in flight

SOURCE_FIELDS = ["firm_name", "website", "contact_name", "contact_role", "email", "phone",
                 "country", "address", "segment", "info"]
FIELDS = ["id", "email", "phone", "firm_name", "segment", "variant", "contact_name", "contact_role",
          "country", "website", "info", "fit_score", "hook", "subject", "email_body",
          "whatsapp_msg", "followup1", "followup2", "mailto_link", "whatsapp_link", "status",
          "sent_date", "followups_sent", "next_action"]

UA = {"User-Agent": "OutreachApp/1.0 (small-business prospect research; personal use)"}
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
BAD_EMAIL = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", "sentry", "wixpress",
             "example.", "yourdomain", "domain.com")

# ------------------------------------------------------------------ OFFERS (per segment)
DEFAULT_OFFER = """PROBLEM: small business owners lose hours each week typing receipts and supplier invoices into spreadsheets.
DELIVERABLE: they send photos of receipts/invoices and get back a clean, categorized weekly expense sheet.
FIRST STEP: free trial on one week of their receipts (max 20); a sample sheet is available first.
PRICE (never mention in first messages): if they continue, USD 300 for a one-month pilot; second month free if it doesn't deliver.
(Edit this for each business type in the app: one problem, one deliverable, one small free step.)"""


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
               [("shop", "clothes|boutique|shoes|fashion_accessories|perfumery|bag|jewelry")]),
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


# ------------------------------------------------------------------ CACHE
CACHE = BASE / "data" / "cache"
CACHE.mkdir(parents=True, exist_ok=True)


def _cf(kind, key):
    return CACHE / f"{kind}_{hashlib.md5(key.encode()).hexdigest()}.json"


def cache_get(kind, key, ttl):
    f = _cf(kind, key)
    try:
        if f.exists() and time.time() - f.stat().st_mtime < ttl:
            return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def cache_put(kind, key, val):
    try:
        _cf(kind, key).write_text(json.dumps(val), encoding="utf-8")
    except Exception:
        pass


# ------------------------------------------------------------------ SOURCING
OVERPASS_MIRRORS = ["https://overpass-api.de/api/interpreter",
                    "https://overpass.kumi.systems/api/interpreter",
                    "https://overpass.private.coffee/api/interpreter",
                    "https://overpass.openstreetmap.fr/api/interpreter",
                    "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]


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
        time.sleep(6)
    raise RuntimeError("Map data servers are busy or unreachable. Wait a minute and try again. "
                       f"Last error: {last}")



def nominatim_fallback(place, seg_name):
    """Backup source when every Overpass server is down: category search via Nominatim."""
    kw = seg_name.split("-")[-1].split("|")[0] if seg_name.startswith(("amenity", "shop", "office", "tourism", "leisure")) else seg_name
    res = http_json("GET", ["https://nominatim.openstreetmap.org/search"], retries=2,
                    params={"q": f"{kw} in {place}", "format": "json", "limit": 40,
                            "addressdetails": 1, "extratags": 1})
    els = []
    for r in res:
        t = dict(r.get("extratags") or {})
        t["name"] = r.get("name") or r.get("display_name", "").split(",")[0]
        a = r.get("address", {})
        t.setdefault("addr:street", a.get("road", ""))
        t.setdefault("addr:city", a.get("city") or a.get("town") or a.get("suburb", ""))
        els.append({"tags": t})
    return els


def has_serper():
    k = _env_key("SERPER_API_KEY")
    return bool(k) and "paste" not in k.lower()


def _err_text(r):
    try:
        return str(r.json().get("error") or r.json())[:160]
    except Exception:
        return (r.text or "")[:160]


def has_serpapi():
    k = _env_key("SERPAPI_KEY")
    return bool(k) and "paste" not in k.lower()


def google_provider():
    return "serper" if has_serper() else "serpapi" if has_serpapi() else "osm"


def source_name():
    if has_serper() and has_serpapi():
        return "Google Maps data (Serper first, SerpApi as automatic backup, then OpenStreetMap)"
    return {"serper": "Google Maps data (Serper)", "serpapi": "Google Maps data (SerpApi)",
            "osm": "OpenStreetMap (free, thinner data). Add a Serper or SerpApi key in .env for Google Maps data."}[google_provider()]


def serpapi_elements(place, segment_text, cc, target):
    """Google Maps data via SerpApi (free plan, no card). Returned in the same shape as OSM elements."""
    out, seen = [], set()
    for start in range(0, 100, 20):
        params = {"engine": "google_maps", "type": "search", "q": f"{segment_text.strip()} in {place}",
                  "hl": "en", "start": start, "api_key": _env_key("SERPAPI_KEY")}
        if cc:
            params["gl"] = cc.lower()
        r = requests.get("https://serpapi.com/search.json", params=params, timeout=40)
        if r.status_code in (401, 403):
            raise RuntimeError(f"SerpApi said: {_err_text(r)} (HTTP {r.status_code}). Run the key test in the sidebar.")
        if r.status_code == 429:
            raise RuntimeError("SerpApi monthly searches used up. Check your serpapi.com dashboard.")
        r.raise_for_status()
        fresh = 0
        for p_ in r.json().get("local_results", []):
            name, pid = p_.get("title"), (p_.get("place_id") or p_.get("title"))
            if not name or pid in seen:
                continue
            seen.add(pid)
            fresh += 1
            out.append({"tags": {"name": name, "website": p_.get("website") or "", "phone": p_.get("phone") or "",
                                 "addr:street": p_.get("address") or "", "description": p_.get("type") or ""}})
        if len(out) >= target or not fresh:
            break
    return out


def check_keys():
    """Test each key with a free call and say exactly what is wrong."""
    out = []
    if not has_key():
        out.append("Groq: no key found. Set GROQ_API_KEY in .env")
    else:
        try:
            ids = [m.id for m in get_client().models.list().data]
            out.append(f"Groq: OK ({len(ids)} models available)")
        except Exception as e:
            out.append(f"Groq: FAILED - {str(e)[:140]}")
    sk = _env_key("SERPER_API_KEY")
    if sk:
        hint = " (64 characters: that looks like a SerpApi key, move it to SERPAPI_KEY)" if len(sk) == 64 else ""
        out.append(f"Serper: key found, {len(sk)} characters{hint}")
    if has_serpapi():
        try:
            r = requests.get("https://serpapi.com/account", params={"api_key": _env_key("SERPAPI_KEY")}, timeout=20)
            if r.status_code == 200:
                d = r.json()
                out.append(f"SerpApi: OK - plan {d.get('plan_name', '?')}, {d.get('total_searches_left', '?')} searches left")
            else:
                out.append(f"SerpApi: FAILED (HTTP {r.status_code}) - {_err_text(r)}")
        except Exception as e:
            out.append(f"SerpApi: could not reach server - {str(e)[:100]}")
    else:
        out.append("SerpApi: no key set")
    if has_serper() and has_serpapi():
        out.append("Both keys set: Serper is tried first, SerpApi is the automatic backup.")
    out.append("Active map source: " + source_name())
    return out


def segment_for(text):
    """Known type -> OSM tags. With a Serper key ANY text works (Google understands it)."""
    try:
        return resolve_segment(text)
    except ValueError:
        if google_provider() != "osm" and (text or "").strip():
            return slug(text), None
        raise


def overpass_race(q, timeout=20):
    """Ask every Overpass mirror at once; the first good answer wins (no more waiting on dead servers)."""
    def one(url):
        r = requests.post(url, data={"data": q}, headers=UA, timeout=timeout)
        r.raise_for_status()
        return r.json()
    ex = ThreadPoolExecutor(len(OVERPASS_MIRRORS))
    futs = [ex.submit(one, u) for u in OVERPASS_MIRRORS]
    last = "no response"
    try:
        for f in as_completed(futs, timeout=timeout + 5):
            try:
                return f.result()
            except Exception as e:
                last = str(e)[:100]
    except Exception as e:
        last = str(e)[:100]
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    raise RuntimeError(f"All free map servers failed right now ({last}).")


def serper_elements(place, segment_text, cc, target):
    """Google Maps data via Serper.dev (free credits, no card). Returned in the same shape as OSM elements."""
    hdr = {"X-API-KEY": _env_key("SERPER_API_KEY"), "Content-Type": "application/json"}
    out, seen = [], set()
    for page in range(1, 6):
        payload = {"q": f"{segment_text.strip()} in {place}", "page": page}
        if cc:
            payload["gl"] = cc.lower()
        r = None
        for ep in ("maps", "places"):
            r = requests.post(f"https://google.serper.dev/{ep}", json=payload, headers=hdr, timeout=20)
            if r.status_code != 404:
                break
        if r.status_code in (401, 403):
            raise RuntimeError(f"Serper said: {_err_text(r)} (HTTP {r.status_code}). Run the key test in the sidebar.")
        if r.status_code in (402, 429):
            raise RuntimeError("Serper credits or rate limit used up. Check your serper.dev dashboard.")
        r.raise_for_status()
        fresh = 0
        for p_ in r.json().get("places", []):
            name, cid = p_.get("title"), (p_.get("cid") or p_.get("title"))
            if not name or cid in seen:
                continue
            seen.add(cid)
            fresh += 1
            out.append({"tags": {"name": name, "website": p_.get("website") or "",
                                 "phone": p_.get("phoneNumber") or "", "addr:street": p_.get("address") or "",
                                 "description": p_.get("category") or ""}})
        if len(out) >= target or not fresh:
            break
    return out


def reset_queue():
    """New session: archive everything not yet sent (history and follow-ups are kept)."""
    rows = load()
    n = 0
    for r in rows:
        if r["status"] == "new":
            r["status"] = "archived"
            n += 1
    save(rows)
    if PROSPECTS.exists():
        PROSPECTS.unlink()
    return n


def cmd_source(place, segment, country="", limit=40):
    """Find businesses of a segment in a place. Google Maps data if SERPER_API_KEY is set, else OpenStreetMap."""
    seg_name, clauses = segment_for(segment)
    provider = google_provider()
    use_serper = provider != "osm"
    gkey = place.lower()
    geo = cache_get("geo", gkey, 30 * 86400)
    if geo is None:
        try:
            g = http_json("GET", ["https://nominatim.openstreetmap.org/search"], retries=2, timeout=15,
                          params={"q": place, "format": "json", "limit": 1, "addressdetails": 1})
            if not g:
                raise RuntimeError(f"Could not find '{place}'. Try 'Lagos, Nigeria'.")
            geo = {"bbox": g[0]["boundingbox"], "cc": g[0].get("address", {}).get("country_code", "")}
            cache_put("geo", gkey, geo)
        except RuntimeError:
            if not use_serper:
                raise
            geo = {"bbox": None, "cc": ""}
    cc = (country or geo["cc"]).upper()
    ekey = f"{gkey}|{seg_name}|{provider}"
    els = cache_get("els", ekey, 86400)  # repeat searches within 24h are instant
    if els is None:
        for prov in (["serper"] if has_serper() else []) + (["serpapi"] if has_serpapi() else []):
            try:
                els = (serper_elements if prov == "serper" else serpapi_elements)(place, segment, cc, int(limit) * 2)
                cache_put("els", ekey, els)
                provider = prov
                break
            except Exception as e:  # out of credits, bad key, outage: try the next source
                print(f"  {prov} failed: {str(e)[:100]}; trying the next source")
        if els is None:
            if geo.get("bbox") is None:
                raise RuntimeError("Google Maps sources failed and the place could not be looked up for the free map. Try again in a minute.")
            provider = "osm"
            s_, n_, w_, e_ = geo["bbox"]
            contact = ["phone", "website", "email", "contact:phone", "contact:website", "contact:email"]
            body = "".join(f'nwr["{k}"~"^({v})$"]["{c}"]({s_},{w_},{n_},{e_});'
                           for k, v in clauses for c in contact)
            q = f"[out:json][timeout:25];({body});out tags center 300;"
            try:
                els = overpass_race(q).get("elements", [])
                cache_put("els", ekey, els)
            except RuntimeError as err:
                print("Overpass unavailable, using Nominatim fallback:", str(err)[:120])
                els = nominatim_fallback(place, seg_name)
    have = {(r["firm_name"].lower(), domain_of(r["website"])) for r in load()}
    if PROSPECTS.exists():
        with open(PROSPECTS, newline="", encoding="utf-8") as f:
            have |= {(r["firm_name"].lower(), domain_of(r["website"])) for r in csv.DictReader(f)}
    new = []
    for el in els:
        t = el.get("tags", {})
        name = t.get("name")
        site = t.get("website") or t.get("contact:website") or ""
        phone = t.get("phone") or t.get("contact:phone") or t.get("contact:mobile") or ""
        mail = t.get("email") or t.get("contact:email") or ""
        if not name or not (site or phone or mail):
            continue
        key = (name.lower(), domain_of(site))
        if key in have:
            continue
        have.add(key)
        addr = " ".join(filter(None, [t.get("addr:housenumber"), t.get("addr:street"), t.get("addr:city")]))
        info = "; ".join(f"{k}={t[k]}" for k in ("cuisine", "description", "brand", "delivery", "takeaway",
                                                  "contact:instagram", "contact:facebook") if t.get(k))
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
    print(f"{len(new)} new {seg_name} from {place} ({cc}) via {provider}")
    return len(new)


# ------------------------------------------------------------------ RESEARCH + DRAFT
def parse_pages(htmls, url):
    """Readable text + emails from raw HTML of a few pages."""
    texts, emails = [], []
    for html in htmls:
        if not html:
            continue
        emails += EMAIL_RE.findall(html)
        t = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
        t = re.sub(r"(?s)<[^>]+>", " ", t)
        texts.append(re.sub(r"\s+", " ", t).strip()[:800])
    d = domain_of(url)
    clean = [m.lower() for m in emails if not any(b in m.lower() for b in BAD_EMAIL)]
    clean.sort(key=lambda m: (d not in m, m.startswith(("info@", "contact@", "hello@", "office@"))))
    return " ".join(texts)[:2200], list(dict.fromkeys(clean))


async def afetch_site(http, url):
    """Fetch homepage + /contact + /about IN PARALLEL. Cached 7 days."""
    if not url:
        return "", []
    hit = cache_get("site", url, 7 * 86400)
    if hit:
        return hit["text"], hit["emails"]
    full = url if url.startswith("http") else "https://" + url
    base = full.rstrip("/")

    async def one(path):
        try:
            r = await http.get(base + path, timeout=6, follow_redirects=True)
            if r.status_code == 200 and "text/html" in r.headers.get("content-type", ""):
                return r.text
        except Exception:
            pass
        return None

    htmls = await asyncio.gather(*(one(p) for p in ("", "/contact", "/about")))
    text, emails = parse_pages(htmls, full)
    if text:
        cache_put("site", url, {"text": text, "emails": emails})
    return text, emails


PROMPT = """You are a cold-outreach copywriter. Your ONLY objective: maximize the probability that THIS recipient replies positively. Optimize for the recipient's psychology, never for the sender's taste.

Sender: {sender}
OFFER (structured; the GOAL line defines what a positive reply means, CTA is the closing question):
{offer}

RECIPIENT: {firm} ({segment}), {place}. Known contact: {name} {role}
SITE_TEXT (only source of truth about them; may be thin or empty): {site}
Language: write all messages in {lang}, unless SITE_TEXT is clearly in another language, then use that one.
Style variant: {variant_style}

Principles (follow strictly):
1. Relevance first. The first sentence is about THEM: one specific, true observation from SITE_TEXT (exact product/service words, never mislabel). If SITE_TEXT is thin, use only the business type and place and do not pretend you studied them.
2. One problem only, phrased the way their owner would say it, tied to their business context. No feature lists.
3. Sell the outcome, not the tool. No jargon: avoid 'AI', 'automation', 'platform', 'solution', 'leverage' unless the offer itself requires it.
4. Remove risk. Include the offer's FIRST STEP with its limit, so replying costs them nothing.
5. Credibility only from the offer's PROOF line. NEVER invent clients, numbers, statistics, or claims like 'most owners'. If PROOF is none, imply no proof.
6. Exactly one low-effort call to action, answerable in one word, ending the message (use the offer's CTA). Exactly one question mark. Do not ask for a call or calendar slot unless the offer's GOAL/CTA says so.
7. Brevity: email_body max 75 words, plain text, no links, no attachments, no emojis, no flattery, no 'hope this finds you well'. Sign off with the sender name.
8. Greeting: 'Hi <first name>,' if known, else 'Hi <business name> team,'. If no contact name is known and words allow, add one short line asking who handles this.
9. Price: state it only if the offer's PRICE line says INCLUDE in first message; otherwise never mention price in any message.
10. subject: max 5 words, lowercase, specific to them, no hype, no clickbait.
11. whatsapp_msg: max 45 words, conversational, names the business, same first step and CTA, no price unless allowed, no links.
12. followup1 (max 45 words, no 'just checking in'): add a NEW angle or one concrete example of the result. followup2 (max 35 words): a polite close-the-loop that makes saying no easy.
- contact_name/contact_role: if no known contact, give an owner/manager/founder ONLY if explicitly named in SITE_TEXT, else "".
- hook = the specific observation you used. If SITE_TEXT is thin, hook = what kind of business it is and where, and fit_score is at most 3.
- fit_score 1-5: likelihood this business has the problem the offer solves, given its type and size (5 = clear recurring pain; 1 = irrelevant).
Return ONLY JSON with keys: contact_name, contact_role, fit_score, hook, subject, email_body, whatsapp_msg, followup1, followup2."""

VARIANTS = {
    "A": "PROBLEM-LED: open with the specific problem this business likely has, then the fix, then the first step.",
    "B": "RESULT-LED: open with the concrete result they would get, then why it fits their business, then the first step.",
}

OFFER_PROMPT = """Turn the user's rough description of what they sell or want to achieve into a structured cold-outreach brief.
Use ONLY what the user wrote. Do NOT invent proof, prices, numbers, clients or guarantees. Where something important is missing, put "none" and add a concrete item to "gaps".
USER DESCRIPTION: {raw}
TARGET SEGMENT: {segment}
Return ONLY JSON with keys:
goal: one of sell, book_call, pilot, partnership, feedback, other (what a positive reply means)
problem: the single sharpest problem this target has, in their own words
deliverable: what they get, in plain outcome words (no jargon)
first_step: the smallest low-risk step the recipient can take or receive free, with a limit; if the user gave none, propose ONE sensible option and end it with "(suggested)"
proof: only proof the user stated, else "none"
price: price/terms if the user stated them, else "none"
include_price_in_first_message: true only if the user said to, else false
cta: the one-line, low-effort closing question (answerable in one word)
gaps: up to 4 short strings, each naming what is weak or missing and what to add to raise reply rates"""


def has_key():
    k = _env_key("GROQ_API_KEY")
    return bool(k) and "paste" not in k.lower()


def get_client():
    return Groq(api_key=_env_key("GROQ_API_KEY"))



def structure_offer(raw, segment=""):
    """Turn a rough description into a structured offer. Returns (offer_text, gaps)."""
    prompt = OFFER_PROMPT.format(raw=raw.strip(), segment=segment or "general")
    d = chat_json(prompt, 0.2)
    price = d.get("price") or "none"
    inc = bool(d.get("include_price_in_first_message"))
    price_line = (f"{price} (INCLUDE in first message)" if inc and price != "none"
                  else f"{price} (never mention in first messages or follow-ups)")
    text = "\n".join([f"GOAL: {d.get('goal', 'sell')}", f"PROBLEM: {d.get('problem', '')}",
                      f"DELIVERABLE: {d.get('deliverable', '')}", f"FIRST STEP: {d.get('first_step', '')}",
                      f"PROOF: {d.get('proof', 'none')}", f"PRICE: {price_line}", f"CTA: {d.get('cta', '')}"])
    return text, [g for g in d.get("gaps", []) if g]


def _model_gone(msg):
    return any(x in msg for x in ("does not exist", "model_not_found", "decommissioned", "no access", "not supported"))


def _parse_json(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def _chat_kwargs(model, prompt, temperature):
    kw = dict(model=model, temperature=temperature, response_format={"type": "json_object"},
              messages=[{"role": "user", "content": prompt}])
    if model.startswith("openai/gpt-oss"):
        kw["extra_body"] = {"reasoning_effort": "low"}  # faster, cheaper; plenty for short messages
    return kw


def live_models():
    """Ask Groq which models this key can use right now and order them. Self-heals when models are retired."""
    if _LIVE["done"]:
        return MODELS
    try:
        ids = [m.id for m in get_client().models.list().data]
    except Exception as e:
        print("  could not list Groq models:", str(e)[:80])
        return MODELS
    skip = ("whisper", "orpheus", "guard", "compound", "tts", "embed", "moderation")
    ids = [i for i in ids if not any(x in i.lower() for x in skip)]
    wanted = [m for m in MODELS if m in ids]
    wanted += [m for m in PREFERRED if m in ids and m not in wanted]
    if not wanted:
        wanted = ids[:3]
    if wanted:
        MODELS[:] = wanted
    _LIVE["done"] = True
    print("  AI models in use:", ", ".join(MODELS))
    return MODELS


def chat_json(prompt, temperature=0.2):
    """Synchronous JSON chat with automatic model switching."""
    live_models()
    last = None
    for _ in range(len(MODELS) + 2):
        if not MODELS:
            break
        model = MODELS[0]
        try:
            r = get_client().chat.completions.create(**_chat_kwargs(model, prompt, temperature))
            return _parse_json(r.choices[0].message.content)
        except Exception as e:
            last, msg = e, str(e).lower()
            if _model_gone(msg) and len(MODELS) > 1:
                MODELS.remove(model)
            elif ("429" in msg or "rate" in msg) and len(MODELS) > 1:
                MODELS.append(MODELS.pop(0))
            else:
                raise
    raise last or RuntimeError("No AI model available for this Groq key.")


def get_aclient():
    return AsyncGroq(api_key=_env_key("GROQ_API_KEY"))


def build_prompt(p, site):
    cc = (p.get("country") or "").upper()
    return PROMPT.format(sender=SENDER, offer=get_offer(p.get("segment", "")), firm=p["firm_name"],
                         segment=p.get("segment") or "business", place=p.get("address") or cc,
                         name=p.get("contact_name", ""), role=p.get("contact_role", ""),
                         site=site or "(none)", lang=LANG.get(cc, "English"),
                         variant_style=VARIANTS.get(p.get("variant", "A"), VARIANTS["A"]))


def _retry_after(e):
    try:
        v = e.response.headers.get("retry-after")
        return min(float(v), 60) if v else 0
    except Exception:
        return 0


async def adraft(p, site, sem, ac):
    prompt = build_prompt(p, site)
    async with sem:  # holding the slot while waiting also throttles everyone on a rate limit
        for attempt in range(8):
            if not MODELS:
                LAST_AI_ERROR["msg"] = "No AI model available for this Groq key."
                return None
            model = MODELS[attempt % len(MODELS)]
            try:
                r = await ac.chat.completions.create(**_chat_kwargs(model, prompt, 0.4))
                return _parse_json(r.choices[0].message.content)
            except Exception as e:
                msg = str(e).lower()
                LAST_AI_ERROR["msg"] = str(e)[:200]
                print(f"  {model}: {str(e)[:80]}")
                if _model_gone(msg):
                    if model in MODELS and len(MODELS) > 1:
                        MODELS.remove(model)  # retired model: drop it and move on
                    continue
                limited = "429" in msg or "rate" in msg
                if limited and attempt < len(MODELS) - 1:
                    continue  # each model has its own limit: switch instantly
                await asyncio.sleep(min(_retry_after(e) or 6 * (attempt + 1), 20) if limited else 2)
    return None


# ------------------------------------------------------------------ DB
def load():
    if not DB.exists():
        return []
    with open(DB, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save(rows):
    tmp = DB.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    for _ in range(10):  # Windows can briefly lock the file while the app reads it
        try:
            os.replace(tmp, DB)
            return
        except PermissionError:
            time.sleep(0.1)
    os.replace(tmp, DB)


def today():
    return dt.date.today()


JOB = {"running": False, "stage": "", "done": 0, "total": 0, "found": None, "drafted": 0, "error": "", "timings": {}}


async def _pipeline(todo, rows, progress):
    """All websites are fetched at once (async); AI drafts run at the rate-limit-safe pace.
    Fetching never waits on the AI, and each draft is saved the moment it is ready."""
    fsem, lsem = asyncio.Semaphore(FETCH_CONC), asyncio.Semaphore(LLM_CONC)
    t0 = time.time()
    marks = {"fetch": 0.0}
    await asyncio.to_thread(live_models)
    ac = get_aclient()
    if progress:
        progress(0, len(todo))

    async with httpx.AsyncClient(headers=UA, limits=httpx.Limits(max_connections=FETCH_CONC * 3)) as http:
        async def one(item):
            pid, phone, cc, p = item
            p = {**p, "variant": "A" if int(hashlib.md5(pid.encode()).hexdigest(), 16) % 2 == 0 else "B"}
            async with fsem:
                site, found = await afetch_site(http, p.get("website", ""))
            marks["fetch"] = max(marks["fetch"], time.time() - t0)
            email = (p.get("email") or "").strip() or (found[0] if found else "")
            info = p.get("info", "")
            base = {"id": pid, "email": email, "phone": phone, "firm_name": p["firm_name"],
                    "segment": p.get("segment", ""), "variant": p["variant"],
                    "contact_name": p.get("contact_name", ""), "contact_role": p.get("contact_role", ""),
                    "country": cc, "website": p.get("website", ""), "info": info,
                    "sent_date": "", "followups_sent": "0", "next_action": ""}
            if not (email or phone):
                return {**base, "fit_score": "1", "status": "nofit"}
            d = await adraft(p, (site + (" INFO: " + info if info else "")).strip(), lsem, ac)
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

        tasks = {asyncio.create_task(one(it)): it for it in todo}
        done = 0
        for fut in asyncio.as_completed(list(tasks)):
            done += 1
            try:
                row = await fut
            except Exception as e:
                print("  skipped:", str(e)[:80])
                row = None
            if row:
                rows.append(row)
                save(rows)  # drafts are usable immediately; nothing is lost on a crash
            if progress:
                progress(done, len(todo))
            print(f"[{done}/{len(todo)}]")
    return {"read websites": round(marks["fetch"], 1), "research + draft total": round(time.time() - t0, 1)}


def cmd_generate(path=PROSPECTS, progress=None, max_new=None):
    rows = load()
    seen = {r["id"] for r in rows}
    if not Path(path).exists():
        print("No prospects file yet.")
        return {}
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
    todo.sort(key=lambda it: 0 if it[3].get("email") else 1 if it[3].get("website") else 2)
    if max_new:
        todo = todo[:int(max_new)]  # draft only what you will send today (saves AI quota and time)
    JOB["drafted"] = len(todo)
    if not todo:
        print("Nothing new to draft.")
        return {}
    timings = asyncio.run(_pipeline(todo, rows, progress))
    print("Done.", timings)
    return timings


def start_job(place, segment, limit, clear_first=True):
    """Run find + research + draft in the background so the app never freezes."""
    if JOB["running"]:
        return False
    try:
        seg_name, _ = segment_for(segment)
    except ValueError as e:
        JOB.update(error=str(e), found=None, timings={})
        return False
    if not has_segment_offer(seg_name):
        JOB.update(error=f"No offer saved for '{seg_name}'. Write one under 'Offer' in the sidebar "
                         "and click Save first, so the messages pitch YOUR product to this audience.",
                   found=None, timings={})
        return False
    JOB.update(running=True, stage="Finding businesses", done=0, total=0, found=None, drafted=0, error="", timings={})

    def run():
        try:
            if clear_first:
                reset_queue()
            t = time.time()
            JOB["found"] = cmd_source(place, segment, "", int(limit) * 2)
            JOB["timings"]["find on map"] = round(time.time() - t, 1)
            JOB["stage"] = "Researching + drafting"
            LAST_AI_ERROR["msg"] = ""
            before = len(load())
            JOB["timings"].update(cmd_generate(progress=lambda d, tot: JOB.update(done=d, total=tot), max_new=int(limit)) or {})
            if JOB["drafted"] and len(load()) == before and LAST_AI_ERROR["msg"]:
                JOB["error"] = "AI drafting failed: " + LAST_AI_ERROR["msg"]
        except Exception as e:
            JOB["error"] = str(e)
        finally:
            JOB["running"] = False

    threading.Thread(target=run, daemon=True).start()
    return True


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


def stats_by(rows, key):
    out = {}
    for r in rows:
        if not r["sent_date"]:
            continue
        g = out.setdefault(r.get(key) or "-", {"sent": 0, "replies": 0})
        g["sent"] += 1
        g["replies"] += r["status"] in ("replied", "call", "won", "lost")
    return [{key: k, "sent": v["sent"], "replies": v["replies"],
             "reply %": round(100 * v["replies"] / v["sent"], 1)} for k, v in sorted(out.items())]


def cmd_stats():
    rows = load()
    touched = [r for r in rows if r["sent_date"]]
    replied = [r for r in touched if r["status"] in ("replied", "call", "won", "lost")]
    calls = [r for r in touched if r["status"] in ("call", "won")]
    won = [r for r in touched if r["status"] == "won"]
    n = len(touched) or 1
    print(f"prospects {len(rows)} | contacted {len(touched)} | replies {len(replied)} "
          f"({100 * len(replied) / n:.1f}%) | calls {len(calls)} | won {len(won)}")
    for key in ("variant", "segment"):
        for row in stats_by(rows, key):
            print(row)


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
    elif a[:1] == ["check"]: print("\n".join(check_keys()))
    elif a[:1] == ["segments"]: print("\n".join(SEGMENTS))
    else: print(__doc__)
