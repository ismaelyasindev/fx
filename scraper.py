"""
scraper.py — Forex Factory calendar scraper + OANDA price reaction engine
Pulls news events, matches to your watchlist, fetches GBP/USD reactions from OANDA.
"""

import httpx
import asyncio
import math
import statistics
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
import re
import json

from db import get_db, check_connection

_ff_scraper = None


def _get_ff_scraper():
    global _ff_scraper
    if _ff_scraper is None:
        import cloudscraper
        _ff_scraper = cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "darwin", "desktop": True}
        )
    return _ff_scraper


def _fetch_ff_html(url: str) -> str | None:
    """Fetch FF calendar HTML. FF uses Cloudflare — plain httpx gets 403."""
    try:
        r = _get_ff_scraper().get(url, timeout=45)
        if not r.ok or "Just a moment" in r.text:
            print(f"  FF returned {r.status_code} for {url}")
            return None
        return r.text
    except Exception as e:
        print(f"  FF fetch error for {url}: {e}")
        return None

# ── NEWS EVENT MAP ────────────────────────────────────────
# Maps your internal name → (forex_factory_keyword, country, impact_colours)
# impact: 'red' = high, 'orange' = medium
# We match FF event titles using keyword (case-insensitive contains match)

NEWS_MAP = [
    # YOUR NAME                          FF KEYWORD                          COUNTRY   MIN_IMPACT  EXCLUSIONS                                                                         VALUE_TYPE
    # value_type: "percent"      – proportional 5% rule (rates, indices, % changes)
    #             "count_large"  – proportional 5% rule (large always-positive counts)
    #             "count_signed" – absolute stdev-based rule (can cross zero, e.g. Claimant Count)
    ("Non Farm Payroll US",              "Non-Farm Employment Change",        "USD",    "red",      ["ADP"],                                                                           "count_large"),
    ("Unemployment US",                  "Unemployment Rate",                 "USD",    "red",      ["Claims", "ADP"],                                                                 "percent"),
    ("ISM Services US",                  "ISM Services PMI",                  "USD",    "orange",   [],                                                                                "percent"),
    ("ISM Manufacturing US",             "ISM Manufacturing PMI",             "USD",    "orange",   [],                                                                                "percent"),
    ("Retail UK",                        "Retail Sales",                      "GBP",    "orange",   ["Core"],                                                                          "percent"),
    ("GDP UK",                           "GDP",                               "GBP",    "orange",   ["Price Index", "Prelim", "Final"],                                                "percent"),
    ("GDP US",                           "GDP",                               "USD",    "red",      ["Price Index", "Prelim", "Final"],                                                "percent"),
    ("Existing Home Sales US",           "Existing Home Sales",               "USD",    "orange",   [],                                                                                "count_large"),
    ("Core Inflation MoM US",            "Core CPI",                         "USD",    "red",      ["PPI"],                                                                           "percent"),
    ("Inflation Rate YoY US",            "CPI y/y",                          "USD",    "red",      ["Core", "PPI"],                                                                   "percent"),
    ("Inflation Rate YoY UK",            "CPI y/y",                          "GBP",    "orange",   ["Core", "PPI"],                                                                   "percent"),
    ("FOMC US",                          "FOMC",                              "USD",    "orange",   ["Member"],                                                                        "percent"),
    ("Building Permits US",              "Building Permits",                  "USD",    "orange",   [],                                                                                "count_large"),
    ("Housing Starts US",                "Housing Starts",                    "USD",    "orange",   [],                                                                                "count_large"),
    ("S&P Global Manufacturing PMI UK",  "Manufacturing PMI",                 "GBP",    "orange",   [],                                                                                "percent"),
    ("S&P Global Services PMI UK",       "Services PMI",                      "GBP",    "orange",   [],                                                                                "percent"),
    ("BOE Interest Rate US",             "BOE",                               "GBP",    "red",      ["Speech", "Minutes", "Testimony", "Gov ", "Report", "Summary", "Letter", "Survey", "Bulletin"], "percent"),
    ("Fed Chair Powell Speech US",       "Fed Chair Powell",                  "USD",    "red",      ["Minutes"],                                                                       "percent"),
    ("Personal Spending US",             "Personal Spending",                 "USD",    "orange",   [],                                                                                "percent"),
    ("Core PCE MoM US",                  "Core PCE",                         "USD",    "orange",   [],                                                                                "percent"),
    ("PPI US",                           "PPI",                               "USD",    "orange",   ["Core"],                                                                          "percent"),
    ("PPI MoM US",                       "PPI m/m",                          "USD",    "orange",   ["Core"],                                                                          "percent"),
    ("Michigan Consumer Sentiment US",   "Michigan",                          "USD",    "orange",   [],                                                                                "percent"),
    ("Retail US",                        "Retail Sales",                      "USD",    "orange",   ["Core"],                                                                          "percent"),
    ("Durable Goods US",                 "Durable Goods",                     "USD",    "orange",   [],                                                                                "percent"),
    ("JOLTs Job Openings US",            "JOLTS",                             "USD",    "orange",   [],                                                                                "count_large"),
    ("Unemployment UK",                  "Claimant Count",                    "GBP",    "orange",   [],                                                                                "count_signed"),
    ("Average Earnings UK",              "Average Earnings",                  "GBP",    "orange",   [],                                                                                "percent"),
]

# Lookup from event name → value_type (built once at module load)
_VALUE_TYPE: dict = {row[0]: row[5] for row in NEWS_MAP}

# Per-event absolute threshold cache — populated once at start of run_sync/scrape_upcoming
_scrape_thresholds: dict = {}

# ── DB HELPERS ────────────────────────────────────────────
def ensure_tables():
    """Ensure event_groups table and new columns on price_reactions exist."""
    try:
        conn = get_db()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS event_groups (
                id BIGSERIAL PRIMARY KEY,
                group_key TEXT NOT NULL,
                event_date TEXT NOT NULL,
                event_time TEXT,
                member_names TEXT NOT NULL,
                scenario TEXT,
                price_reaction_id INTEGER,
                created_at TEXT DEFAULT to_char(NOW(), 'YYYY-MM-DD"T"HH24:MI:SS'),
                UNIQUE(group_key, event_date)
            )
        """)
        # Section 4: signed pip columns and post-entry metrics
        # Section 6: is_co_released flag to exclude from direction stats
        new_pr_cols = [
            "ALTER TABLE price_reactions ADD COLUMN IF NOT EXISTS signed_pip_5m REAL",
            "ALTER TABLE price_reactions ADD COLUMN IF NOT EXISTS signed_pip_15m REAL",
            "ALTER TABLE price_reactions ADD COLUMN IF NOT EXISTS signed_pip_30m REAL",
            "ALTER TABLE price_reactions ADD COLUMN IF NOT EXISTS signed_pip_60m REAL",
            "ALTER TABLE price_reactions ADD COLUMN IF NOT EXISTS post_entry_5_30 REAL",
            "ALTER TABLE price_reactions ADD COLUMN IF NOT EXISTS post_entry_5_60 REAL",
            "ALTER TABLE price_reactions ADD COLUMN IF NOT EXISTS max_adverse_5_30 REAL",
            "ALTER TABLE price_reactions ADD COLUMN IF NOT EXISTS is_co_released BOOLEAN DEFAULT FALSE",
            "ALTER TABLE news_events ADD COLUMN IF NOT EXISTS classification_method TEXT",
        ]
        for sql in new_pr_cols:
            try:
                conn.execute(sql)
            except Exception as e:
                print(f"  Column migration skipped: {e}")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"  ensure_tables error: {e}")
    check_connection()


def build_event_groups() -> int:
    """Populate event_groups from pairs of news_events sharing the same date+time slot.

    Returns the number of group-occurrence rows inserted or updated.
    Wrapped in try/except so a failure never breaks sync.
    """
    try:
        conn = get_db()

        # Find (event_date, event_time) slots that have 2+ watchlist events
        slot_rows = conn.execute("""
            SELECT event_date, event_time
            FROM news_events
            WHERE your_name IS NOT NULL AND your_name != ''
              AND event_time IS NOT NULL AND event_time != ''
            GROUP BY event_date, event_time
            HAVING COUNT(*) >= 2
        """).fetchall()

        inserted = 0
        for slot in slot_rows:
            sd = dict(slot)
            event_date = sd["event_date"]
            event_time = sd["event_time"]

            # Fetch all members for this slot, sorted alphabetically by your_name
            members = [dict(m) for m in conn.execute("""
                SELECT id, your_name, beat_miss
                FROM news_events
                WHERE event_date = %s AND event_time = %s
                  AND your_name IS NOT NULL AND your_name != ''
                ORDER BY your_name ASC
            """, (event_date, event_time)).fetchall()]

            if len(members) < 2:
                continue

            # Skip if any member has unknown/missing beat_miss
            if any(m.get("beat_miss") in (None, "", "unknown") for m in members):
                continue

            group_key    = "|".join(m["your_name"] for m in members)  # sorted alphabetically
            scenario     = "|".join(m["beat_miss"]  for m in members)  # same order

            # price_reaction_id = the reaction row for the lowest-id member
            min_id = min(m["id"] for m in members)
            pr_row = conn.execute(
                "SELECT id FROM price_reactions WHERE news_event_id = %s LIMIT 1",
                (min_id,),
            ).fetchone()
            if not pr_row:
                continue
            price_reaction_id = dict(pr_row)["id"]

            try:
                conn.execute("""
                    INSERT INTO event_groups
                        (group_key, event_date, event_time, member_names, scenario, price_reaction_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (group_key, event_date) DO UPDATE SET
                        scenario           = EXCLUDED.scenario,
                        price_reaction_id  = EXCLUDED.price_reaction_id
                """, (group_key, event_date, event_time, group_key, scenario, price_reaction_id))
                # Mark all members' price_reactions as co-released so direction stats
                # exclude them from per-event consistency calculations
                for m in members:
                    try:
                        conn.execute(
                            "UPDATE price_reactions SET is_co_released = TRUE WHERE news_event_id = %s",
                            (m["id"],)
                        )
                    except Exception:
                        pass
                inserted += 1
            except Exception as e:
                print(f"  event_groups insert {group_key} {event_date}: {e}")

        conn.commit()
        conn.close()
        print(f"  event_groups: built/updated {inserted} group entries")
        return inserted

    except Exception as e:
        print(f"  build_event_groups error: {e}")
        return 0


def get_group_analysis(group_key: str) -> dict:
    """Return co-occurrence statistics for a group identified by pipe-joined sorted names."""
    conn = get_db()
    try:
        members = sorted(group_key.split("|"))

        rows = [dict(r) for r in conn.execute("""
            SELECT eg.scenario,
                   pr.pip_5m, pr.pip_15m, pr.pip_30m, pr.direction_5m
            FROM event_groups eg
            LEFT JOIN price_reactions pr ON pr.id = eg.price_reaction_id
            WHERE eg.group_key = %s
        """, (group_key,)).fetchall()]

        total_occurrences = len(rows)

        # Aggregate by scenario
        sc_data: dict[str, dict] = {}
        for r in rows:
            sc = r.get("scenario")
            if not sc:
                continue
            if sc not in sc_data:
                sc_data[sc] = {"count": 0, "pip5": [], "pip15": [], "pip30": [], "up": 0, "down": 0}
            d = sc_data[sc]
            d["count"] += 1
            if r.get("pip_5m") is not None:
                d["pip5"].append(r["pip_5m"])
            if r.get("pip_15m") is not None:
                d["pip15"].append(r["pip_15m"])
            if r.get("pip_30m") is not None:
                d["pip30"].append(r["pip_30m"])
            if r.get("direction_5m") == "up":
                d["up"] += 1
            elif r.get("direction_5m") == "down":
                d["down"] += 1

        def _avg(xs):
            return round(sum(xs) / len(xs), 1) if xs else None

        scenarios = []
        for sc, d in sorted(sc_data.items(), key=lambda x: -x[1]["count"]):
            sc_parts = sc.split("|")
            label_parts = [
                f"{members[i].replace(' US','').replace(' UK','')} {(sc_parts[i] if i < len(sc_parts) else '?').upper()}"
                for i in range(len(members))
            ]
            label = " + ".join(label_parts)
            total_dir = d["up"] + d["down"]
            direction = "up" if d["up"] >= d["down"] else "down"
            consistency = round(max(d["up"], d["down"]) / total_dir * 100) if total_dir > 0 else 0
            scenarios.append({
                "scenario":    sc,
                "label":       label,
                "count":       d["count"],
                "avg_pip_5m":  _avg(d["pip5"]),
                "avg_pip_15m": _avg(d["pip15"]),
                "avg_pip_30m": _avg(d["pip30"]),
                "up_count":    d["up"],
                "down_count":  d["down"],
                "direction":   direction,
                "consistency": consistency,
            })

        conn.close()
        return {
            "group_key":          group_key,
            "members":            members,
            "total_occurrences":  total_occurrences,
            "scenarios":          scenarios,
        }

    except Exception as e:
        print(f"  get_group_analysis error: {e}")
        conn.close()
        return {"group_key": group_key, "members": [], "total_occurrences": 0, "scenarios": [], "error": str(e)}

# ── FOREX FACTORY SCRAPER ─────────────────────────────────
def build_ff_url(year: int, month: int) -> str:
    dt = datetime(year, month, 1)
    return f"https://www.forexfactory.com/calendar?month={dt.strftime('%b').lower()}.{year}"

def parse_impact(td) -> str:
    """Extract impact colour from FF impact cell."""
    if not td:
        return ""
    span = td.find("span", class_=re.compile(r"impact|ff-impact"))
    cls = " ".join(span.get("class", [])) if span else td.get("class", [])
    if isinstance(cls, list):
        cls = " ".join(cls)
    cls = cls.lower()
    if "impact-red" in cls or "impact--red" in cls or "high" in cls:
        return "red"
    if "impact-ora" in cls or "impact--orange" in cls or "medium" in cls:
        return "orange"
    if "impact-yel" in cls or "impact--yellow" in cls or "low" in cls:
        return "yellow"
    return ""

def _impact_ok(actual_impact: str, min_impact: str) -> bool:
    """Red always qualifies. Orange only qualifies if the requirement is orange."""
    if actual_impact == "red":
        return True
    return actual_impact == "orange" and min_impact == "orange"


def match_event(ff_title: str, ff_currency: str, ff_impact: str = "") -> dict | None:
    """Match a FF event row to one of our tracked events.

    Checks: currency match, keyword substring, exclusion substrings, impact guard.
    ff_impact is optional; when empty the impact guard is skipped (backwards compat).
    """
    ff_title_lower = ff_title.lower()
    for (your_name, keyword, country, min_impact, exclusions, value_type) in NEWS_MAP:
        if ff_currency.upper() != country:
            continue
        if keyword.lower() not in ff_title_lower:
            continue
        # Exclusion guard: any disqualifying substring → skip
        if any(excl.lower() in ff_title_lower for excl in exclusions):
            continue
        # Impact guard: downgrade (orange where red expected) is rejected
        if ff_impact and not _impact_ok(ff_impact, min_impact):
            continue
        return {"your_name": your_name, "country": country, "min_impact": min_impact, "value_type": value_type}
    return None

def _clean_numeric(v: str) -> float:
    """Strip %, K, M suffixes and return float. Raises on failure."""
    v = v.strip().replace("%", "").replace("K", "000").replace("M", "000000").replace(",", "")
    return float(v)


def get_abs_thresholds() -> dict:
    """Return {event_name: abs_threshold} for all tracked events.

    abs_threshold = stdev(actual_numeric_values) * 0.1.
    Falls back to 1.0 if fewer than 10 valid rows or stdev is zero.
    """
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT your_name, actual FROM news_events
            WHERE actual IS NOT NULL AND actual NOT IN ('', '-', 'N/A')
            ORDER BY your_name
        """).fetchall()
    except Exception:
        conn.close()
        return {}
    conn.close()

    from collections import defaultdict
    event_actuals: dict = defaultdict(list)
    for r in rows:
        d = dict(r)
        try:
            event_actuals[d["your_name"]].append(_clean_numeric(d["actual"]))
        except Exception:
            pass

    thresholds = {}
    for name, vals in event_actuals.items():
        if len(vals) < 10:
            thresholds[name] = 1.0
        else:
            try:
                sd = statistics.stdev(vals)
                thresholds[name] = round(sd * 0.1, 4) if sd > 0 else 1.0
            except Exception:
                thresholds[name] = 1.0
    return thresholds


def classify_beat_miss(actual: str, forecast: str,
                       abs_threshold: float = 1.0,
                       value_type: str = "percent") -> tuple:
    """Return (beat_miss, classification_method).

    classification_method is 'absolute' or 'proportional'.

    Method is chosen from value_type (set per-event in NEWS_MAP):
      "count_signed" → ABSOLUTE threshold (stdev * 0.1, fallback 1.0)
                       Only for series that genuinely cross zero (e.g. Claimant Count Change).
      "percent" or "count_large" → PROPORTIONAL 5% rule.

    Opposite-signs is kept as a secondary safety trigger for absolute mode,
    catching any edge case where the series unexpectedly crosses zero.
    """
    try:
        if not actual or not forecast or actual in ("-", "N/A", "") or forecast in ("-", "N/A", ""):
            return "unknown", "proportional"
        a = _clean_numeric(actual)
        f = _clean_numeric(forecast)
        diff = a - f

        # Primary: use value_type; secondary safety: opposite signs also use absolute
        opposite_signs = (f > 0 > a) or (f < 0 < a)
        use_absolute = (value_type == "count_signed") or opposite_signs

        if use_absolute:
            if diff > abs_threshold:
                return "beat", "absolute"
            elif diff < -abs_threshold:
                return "miss", "absolute"
            else:
                return "inline", "absolute"
        else:
            pct = abs(diff) / abs(f) * 100 if abs(f) > 0 else 0.0
            if pct <= 5.0:
                return "inline", "proportional"
            return ("beat", "proportional") if diff > 0 else ("miss", "proportional")
    except Exception:
        return "unknown", "proportional"


def reclassify_existing_rows() -> dict:
    """Re-run beat/miss classification on all stored news_events rows.

    Uses the corrected hybrid classifier. Writes beat_miss and
    classification_method. Returns {"changed": N, "total": M}.
    """
    thresholds = get_abs_thresholds()
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT id, your_name, actual, forecast, beat_miss
            FROM news_events
            WHERE actual IS NOT NULL AND actual NOT IN ('', '-', 'N/A')
            AND forecast IS NOT NULL AND forecast NOT IN ('', '-', 'N/A')
        """).fetchall()
    except Exception as e:
        conn.close()
        print(f"  reclassify_existing_rows query error: {e}")
        return {"changed": 0, "total": 0}

    changed = 0
    for row in rows:
        d = dict(row)
        threshold = thresholds.get(d["your_name"], 1.0)
        vtype = _VALUE_TYPE.get(d["your_name"], "percent")
        new_bm, method = classify_beat_miss(d["actual"], d["forecast"], threshold, vtype)
        if new_bm != (d["beat_miss"] or "unknown"):
            changed += 1
        try:
            conn.execute(
                "UPDATE news_events SET beat_miss = %s, classification_method = %s WHERE id = %s",
                (new_bm, method, d["id"])
            )
        except Exception as e:
            print(f"  reclassify row {d['id']} error: {e}")

    conn.commit()
    conn.close()
    print(f"  reclassify_existing_rows: {changed} rows changed out of {len(rows)}")
    return {"changed": changed, "total": len(rows)}

async def scrape_month(year: int, month: int, client: httpx.AsyncClient, watchlist_only: bool = True) -> list:
    """Scrape one month of FF calendar.

    watchlist_only=True (sync): only NEWS_MAP matches — keeps analysis/history clean.
    watchlist_only=False (This Week): all USD/GBP red/orange events, mapped when possible.
    """
    url = build_ff_url(year, month)
    events = []
    try:
        html = await asyncio.to_thread(_fetch_ff_html, url)
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", class_=re.compile(r"calendar"))
        if not table:
            # Try alternative structure
            table = soup.find("table")
        if not table:
            print(f"  No calendar table found for {year}-{month:02d}")
            return []

        current_date = None
        current_time = None

        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if not cells:
                continue

            # Date cell
            date_cell = row.find("td", class_=re.compile(r"date|calendar__date"))
            if date_cell and date_cell.get_text(strip=True):
                raw_date = date_cell.get_text(strip=True)
                # FF format varies: "Wed Jan 20", "Jan 20", or "WedJan 20" (no space)
                inner = date_cell.find("span", class_="date")
                if inner:
                    day_span = inner.find("span")
                    if day_span:
                        raw_date = day_span.get_text(strip=True)
                    else:
                        raw_date = re.sub(r"^[A-Za-z]{3}\s*", "", inner.get_text(strip=True))
                else:
                    raw_date = re.sub(r"^[A-Za-z]{3}", "", raw_date).strip()
                try:
                    for fmt in ["%a %b %d", "%b %d", "%A %b %d"]:
                        try:
                            parsed = datetime.strptime(f"{raw_date} {year}", f"{fmt} %Y")
                            current_date = parsed.strftime("%Y-%m-%d")
                            break
                        except:
                            continue
                except:
                    pass

            # Time cell
            time_cell = row.find("td", class_=re.compile(r"time|calendar__time"))
            if time_cell:
                t = time_cell.get_text(strip=True)
                if t and t not in ("", "All Day", "Tentative"):
                    current_time = t

            # Currency cell
            currency_cell = row.find("td", class_=re.compile(r"currency|calendar__currency"))
            if not currency_cell:
                continue
            currency = currency_cell.get_text(strip=True).upper()
            if currency not in ("USD", "GBP"):
                continue

            # Impact cell
            impact_cell = row.find("td", class_=re.compile(r"impact|calendar__impact"))
            impact = parse_impact(impact_cell) if impact_cell else ""
            if impact not in ("red", "orange"):
                continue

            # Event name cell
            event_cell = row.find("td", class_=re.compile(r"event|calendar__event"))
            if not event_cell:
                continue
            ff_title = event_cell.get_text(strip=True)

            # Data cells
            def get_cell_text(cls):
                cell = row.find("td", class_=re.compile(cls))
                return cell.get_text(strip=True) if cell else ""

            actual = get_cell_text(r"actual|calendar__actual")
            forecast = get_cell_text(r"forecast|calendar__forecast")
            previous = get_cell_text(r"previous|calendar__previous")

            # Match to our watchlist (your notes / ratings / brief pipeline)
            match = match_event(ff_title, currency, impact)
            if watchlist_only and not match:
                continue

            if not current_date:
                continue

            your_name_for_threshold = match["your_name"] if match else None
            threshold = (_scrape_thresholds.get(your_name_for_threshold, 1.0)
                         if your_name_for_threshold else 1.0)
            vtype = match["value_type"] if match else "percent"
            beat_miss, cls_method = classify_beat_miss(actual, forecast, threshold, vtype)

            events.append({
                "your_name": your_name_for_threshold,
                "ff_title": ff_title,
                "country": match["country"] if match else currency,
                "event_date": current_date,
                "event_time": current_time or "",
                "previous": previous,
                "forecast": forecast,
                "actual": actual,
                "impact": impact,
                "beat_miss": beat_miss,
                "classification_method": cls_method,
                "on_watchlist": bool(match),
            })

    except Exception as e:
        print(f"  Error scraping {year}-{month:02d}: {e}")

    return events

# ── OANDA PRICE REACTIONS ─────────────────────────────────
def get_oanda_config() -> dict:
    conn = get_db()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    cfg = {r["key"]: r["value"] for r in rows}
    return cfg

def parse_event_datetime(event_date: str, event_time: str) -> datetime | None:
    """Convert event date + time string to UTC datetime."""
    try:
        if not event_time or event_time in ("", "Tentative", "All Day"):
            # Default to 13:30 UTC (typical US news time)
            dt_str = f"{event_date} 13:30"
        else:
            # FF times are in US Eastern — convert to UTC (ET + 5h in winter, +4h in summer)
            # Use approximate: most major news is 8:30am ET = 13:30 UTC
            # We'll store the raw time and let OANDA window handle it
            dt_str = f"{event_date} {event_time}"

        # Try parsing various formats
        for fmt in ["%Y-%m-%d %I:%M%p", "%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p"]:
            try:
                return datetime.strptime(dt_str, fmt).replace(tzinfo=timezone.utc)
            except:
                continue
        # Fallback: just use date at 13:30 UTC
        return datetime.strptime(f"{event_date} 13:30", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
    except:
        return None

async def fetch_price_reaction(event: dict, client: httpx.AsyncClient, cfg: dict) -> dict | None:
    """Fetch GBP/USD candles around an event and compute pip moves."""
    token = cfg.get("token", "")
    env = cfg.get("env", "practice")
    if not token:
        return None

    base_url = "https://api-fxtrade.oanda.com" if env == "live" else "https://api-fxpractice.oanda.com"
    event_dt = parse_event_datetime(event["event_date"], event["event_time"])
    if not event_dt:
        return None

    # Fetch 5M candles: 15 min before to 75 min after
    from_dt = event_dt - timedelta(minutes=15)
    to_dt = event_dt + timedelta(minutes=75)

    from_str = from_dt.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
    to_str = to_dt.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")

    try:
        r = await client.get(
            f"{base_url}/v3/instruments/GBP_USD/candles",
            params={"from": from_str, "to": to_str, "granularity": "M5", "price": "M"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15
        )
        if not r.is_success:
            return None

        candles = r.json().get("candles", [])
        if len(candles) < 2:
            return None

        # Find the candle at/just after event time
        def candle_close(idx):
            if 0 <= idx < len(candles):
                return float(candles[idx]["mid"]["c"])
            return None

        # Candles start 15 min before event, so the event candle is at index 3
        event_idx = 3

        # Open price = price at the exact moment the news releases
        # candles[event_idx]["mid"]["o"] is the open of the 5M candle that starts
        # at event_dt — the last traded price before the data number hit
        open_price = float(candles[event_idx]["mid"]["o"])

        # Price at 5, 15, 30, 60 min after the event
        p5 = candle_close(event_idx + 1)   # 5 min after
        p15 = candle_close(event_idx + 3)  # 15 min after
        p30 = candle_close(event_idx + 6)  # 30 min after
        p60 = candle_close(event_idx + 12) # 60 min after

        def abs_pips(a, b):
            if a is None or b is None:
                return None
            return round(abs(a - b) * 10000, 1)

        def signed_pips(a, b):
            """Positive = price rose."""
            if a is None or b is None:
                return None
            return round((b - a) * 10000, 1)

        def direction(a, b):
            if a is None or b is None:
                return None
            return "up" if b > a else "down"

        sp5  = signed_pips(open_price, p5)
        sp15 = signed_pips(open_price, p15)
        sp30 = signed_pips(open_price, p30)
        sp60 = signed_pips(open_price, p60)

        # post_entry_5_30 / post_entry_5_60: signed move from 5m entry price
        # Positive always means "continued in the 5m direction"
        post_entry_5_30 = post_entry_5_60 = None
        if p5 is not None and p30 is not None:
            raw = (p30 - p5) * 10000
            post_entry_5_30 = round(raw if (sp5 or 0) >= 0 else -raw, 1)
        if p5 is not None and p60 is not None:
            raw = (p60 - p5) * 10000
            post_entry_5_60 = round(raw if (sp5 or 0) >= 0 else -raw, 1)

        # max_adverse_5_30: worst move against the 5m direction between the
        # entry candle (event_idx+1) and 30m candle (event_idx+6), as a
        # positive pip number. Uses candle highs/lows from the existing response.
        max_adverse_5_30 = None
        if p5 is not None:
            going_up = (sp5 or 0) >= 0
            worst = 0.0
            for i in range(event_idx + 1, min(event_idx + 7, len(candles))):
                try:
                    if going_up:
                        # Adverse: low goes below p5
                        low = float(candles[i]["mid"]["l"])
                        diff = max(0.0, p5 - low)
                    else:
                        # Adverse: high goes above p5
                        high = float(candles[i]["mid"]["h"])
                        diff = max(0.0, high - p5)
                    if diff > worst:
                        worst = diff
                except Exception:
                    pass
            max_adverse_5_30 = round(worst * 10000, 1)

        return {
            "open_price": open_price,
            "price_5m": p5,
            "price_15m": p15,
            "price_30m": p30,
            "price_60m": p60,
            "pip_5m": abs_pips(open_price, p5),
            "pip_15m": abs_pips(open_price, p15),
            "pip_30m": abs_pips(open_price, p30),
            "pip_60m": abs_pips(open_price, p60),
            "direction_5m": direction(open_price, p5),
            "direction_15m": direction(open_price, p15),
            "signed_pip_5m": sp5,
            "signed_pip_15m": sp15,
            "signed_pip_30m": sp30,
            "signed_pip_60m": sp60,
            "post_entry_5_30": post_entry_5_30,
            "post_entry_5_60": post_entry_5_60,
            "max_adverse_5_30": max_adverse_5_30,
        }
    except Exception as e:
        print(f"  OANDA error for {event['your_name']} {event['event_date']}: {e}")
        return None

# ── SAVE TO DB ────────────────────────────────────────────
def save_events(events: list) -> tuple[int, int]:
    """Save news events to DB. Returns (total, new)."""
    conn = get_db()
    c = conn.cursor()
    new_count = 0
    for ev in events:
        try:
            cls_method = ev.get("classification_method", "proportional")
            c.execute("""
                INSERT INTO news_events
                (your_name, ff_title, country, event_date, event_time, previous, forecast, actual, impact, beat_miss, classification_method)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (your_name, event_date, event_time) DO UPDATE SET
                    ff_title = EXCLUDED.ff_title,
                    previous = EXCLUDED.previous,
                    forecast = EXCLUDED.forecast,
                    impact = EXCLUDED.impact
                WHERE news_events.actual = '' OR news_events.actual IS NULL
            """, (ev["your_name"], ev["ff_title"], ev["country"], ev["event_date"],
                  ev["event_time"], ev["previous"], ev["forecast"], ev["actual"],
                  ev["impact"], ev["beat_miss"], cls_method))
            if c.rowcount > 0:
                new_count += 1
            else:
                c.execute("""
                    UPDATE news_events SET actual=%s, beat_miss=%s, classification_method=%s
                    WHERE your_name=%s AND event_date=%s AND event_time=%s
                    AND (actual='' OR actual IS NULL)
                """, (ev["actual"], ev["beat_miss"], cls_method,
                      ev["your_name"], ev["event_date"], ev["event_time"]))
        except Exception as e:
            print(f"  DB error saving event: {e}")
    conn.commit()
    conn.close()
    return len(events), new_count

def save_reaction(event_id: int, reaction: dict):
    conn = get_db()
    conn.execute("""
        INSERT INTO price_reactions
        (news_event_id, pip_5m, pip_15m, pip_30m, pip_60m, direction_5m, direction_15m,
         open_price, price_5m, price_15m, price_30m, price_60m,
         signed_pip_5m, signed_pip_15m, signed_pip_30m, signed_pip_60m,
         post_entry_5_30, post_entry_5_60, max_adverse_5_30)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (news_event_id) DO UPDATE SET
            pip_5m=excluded.pip_5m, pip_15m=excluded.pip_15m,
            pip_30m=excluded.pip_30m, pip_60m=excluded.pip_60m,
            direction_5m=excluded.direction_5m, direction_15m=excluded.direction_15m,
            open_price=excluded.open_price, price_5m=excluded.price_5m,
            price_15m=excluded.price_15m, price_30m=excluded.price_30m,
            price_60m=excluded.price_60m,
            signed_pip_5m=excluded.signed_pip_5m, signed_pip_15m=excluded.signed_pip_15m,
            signed_pip_30m=excluded.signed_pip_30m, signed_pip_60m=excluded.signed_pip_60m,
            post_entry_5_30=excluded.post_entry_5_30, post_entry_5_60=excluded.post_entry_5_60,
            max_adverse_5_30=excluded.max_adverse_5_30,
            fetched_at=NOW()
    """, (event_id,
          reaction.get("pip_5m"), reaction.get("pip_15m"),
          reaction.get("pip_30m"), reaction.get("pip_60m"),
          reaction.get("direction_5m"), reaction.get("direction_15m"),
          reaction.get("open_price"), reaction.get("price_5m"),
          reaction.get("price_15m"), reaction.get("price_30m"), reaction.get("price_60m"),
          reaction.get("signed_pip_5m"), reaction.get("signed_pip_15m"),
          reaction.get("signed_pip_30m"), reaction.get("signed_pip_60m"),
          reaction.get("post_entry_5_30"), reaction.get("post_entry_5_60"),
          reaction.get("max_adverse_5_30")))
    conn.commit()
    conn.close()

def get_events_without_reactions() -> list:
    conn = get_db()
    rows = conn.execute("""
        SELECT ne.* FROM news_events ne
        LEFT JOIN price_reactions pr ON pr.news_event_id = ne.id
        WHERE pr.id IS NULL
        AND ne.actual != '' AND ne.actual IS NOT NULL
        AND ne.event_date <= CURRENT_DATE::TEXT
        ORDER BY ne.event_date DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_events_without_reactions() -> int:
    """Count events that need a backfill pass:
    - events with no price_reactions row at all, AND
    - events with a reaction row but NULL post_entry_5_30 (fetched before new columns existed).
    """
    conn = get_db()
    try:
        r1 = conn.execute("""
            SELECT COUNT(*) AS cnt FROM news_events ne
            LEFT JOIN price_reactions pr ON pr.news_event_id = ne.id
            WHERE pr.id IS NULL
            AND ne.actual != '' AND ne.actual IS NOT NULL
            AND ne.event_date <= CURRENT_DATE::TEXT
        """).fetchone()
        r2 = conn.execute("""
            SELECT COUNT(*) AS cnt FROM news_events ne
            JOIN price_reactions pr ON pr.news_event_id = ne.id
            WHERE pr.post_entry_5_30 IS NULL
            AND ne.actual != '' AND ne.actual IS NOT NULL
            AND ne.event_date <= CURRENT_DATE::TEXT
        """).fetchone()
        conn.close()
        return (dict(r1)["cnt"] or 0) + (dict(r2)["cnt"] or 0)
    except Exception as e:
        conn.close()
        print(f"  count_events_without_reactions error: {e}")
        return 0


async def backfill_missing_reactions() -> int:
    """Fetch/re-fetch OANDA reactions for:

    Pass 1 — events with no price_reactions row at all.
    Pass 2 — events whose reaction row has NULL post_entry_5_30 (fetched before the
              post-entry columns were added in Jul 2026; save_reaction overwrites with new data).

    Called at the end of run_sync() and on app startup. Returns reactions filled/upgraded.
    """
    cfg = get_oanda_config()

    # Pass 1: completely missing reactions
    events_missing = get_events_without_reactions()

    # Pass 2: existing reactions that pre-date the post-entry columns
    try:
        conn = get_db()
        rows = conn.execute("""
            SELECT ne.* FROM news_events ne
            JOIN price_reactions pr ON pr.news_event_id = ne.id
            WHERE pr.post_entry_5_30 IS NULL
            AND ne.actual != '' AND ne.actual IS NOT NULL
            AND ne.event_date <= CURRENT_DATE::TEXT
            ORDER BY ne.event_date DESC
        """).fetchall()
        conn.close()
        events_upgrade = [dict(r) for r in rows]
    except Exception as e:
        print(f"  backfill upgrade query error: {e}")
        events_upgrade = []

    all_events = events_missing + events_upgrade
    if not all_events:
        return 0

    filled = 0
    async with httpx.AsyncClient() as client:
        for event in all_events:
            try:
                reaction = await fetch_price_reaction(event, client, cfg)
                if reaction:
                    save_reaction(event["id"], reaction)
                    filled += 1
            except Exception as e:
                print(f"  backfill error for {event.get('your_name')} {event.get('event_date')}: {e}")
            await asyncio.sleep(0.3)
    print(f"  backfill: {filled} reactions filled/upgraded "
          f"({len(events_missing)} missing + {len(events_upgrade)} needing post-entry upgrade)")
    return filled


def audit_matches() -> list:
    """For each watchlist event name, return the distinct ff_title values currently
    stored in news_events under that name, with a count.  Flags names with >1 title."""
    conn = get_db()
    rows = conn.execute("""
        SELECT your_name, ff_title, COUNT(*) AS cnt
        FROM news_events
        WHERE your_name IS NOT NULL AND your_name != ''
        GROUP BY your_name, ff_title
        ORDER BY your_name ASC, cnt DESC
    """).fetchall()
    conn.close()

    groups: dict[str, list] = {}
    for r in rows:
        d = dict(r)
        name = d["your_name"]
        groups.setdefault(name, []).append({"ff_title": d["ff_title"], "count": d["cnt"]})

    result = []
    for name, titles in groups.items():
        result.append({
            "your_name": name,
            "titles": titles,
            "multi_title": len(titles) > 1,
        })
    return result


def audit_reclassify() -> list:
    """Return news_events rows whose ff_title no longer matches their stored your_name
    under the current NEWS_MAP (with exclusion lists and impact guards)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, your_name, ff_title, impact, country FROM news_events WHERE your_name IS NOT NULL AND your_name != ''"
    ).fetchall()
    conn.close()

    to_delete = []
    for r in rows:
        d = dict(r)
        new_match = match_event(d["ff_title"] or "", d["country"] or "USD", d["impact"] or "")
        # Delete if no match at all, or matched to a different event
        if not new_match or new_match["your_name"] != d["your_name"]:
            to_delete.append({"id": d["id"], "your_name": d["your_name"], "ff_title": d["ff_title"]})
    return to_delete

# ── MAIN SYNC FUNCTION ────────────────────────────────────
async def run_sync(progress_callback=None, start_date: datetime | None = None) -> dict:
    """Full sync: scrape FF + fetch OANDA reactions for new events."""
    ensure_tables()
    # Pre-load per-event absolute thresholds once so scrape_month can use them
    global _scrape_thresholds
    _scrape_thresholds = get_abs_thresholds()
    cfg = get_oanda_config()

    log_conn = get_db()
    log_c = log_conn.cursor()
    log_c.execute(
        "INSERT INTO sync_log (started_at, status) VALUES (%s, %s) RETURNING id",
        (datetime.now().isoformat(), "running"),
    )
    log_id = log_c.fetchone()["id"]
    log_conn.commit()
    log_conn.close()

    total_events = 0
    new_events = 0
    reactions_fetched = 0
    errors = []

    try:
        start = start_date or datetime(2020, 1, 1)
        end = datetime.now() + timedelta(days=35)  # include ~1 month ahead for This Week

        # Build list of (year, month) tuples to scrape
        months = []
        current = datetime(start.year, start.month, 1)
        while current <= end:
            months.append((current.year, current.month))
            if current.month == 12:
                current = datetime(current.year + 1, 1, 1)
            else:
                current = datetime(current.year, current.month + 1, 1)

        if progress_callback:
            await progress_callback({"stage": "scraping", "message": f"Scraping {len(months)} months from Forex Factory...", "progress": 0})

        # Scrape all months
        async with httpx.AsyncClient() as client:
            all_events = []
            for i, (year, month) in enumerate(months):
                if progress_callback:
                    pct = int((i / len(months)) * 50)
                    await progress_callback({"stage": "scraping", "message": f"Scraping {datetime(year, month, 1).strftime('%b %Y')}...", "progress": pct})

                month_events = await scrape_month(year, month, client)
                all_events.extend(month_events)
                print(f"  {datetime(year, month, 1).strftime('%b %Y')}: {len(month_events)} events found")
                await asyncio.sleep(2)  # Be polite to FF

            total_events, new_events = save_events(all_events)
            print(f"\n  Total events: {total_events}, New: {new_events}")

            # Fetch OANDA price reactions for events without them
            if progress_callback:
                await progress_callback({"stage": "reactions", "message": "Fetching price reactions from OANDA...", "progress": 50})

            events_needing_reactions = get_events_without_reactions()
            print(f"  Events needing price reactions: {len(events_needing_reactions)}")

            for i, event in enumerate(events_needing_reactions):
                if progress_callback:
                    pct = 50 + int((i / max(len(events_needing_reactions), 1)) * 48)
                    await progress_callback({
                        "stage": "reactions",
                        "message": f"Fetching reaction: {event['your_name']} {event['event_date']}",
                        "progress": pct
                    })

                reaction = await fetch_price_reaction(event, client, cfg)
                if reaction:
                    save_reaction(event["id"], reaction)
                    reactions_fetched += 1

                await asyncio.sleep(0.3)  # Rate limit OANDA

        status = "success"

    except Exception as e:
        status = "error"
        errors.append(str(e))
        print(f"  Sync error: {e}")

    # Backfill any remaining missing reactions (e.g. from This Week saves)
    if progress_callback:
        await progress_callback({"stage": "backfill", "message": "Backfilling missing reactions...", "progress": 97})
    try:
        extra = await backfill_missing_reactions()
        reactions_fetched += extra
    except Exception as e:
        print(f"  backfill error: {e}")

    # Build co-occurrence groups after reactions are fetched
    if progress_callback:
        await progress_callback({"stage": "grouping", "message": "Building co-occurrence groups...", "progress": 99})
    build_event_groups()

    # Update sync log
    log_conn = get_db()
    log_conn.execute("""
        UPDATE sync_log SET finished_at=%s, events_found=%s, events_new=%s,
        reactions_fetched=%s, status=%s, error=%s WHERE id=%s
    """, (datetime.now().isoformat(), total_events, new_events,
          reactions_fetched, status, "; ".join(errors) if errors else None, log_id))
    log_conn.commit()
    log_conn.close()

    if progress_callback:
        await progress_callback({"stage": "done", "message": "Sync complete", "progress": 100})

    return {
        "status": status,
        "total_events": total_events,
        "new_events": new_events,
        "reactions_fetched": reactions_fetched,
        "errors": errors
    }

# ── ANALYSIS QUERIES ──────────────────────────────────────
def get_analysis_summary() -> list:
    """Per-event analysis: avg pip moves, direction consistency, beat/miss rate.

    Direction stats use SOLO (not co-released) occurrences only, to avoid
    double-counting. Pip averages include all rows. post_entry_5_30 and
    max_adverse_5_30 reflect what the trader can actually capture.
    """
    conn = get_db()
    rows = conn.execute("""
        SELECT
            ne.your_name,
            ne.country,
            ne.impact,
            COUNT(ne.id) AS total_occurrences,
            SUM(CASE WHEN NOT COALESCE(pr.is_co_released, FALSE) THEN 1 ELSE 0 END) AS solo_count,
            SUM(CASE WHEN COALESCE(pr.is_co_released, FALSE) THEN 1 ELSE 0 END) AS co_released_count,
            SUM(CASE WHEN ne.beat_miss='beat' THEN 1 ELSE 0 END) AS beat_count,
            SUM(CASE WHEN ne.beat_miss='miss' THEN 1 ELSE 0 END) AS miss_count,
            AVG(pr.pip_5m) AS avg_pip_5m,
            AVG(pr.pip_15m) AS avg_pip_15m,
            AVG(pr.pip_30m) AS avg_pip_30m,
            AVG(pr.pip_60m) AS avg_pip_60m,
            AVG(pr.post_entry_5_30) AS avg_post_entry_5_30,
            AVG(pr.max_adverse_5_30) AS avg_max_adverse,
            -- Direction stats: SOLO rows only (exclude co-released)
            SUM(CASE WHEN NOT COALESCE(pr.is_co_released, FALSE) AND pr.direction_5m='up' THEN 1 ELSE 0 END) AS dir_up_5m,
            SUM(CASE WHEN NOT COALESCE(pr.is_co_released, FALSE) AND pr.direction_5m='down' THEN 1 ELSE 0 END) AS dir_down_5m,
            SUM(CASE WHEN NOT COALESCE(pr.is_co_released, FALSE) AND ne.beat_miss='beat' AND pr.direction_5m='up' THEN 1 ELSE 0 END) AS beat_up,
            SUM(CASE WHEN NOT COALESCE(pr.is_co_released, FALSE) AND ne.beat_miss='beat' AND pr.direction_5m='down' THEN 1 ELSE 0 END) AS beat_down,
            SUM(CASE WHEN NOT COALESCE(pr.is_co_released, FALSE) AND ne.beat_miss='miss' AND pr.direction_5m='up' THEN 1 ELSE 0 END) AS miss_up,
            SUM(CASE WHEN NOT COALESCE(pr.is_co_released, FALSE) AND ne.beat_miss='miss' AND pr.direction_5m='down' THEN 1 ELSE 0 END) AS miss_down,
            SUM(CASE WHEN NOT COALESCE(pr.is_co_released, FALSE) AND ne.beat_miss='beat' THEN 1 ELSE 0 END) AS solo_beat_count,
            SUM(CASE WHEN NOT COALESCE(pr.is_co_released, FALSE) AND ne.beat_miss='miss' THEN 1 ELSE 0 END) AS solo_miss_count,
            MAX(pr.pip_5m) AS max_pip_5m,
            MIN(pr.pip_5m) AS min_pip_5m
        FROM news_events ne
        LEFT JOIN price_reactions pr ON pr.news_event_id = ne.id
        GROUP BY ne.your_name, ne.country, ne.impact
        ORDER BY AVG(pr.post_entry_5_30) DESC NULLS LAST, avg_pip_5m DESC NULLS LAST
    """).fetchall()
    conn.close()

    results = []
    for r in rows:
        d = dict(r)
        total = d["total_occurrences"] or 0
        solo = d["solo_count"] or 0
        co = d["co_released_count"] or 0
        beat = d["beat_count"] or 0
        miss = d["miss_count"] or 0
        solo_beat = d["solo_beat_count"] or 0
        solo_miss = d["solo_miss_count"] or 0
        beat_up = d["beat_up"] or 0
        beat_down = d["beat_down"] or 0
        miss_up = d["miss_up"] or 0
        miss_down = d["miss_down"] or 0

        d["beat_rate"] = round(beat / total * 100) if total > 0 else None
        # Tradeable based on post-entry data (what trader can actually capture)
        d["tradeable"] = (d["avg_post_entry_5_30"] or 0) >= 10

        # Direction stats derived from solo occurrences only
        if solo_beat > 0:
            d["beat_direction"] = "up" if beat_up > beat_down else "down"
            d["beat_consistency"] = round(max(beat_up, beat_down) / solo_beat * 100)
        else:
            d["beat_direction"] = None
            d["beat_consistency"] = None

        if solo_miss > 0:
            d["miss_direction"] = "up" if miss_up > miss_down else "down"
            d["miss_consistency"] = round(max(miss_up, miss_down) / solo_miss * 100)
        else:
            d["miss_direction"] = None
            d["miss_consistency"] = None

        results.append(d)

    return results

def get_event_history(your_name: str) -> list:
    """Get all occurrences of a specific event with reactions."""
    conn = get_db()
    rows = conn.execute("""
        SELECT ne.*, pr.pip_5m, pr.pip_15m, pr.pip_30m, pr.pip_60m,
               pr.direction_5m, pr.direction_15m, pr.open_price
        FROM news_events ne
        LEFT JOIN price_reactions pr ON pr.news_event_id = ne.id
        WHERE ne.your_name = %s
        ORDER BY ne.event_date DESC
    """, (your_name,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_last_sync() -> dict | None:
    conn = get_db()
    row = conn.execute("""
        SELECT * FROM sync_log ORDER BY id DESC LIMIT 1
    """).fetchone()
    conn.close()
    return dict(row) if row else None

# ── JOURNAL & UPCOMING ────────────────────────────────────
def get_journal_rows() -> list:
    conn = get_db()
    rows = conn.execute("""
        SELECT ne.*,
               pr.pip_5m, pr.pip_15m, pr.pip_30m, pr.pip_60m,
               pr.direction_5m, pr.direction_15m, pr.open_price,
               t.id AS trade_id, t.outcome AS trade_outcome
        FROM news_events ne
        LEFT JOIN price_reactions pr ON pr.news_event_id = ne.id
        LEFT JOIN trades t ON t.news_event_id = ne.id
        ORDER BY ne.event_date DESC, ne.event_time DESC, ne.id DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def save_journal_note(event_id: int, note: str) -> bool:
    conn = get_db()
    cur = conn.execute(
        "UPDATE news_events SET user_note = %s WHERE id = %s",
        (note, event_id),
    )
    conn.commit()
    ok = cur.rowcount > 0
    conn.close()
    return ok

def get_news_event(event_id: int) -> dict | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM news_events WHERE id = %s", (event_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def get_upcoming_from_db(days: int = 7) -> list:
    """Upcoming watchlist events from synced news_events (fallback when FF scrape is blocked)."""
    today = datetime.now().date()
    end_date = today + timedelta(days=days)
    conn = get_db()
    rows = conn.execute("""
        SELECT your_name, ff_title, country, event_date, event_time,
               previous, forecast, actual, impact, beat_miss
        FROM news_events
        WHERE event_date >= %s AND event_date <= %s
        ORDER BY event_date ASC, event_time ASC
    """, (today.isoformat(), end_date.isoformat())).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["on_watchlist"] = True
        out.append(d)
    return out

async def scrape_upcoming(days: int = 7) -> list:
    """Scrape FF for all USD/GBP red/orange events in the next N days.

    Maps to NEWS_MAP / your notes when possible. Unmapped events still appear
    (FF title) but are not saved to news_events — keeps sync/analysis pipeline intact.
    """
    global _scrape_thresholds
    if not _scrape_thresholds:
        _scrape_thresholds = get_abs_thresholds()
    today = datetime.now().date()
    end_date = today + timedelta(days=days)
    months = set()
    d = today
    while d <= end_date:
        months.add((d.year, d.month))
        if d.month == 12:
            d = d.replace(year=d.year + 1, month=1, day=1)
        else:
            d = d.replace(month=d.month + 1, day=1)
    async with httpx.AsyncClient() as client:
        all_events = []
        for year, month in sorted(months):
            all_events.extend(await scrape_month(year, month, client, watchlist_only=False))
            await asyncio.sleep(1)
    # Persist only watchlist matches so Analysis / Brief / Journal stay clean
    mapped = [ev for ev in all_events if ev.get("on_watchlist") and ev.get("your_name")]
    if mapped:
        save_events(mapped)
    upcoming = []
    for ev in all_events:
        try:
            ed = datetime.strptime(ev["event_date"], "%Y-%m-%d").date()
            if today <= ed <= end_date:
                upcoming.append(ev)
        except Exception:
            continue
    upcoming.sort(key=lambda e: (e["event_date"], e.get("event_time") or ""))
    return upcoming

def _consistency_stats(rows: list) -> dict:
    beat = miss = 0
    beat_up = beat_down = miss_up = miss_down = 0
    pips_5 = pips_15 = pips_30 = []
    for r in rows:
        if r.get("pip_5m") is not None:
            pips_5.append(r["pip_5m"])
        if r.get("pip_15m") is not None:
            pips_15.append(r["pip_15m"])
        if r.get("pip_30m") is not None:
            pips_30.append(r["pip_30m"])
        bm = r.get("beat_miss")
        d5 = r.get("direction_5m")
        if bm == "beat":
            beat += 1
            if d5 == "up":
                beat_up += 1
            elif d5 == "down":
                beat_down += 1
        elif bm == "miss":
            miss += 1
            if d5 == "up":
                miss_up += 1
            elif d5 == "down":
                miss_down += 1
    avg = lambda xs: round(sum(xs) / len(xs), 1) if xs else None
    result = {
        "total_occurrences": len(rows),
        "avg_pip_5m": avg(pips_5),
        "avg_pip_15m": avg(pips_15),
        "avg_pip_30m": avg(pips_30),
        "beat_count": beat,
        "miss_count": miss,
        "beat_direction": None,
        "beat_consistency": None,
        "miss_direction": None,
        "miss_consistency": None,
        "max_pip_5m": max(pips_5) if pips_5 else None,
        "min_pip_5m": min(pips_5) if pips_5 else None,
    }
    if beat > 0:
        result["beat_direction"] = "up" if beat_up >= beat_down else "down"
        result["beat_consistency"] = round(max(beat_up, beat_down) / beat * 100)
    if miss > 0:
        result["miss_direction"] = "up" if miss_up >= miss_down else "down"
        result["miss_consistency"] = round(max(miss_up, miss_down) / miss * 100)
    return result

def format_release_times(event_date: str, event_time: str, country: str) -> dict:
    """Best-effort FF time → UTC and London labels."""
    try:
        from zoneinfo import ZoneInfo
        if not event_time or event_time in ("", "Tentative", "All Day"):
            return {"utc": "13:30 UTC", "london": "13:30 London", "raw": "13:30 (typical US release)"}
        local_dt = None
        for fmt in ["%Y-%m-%d %I:%M%p", "%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p"]:
            try:
                local_dt = datetime.strptime(f"{event_date} {event_time}", fmt)
                break
            except Exception:
                continue
        if not local_dt:
            return {"utc": event_time, "london": event_time, "raw": event_time}
        if country == "GBP":
            uk = local_dt.replace(tzinfo=ZoneInfo("Europe/London"))
            utc = uk.astimezone(ZoneInfo("UTC"))
        else:
            et = local_dt.replace(tzinfo=ZoneInfo("America/New_York"))
            utc = et.astimezone(ZoneInfo("UTC"))
            uk = utc.astimezone(ZoneInfo("Europe/London"))
        return {
            "utc": utc.strftime("%H:%M UTC"),
            "london": uk.strftime("%H:%M London"),
            "raw": event_time,
        }
    except Exception:
        return {"utc": event_time or "—", "london": event_time or "—", "raw": event_time or "—"}

def _confidence_label(consistency: int | None, scenario: str) -> str:
    if consistency is None:
        return "Not enough historical data — wait and confirm direction manually."
    if consistency >= 75:
        return "STRONG SIGNAL — high confidence trade"
    if consistency >= 60:
        return "MODERATE — confirm direction after release"
    if consistency >= 50:
        return "WEAK — skip unless other signals align"
    return f"AVOID — direction is unpredictable on {scenario}s"

def get_brief(event_name: str) -> dict:
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT ne.*, pr.pip_5m, pr.pip_15m, pr.pip_30m, pr.pip_60m, pr.direction_5m,
                   pr.post_entry_5_30, pr.max_adverse_5_30,
                   COALESCE(pr.is_co_released, FALSE) AS is_co_released
            FROM news_events ne
            LEFT JOIN price_reactions pr ON pr.news_event_id = ne.id
            WHERE ne.your_name = %s
            AND ne.event_date >= '2020-01-01'
            ORDER BY ne.event_date DESC
        """, (event_name,)).fetchall()
        trades = conn.execute("SELECT * FROM trades ORDER BY date DESC").fetchall()
        rating_row = conn.execute(
            "SELECT type, comment FROM news_ratings WHERE name = %s", (event_name,)
        ).fetchone()

        events = [dict(r) for r in rows]
        if not events:
            return {"error": f"No data for {event_name}"}

        stats = _consistency_stats(events)
        today = datetime.now().strftime("%Y-%m-%d")
        upcoming_row = conn.execute("""
            SELECT * FROM news_events
            WHERE your_name = %s AND event_date >= %s
            ORDER BY event_date ASC LIMIT 1
        """, (event_name, today)).fetchone()
        upcoming = dict(upcoming_row) if upcoming_row else dict(events[0])
        sample = upcoming
        country = sample.get("country", "USD")
        release_date = sample.get("event_date", "")
        release_time = sample.get("event_time") or "—"
        forecast = sample.get("forecast") or "—"
        times = format_release_times(release_date, release_time, country)

        same_day_rows = conn.execute("""
            SELECT DISTINCT your_name FROM news_events
            WHERE event_date = %s AND your_name != %s
            ORDER BY your_name
        """, (release_date, event_name)).fetchall()
        same_day_events = [r["your_name"] for r in same_day_rows]

        matched_trades = [
            dict(t) for t in trades
            if event_name in (t["news1"] or "", t["news2"] or "", t["news3"] or "")
            or t["news_event_id"] in [e["id"] for e in events]
        ]
        wins = [t for t in matched_trades if t.get("outcome") == "Win"]
        win_entries = [t["entry"] for t in wins if t.get("entry")]
        suggested_entry = (
            max(set(win_entries), key=win_entries.count) if win_entries
            else "5 min after checking reaction"
        )
        sls = [t["sl"] for t in matched_trades if t.get("sl")]
        suggested_sl = round(sum(sls) / len(sls), 1) if sls else 10
        win_ratios = [t["ratio"] for t in wins if t.get("ratio")]
        suggested_rr = round(sum(win_ratios) / len(win_ratios), 1) if win_ratios else 5

        # Post-entry averages (what the trader can actually capture after a 5-min wait)
        post_entries = [e["post_entry_5_30"] for e in events if e.get("post_entry_5_30") is not None]
        avg_post_entry = round(sum(post_entries) / len(post_entries), 1) if post_entries else None
        heat_vals = [e["max_adverse_5_30"] for e in events if e.get("max_adverse_5_30") is not None]
        avg_heat = round(sum(heat_vals) / len(heat_vals), 1) if heat_vals else None

        # Solo count (not co-released) — used for direction reliability warning
        solo_events = [e for e in events if not e.get("is_co_released")]
        solo_count = len(solo_events)
        # Find the co-release partner name if this event is almost always co-released
        co_partner: str | None = None
        if solo_count < 5:
            grp_row = conn.execute("""
                SELECT member_names FROM event_groups
                WHERE member_names LIKE %s LIMIT 1
            """, (f"%{event_name}%",)).fetchone()
            if grp_row:
                partners = [m for m in dict(grp_row)["member_names"].split("|") if m != event_name]
                co_partner = " + ".join(partners) if partners else None

        avg5 = stats["avg_pip_5m"] or 0
        avg_pe = avg_post_entry or 0
        if avg_pe >= 15:
            power_verdict = "HIGH-POWER"
        elif avg_pe >= 8:
            power_verdict = "MEDIUM-POWER"
        else:
            power_verdict = "LOW-POWER"

        # Suggested SL: max(15, round(avg_heat * 1.2)) — news spread can be 5-15p
        if avg_heat is not None:
            suggested_sl = max(15, round(avg_heat * 1.2))
        elif not sls:
            suggested_sl = 15

        # Suggested R:R from post-entry data: floor(avg_post_entry / sl * 10)/10, capped 1–5
        if avg_pe and suggested_sl:
            rr_from_data = math.floor(avg_pe / suggested_sl * 10) / 10
            suggested_rr = max(1.0, min(5.0, rr_from_data))
        elif win_ratios:
            suggested_rr = round(sum(win_ratios) / len(win_ratios), 1)
        else:
            suggested_rr = 5

        total_trades = len(matched_trades)
        win_rate = round(len(wins) / total_trades * 100) if total_trades else 0
        avg_win_rr = (
            round(sum(win_ratios) / len(win_ratios), 1) if win_ratios else None
        )

        return {
            "event_name": event_name,
            "country": country,
            "release_date": release_date,
            "release_time": release_time,
            "release_time_utc": times["utc"],
            "release_time_london": times["london"],
            "forecast": forecast,
            "stats": stats,
            "beat_direction": stats["beat_direction"],
            "miss_direction": stats["miss_direction"],
            "beat_consistency": stats["beat_consistency"],
            "miss_consistency": stats["miss_consistency"],
            "beat_count": stats["beat_count"],
            "miss_count": stats["miss_count"],
            "min_pip_5m": stats["min_pip_5m"],
            "max_pip_5m": stats["max_pip_5m"],
            "avg_pip_5m": stats["avg_pip_5m"],
            "avg_post_entry_5_30": avg_post_entry,
            "avg_heat": avg_heat,
            "solo_count": solo_count,
            "co_partner": co_partner,
            "rating_type": rating_row["type"] if rating_row else None,
            "rating_comment": rating_row["comment"] if rating_row else None,
            "same_day_events": same_day_events,
            "suggested_entry": suggested_entry,
            "suggested_sl": suggested_sl,
            "suggested_rr": suggested_rr,
            "rr_below_1": bool(avg_pe and suggested_sl and avg_pe / suggested_sl < 1),
            "power_verdict": power_verdict,
            "abs_threshold": get_abs_thresholds().get(event_name, 1.0),
            "value_type": _VALUE_TYPE.get(event_name, "percent"),
            "user_history": {
                "count": total_trades,
                "win_rate": win_rate,
                "avg_winning_rr": avg_win_rr,
            },
            "past_trades": [
                {
                    "date": t["date"],
                    "outcome": t["outcome"],
                    "ratio": t.get("ratio"),
                    "improvement": t.get("improvement") or "",
                    "entry": t.get("entry") or "",
                }
                for t in matched_trades[:3]
            ],
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        conn.close()
