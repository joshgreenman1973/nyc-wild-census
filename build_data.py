#!/usr/bin/env python3
"""
Build the data files for the NYC Wild Animal Census map.

Sources:
  0. eBird API 2.0 -- birds. Needs a free key in EBIRD_API_KEY (GitHub secret,
     or the macOS Keychain item EBIRD_API_KEY locally). iNaturalist's bird
     record is thin next to eBird's, so eBird supplies the optional bird layer
     on the map and the rarity verdict for birds in the notable feed. We
     publish a derived subset -- the latest record per species per borough --
     rather than a copy of eBird's observation database, and credit eBird
     wherever it shows.
  1. iNaturalist API  -- research-grade wild-animal observations inside the
     NYC place boundary (place_id 674). Powers the species census, the map of
     recent sightings, and the notable-sightings feed.
  2. NYC Open Data (Socrata) -- Urban Park Ranger "Animal Condition Response"
     dataset (fuhs-xmg2): every rescue / response the Rangers logged, with
     species, condition, borough and outcome.

Outputs (written to data/):
  census.json   -- every wild vertebrate species recorded, with counts
  ebird.json    -- optional bird layer: latest eBird record per species per borough
  sightings.json-- recent geotagged observations, for the map
  notable.json  -- recent sightings of the rarest species, for the feed
  rescues.json  -- Park Ranger responses: recent list + aggregates
  meta.json     -- build timestamp + headline totals + source notes

Design choices are documented in README.md (methodology section). The most
important one: the ubiquitous synanthropes the user does NOT want -- pigeon,
house sparrow, starling, brown/black rat, house mouse, feral cat/dog -- are
flagged (`ubiquitous: true`) and hidden by default, never silently dropped.
"""

import json
import os
import re
import subprocess
import time
import sys
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

DATA = Path(__file__).parent / "data"
DATA.mkdir(exist_ok=True)

INAT = "https://api.inaturalist.org/v1"
EBIRD = "https://api.ebird.org/v2"
EBIRD_DAYS = 30          # the API caps `back` at 30
EBIRD_REGIONS = {        # eBird has no "New York City" region, only counties
    "US-NY-005": "Bronx",
    "US-NY-047": "Brooklyn",
    "US-NY-061": "Manhattan",
    "US-NY-081": "Queens",
    "US-NY-085": "Staten Island",
}
PLACE_ID = 674  # New York City on iNaturalist
UA = "nyc-wild-census/1.0 (github.com/joshgreenman1973; personal civic-data project)"

# Iconic taxa we treat as the "wild vertebrate" census.
CLASSES = {
    "Mammalia": "mammals",
    "Aves": "birds",
    "Reptilia": "reptiles",
    "Amphibia": "amphibians",
}

# Charismatic bird orders that belong on the MAP even though birds as a whole
# are too numerous to plot. (taxon_id -> label)
NOTABLE_BIRD_ORDERS = {
    71261: "raptors",       # Accipitriformes (hawks, eagles, kites)
    67570: "falcons",       # Falconiformes
    19350: "owls",          # Strigiformes
    67566: "wading birds",  # Pelecaniformes (herons, egrets, ibises)
    71268: "cormorants",    # Suliformes
    67562: "loons",         # Gaviiformes
}

# The synanthropes the user explicitly excludes ("pigeons, dogs, cats, rats,
# etc."). Matched on scientific name. Flagged, not deleted.
UBIQUITOUS = {
    "Columba livia": "Rock Pigeon",
    "Passer domesticus": "House Sparrow",
    "Sturnus vulgaris": "European Starling",
    "Rattus norvegicus": "Brown Rat",
    "Rattus rattus": "Black Rat",
    "Mus musculus": "House Mouse",
    # The gray squirrel only. The red and flying squirrels are genuinely
    # scarce here (7 and 29 all-time records) and belong in the notable feed.
    "Sciurus carolinensis": "Eastern Gray Squirrel",
    "Felis catus": "Feral / domestic cat",
    "Canis familiaris": "Domestic dog",
    "Canis lupus familiaris": "Domestic dog",
}

RECENT_DAYS = 120  # window for the notable feed (and the map's default view)
MAP_DAYS = 365     # how far back the map's time-range picker can go

# Observations logged at a zoo or aquarium are dropped: at those locations the
# community rarely flags captive exhibit animals, and there is no reliable way
# to tell a wild squirrel on zoo grounds from an exotic species inside the
# reptile house. Excluding the location entirely is the honest call.
CAPTIVE_LOC = re.compile(r"\b(zoo|aquarium)\b", re.I)

# iNaturalist place IDs for the four NYC zoos that have their own (tight)
# place polygons. The Bronx Zoo and the New York Aquarium have no place record,
# so they are handled by bounding box below.
ZOO_PLACE_IDS = "204057,204094,204060,204058"  # Central Park, Prospect Park, Queens, Staten Island
# (nelat, nelng, swlat, swlng) around the two facilities without a place record.
ZOO_BBOXES = [
    {"nelat": 40.8585, "nelng": -73.8660, "swlat": 40.8420, "swlng": -73.8830},  # Bronx Zoo
    {"nelat": 40.5760, "nelng": -73.9735, "swlat": 40.5725, "swlng": -73.9775},  # NY Aquarium
]
# A species is pulled out of the wild census and shown separately once this
# share of its NYC records were made at a zoo or aquarium.
ZOO_ONLY_SHARE = 0.66
# Sanity floors for the census. The wild count sits in the mid-hundreds (536 at
# last check) and only ever grows as observations accumulate, so a collapse
# means a broken or throttled fetch, not a quieter city. Set well below the real
# number: these exist to catch a wipe, not to police normal variation.
CENSUS_FLOOR = 200
CENSUS_DROP_LIMIT = 0.75  # never accept losing more than a quarter at once

# Place labels that carry no real information -- shown when iNaturalist has
# obscured the true coordinates (sensitive or wide-ranging species).
VAGUE_PLACES = {"", "United States", "United States of America", "USA", "US"}


# Trailing country tokens that iNaturalist appends in the observer's own app
# language (e.g. "EE. UU." = Estados Unidos, "美国" = United States).
COUNTRY_SUFFIX = re.compile(
    r",\s*(United States(?: of America)?|USA|US|EE\.?\s*UU\.?|Estados Unidos|美国|미국)\.?\s*$",
    re.I)


def clean_place(place, obscured):
    place = (place or "").strip()
    if obscured and place in VAGUE_PLACES:
        return "Location withheld by iNaturalist"
    if place in VAGUE_PLACES:
        return ""
    # Drop a trailing country token so labels read as NYC places, not "…, USA".
    place = COUNTRY_SUFFIX.sub("", place).strip().rstrip(",")
    return place


def get(url, tries=4):
    """GET JSON with polite retries and rate limiting."""
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode())
            time.sleep(1.1)  # iNat asks for < ~1 req/sec sustained
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                if attempt == tries - 1:
                    # Throttling is the most common real failure here. Falling
                    # through to None let callers read it as "no more data" and
                    # overwrite a good census with an empty one.
                    raise
                wait = 10 * (attempt + 1)
                print(f"  429 throttled, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
            elif e.code >= 500 and attempt < tries - 1:
                time.sleep(5 * (attempt + 1))
            else:
                print(f"  HTTP {e.code} for {url}", file=sys.stderr)
                if attempt == tries - 1:
                    raise
                time.sleep(3)
        except Exception as e:  # noqa: BLE001
            print(f"  error {e} for {url}", file=sys.stderr)
            if attempt == tries - 1:
                raise
            time.sleep(3)
    # Exhausting every retry is a failed fetch, not an empty result. Returning
    # None here made a dead API indistinguishable from the end of the data.
    raise RuntimeError(f"giving up on {url} after {tries} attempts")


def photo_url(taxon, size="square"):
    dp = (taxon or {}).get("default_photo") or {}
    url = dp.get("square_url") or dp.get("url") or ""
    if size == "medium" and url:
        url = url.replace("square", "medium")
    return url


# ---------------------------------------------------------------------------
# 1. Species census
# ---------------------------------------------------------------------------

ALL_ICONIC = ",".join(CLASSES)


def fetch_zoo_counts():
    """Count NYC research-grade records made at a zoo or aquarium, per species.

    Uses iNaturalist's own location labels, not a raw bounding box: the four
    zoos with place polygons are counted directly, while the Bronx Zoo and the
    aquarium (no place record) are counted by pulling every observation in a
    box around them and keeping only those iNaturalist itself geocoded to a
    place named 'Zoo' or 'Aquarium'. That distinction matters -- it keeps a
    wild Bronx River otter (labeled 'Bronx') out of the zoo tally while still
    catching an exhibit gecko (labeled 'Bronx Zoo')."""
    print("Counting zoo/aquarium records...")
    zoo = {}
    # Zoos with tight place polygons.
    page = 1
    while True:
        url = (f"{INAT}/observations/species_counts?place_id={ZOO_PLACE_IDS}"
               f"&iconic_taxa={ALL_ICONIC}&quality_grade=research"
               f"&per_page=500&page={page}")
        d = get(url)
        if not d or not d["results"]:
            break
        for r in d["results"]:
            zoo[r["taxon"]["id"]] = zoo.get(r["taxon"]["id"], 0) + r["count"]
        if page * 500 >= d["total_results"]:
            break
        page += 1
    # Bronx Zoo + aquarium: bounding box, then filter to zoo-labeled records.
    for box in ZOO_BBOXES:
        page = 1
        while True:
            q = {"iconic_taxa": ALL_ICONIC, "quality_grade": "research",
                 "per_page": 200, "page": page, **box}
            d = get(f"{INAT}/observations?" + urllib.parse.urlencode(q))
            if not d or not d["results"]:
                break
            for o in d["results"]:
                if CAPTIVE_LOC.search(o.get("place_guess") or ""):
                    t = o.get("taxon") or {}
                    if t.get("id"):
                        zoo[t["id"]] = zoo.get(t["id"], 0) + 1
            if page * 200 >= d["total_results"]:
                break
            page += 1
    print(f"  {len(zoo)} species have at least one zoo/aquarium record")
    return zoo


def build_census():
    print("Building species census...")
    zoo_counts = fetch_zoo_counts()
    raw = []
    by_class_count = {}
    for taxon, label in CLASSES.items():
        page = 1
        got = 0
        while True:
            url = (f"{INAT}/observations/species_counts?place_id={PLACE_ID}"
                   f"&iconic_taxa={taxon}&quality_grade=research"
                   f"&per_page=500&page={page}")
            d = get(url)
            if not d:
                break
            for r in d["results"]:
                t = r["taxon"]
                sci = t["name"]
                total = r["count"]
                zc = min(zoo_counts.get(t["id"], 0), total)
                raw.append({
                    "id": t["id"],
                    "common": t.get("preferred_common_name") or sci,
                    "sci": sci,
                    "class": label,
                    "count": total - zc,     # wild count (zoo records removed)
                    "total": total,          # raw iNaturalist total, for transparency
                    "zoo": zc,               # records made at a zoo / aquarium
                    "photo": photo_url(t),
                    "wiki": t.get("wikipedia_url") or "",
                    "ubiquitous": sci in UBIQUITOUS,
                })
                got += 1
            if got >= d["total_results"] or not d["results"]:
                break
            page += 1
        by_class_count[label] = got
        print(f"  {label}: {got} species")

    # Split off species that live (almost) entirely inside the zoos.
    species, zoo_only = [], []
    for s in raw:
        share = s["zoo"] / s["total"] if s["total"] else 0
        if s["zoo"] > 0 and (s["count"] == 0 or share >= ZOO_ONLY_SHARE):
            zoo_only.append(s)
        else:
            species.append(s)

    species.sort(key=lambda s: -s["count"])
    zoo_only.sort(key=lambda s: -s["zoo"])
    # Class counts should reflect the wild census, after zoo-only species leave.
    by_class_count = {label: 0 for label in CLASSES.values()}
    for s in species:
        by_class_count[s["class"]] += 1
    # Refuse to publish a collapsed census. A throttled or broken fetch yields
    # few or no species, which is never a real result for a city this size —
    # and overwriting the good file would destroy the only copy.
    prev_path = DATA / "census.json"
    prev_count = 0
    if prev_path.exists():
        try:
            prev_count = len(json.loads(prev_path.read_text()))
        except (ValueError, OSError):
            prev_count = 0
    if len(species) < CENSUS_FLOOR or len(species) < prev_count * CENSUS_DROP_LIMIT:
        raise RuntimeError(
            f"census collapsed to {len(species)} species (previous {prev_count}, "
            f"floor {CENSUS_FLOOR}); refusing to overwrite census.json"
        )

    prev_path.write_text(json.dumps(species, separators=(",", ":")))
    (DATA / "zoo_species.json").write_text(json.dumps(zoo_only, separators=(",", ":")))
    print(f"  -> {len(species)} wild species, {len(zoo_only)} zoo-only species set aside")
    return species, zoo_only, by_class_count


# ---------------------------------------------------------------------------
# 1b. eBird: the bird layer and the rarity verdict for birds
# ---------------------------------------------------------------------------

def ebird_key():
    """Key from the environment (CI) or the macOS Keychain (this laptop)."""
    k = os.environ.get("EBIRD_API_KEY")
    if k:
        return k.strip()
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "EBIRD_API_KEY", "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def ebird_get(path, key, params):
    url = f"{EBIRD}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"X-eBirdApiToken": key, "User-Agent": UA})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise
        except urllib.error.URLError:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise
    return None


def build_ebird(census):
    """Latest eBird record per species per borough, plus eBird's rarity flag.

    eBird's own reviewers maintain the regional filters that decide what counts
    as notable, which is a far better rarity signal for birds than counting
    iNaturalist records."""
    key = ebird_key()
    existing = DATA / "ebird.json"
    if not key:
        print("eBird: no EBIRD_API_KEY, skipping (leaving any existing ebird.json alone)")
        if existing.exists():
            return json.loads(existing.read_text())
        return []

    print(f"Fetching eBird birds (last {EBIRD_DAYS} days, 5 boroughs)...")
    photos = {s["sci"]: s.get("photo") for s in census if s.get("photo")}
    best = {}   # (sciName, borough) -> record
    notable_species = set()

    for region, borough in EBIRD_REGIONS.items():
        rows = ebird_get(f"data/obs/{region}/recent", key,
                         {"back": EBIRD_DAYS}) or []
        rare = ebird_get(f"data/obs/{region}/recent/notable", key,
                         {"back": EBIRD_DAYS, "detail": "full"}) or []
        if not rows:
            raise SystemExit(f"eBird returned no observations for {borough} ({region}). "
                             "Refusing to publish a half-empty bird layer.")
        for r in rare:
            if r.get("sciName"):
                notable_species.add(r["sciName"])
        for r in rows + rare:
            sci, lat, lon = r.get("sciName"), r.get("lat"), r.get("lng")
            if not sci or lat is None or lon is None:
                continue
            k = (sci, borough)
            cur = best.get(k)
            if cur is None or (r.get("obsDt") or "") > (cur.get("obsDt") or ""):
                best[k] = r
        print(f"  {borough}: {len(rows)} species, {len(rare)} notable records")

    points = []
    for (sci, borough), r in best.items():
        points.append({
            "lat": round(r["lat"], 5),
            "lon": round(r["lng"], 5),
            "common": r.get("comName") or sci,
            "sci": sci,
            "class": "birds",
            "date": (r.get("obsDt") or "")[:10],
            "time": (r.get("obsDt") or "")[11:16],
            "place": r.get("locName") or borough,
            "borough": borough,
            "howMany": r.get("howMany"),
            "notable": sci in notable_species,
            "photo": photos.get(sci),
            "uri": f"https://ebird.org/checklist/{r['subId']}" if r.get("subId") else "",
            "source": "ebird",
        })
    points.sort(key=lambda x: (x["date"], x["common"]), reverse=True)
    (DATA / "ebird.json").write_text(json.dumps(points, separators=(",", ":")))
    n_rare = sum(1 for p in points if p["notable"])
    print(f"  -> {len(points)} records, {len(set(p['sci'] for p in points))} species, "
          f"{n_rare} flagged notable by eBird")
    return points


# ---------------------------------------------------------------------------
# 2. Recent geotagged sightings for the map
# ---------------------------------------------------------------------------

def fetch_recent(params, days, cap_pages):
    """Fetch geotagged research-grade observations from the last `days` days.

    Uses id_below cursoring (newest id first) rather than page numbers, since
    iNaturalist refuses page*per_page beyond 10,000."""
    out = []
    d1 = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    id_below = None
    for _ in range(cap_pages):
        q = {
            "place_id": PLACE_ID,
            "quality_grade": "research",
            "geo": "true",
            "d1": d1,
            "order": "desc",
            "order_by": "id",
            "per_page": 200,
            **params,
        }
        if id_below:
            q["id_below"] = id_below
        url = f"{INAT}/observations?" + urllib.parse.urlencode(q)
        d = get(url)
        if not d or not d["results"]:
            break
        out.extend(d["results"])
        id_below = d["results"][-1]["id"]
        if len(d["results"]) < 200:
            break
    else:
        print(f"  WARNING: hit page cap ({cap_pages}); window truncated")
    return out


def to_point(o):
    t = o.get("taxon") or {}
    geo = o.get("geojson") or {}
    coords = geo.get("coordinates")
    if not coords:
        return None
    iconic = (t.get("iconic_taxon_name") or "").lower()
    cls = {"mammalia": "mammals", "aves": "birds", "reptilia": "reptiles",
           "amphibia": "amphibians"}.get(iconic, iconic)
    sci = t.get("name") or ""
    return {
        "lat": round(coords[1], 5),
        "lon": round(coords[0], 5),
        "common": t.get("preferred_common_name") or sci,
        "sci": sci,
        "class": cls,
        "date": o.get("observed_on"),
        "photo": photo_url(t),
        "place": clean_place(o.get("place_guess"), o.get("obscured")),
        "by": (o.get("user") or {}).get("login") or "",
        "uri": o.get("uri") or "",
        "ubiquitous": sci in UBIQUITOUS,
    }


def build_sightings():
    print(f"Fetching map sightings (last {MAP_DAYS} days)...")
    raw = []
    # Mammals, reptiles, amphibians: all of them.
    raw += fetch_recent({"iconic_taxa": "Mammalia,Reptilia,Amphibia"}, MAP_DAYS, cap_pages=120)
    print(f"  mammals/reptiles/amphibians: {len(raw)} obs")
    # Charismatic birds only.
    before = len(raw)
    ids = ",".join(str(i) for i in NOTABLE_BIRD_ORDERS)
    raw += fetch_recent({"taxon_id": ids}, MAP_DAYS, cap_pages=120)
    print(f"  charismatic birds: {len(raw) - before} obs")

    seen = set()
    points = []
    dropped_captive = 0
    for o in raw:
        if o["id"] in seen:
            continue
        seen.add(o["id"])
        if CAPTIVE_LOC.search(o.get("place_guess") or ""):
            dropped_captive += 1
            continue
        p = to_point(o)
        if p and p["lat"] and p["lon"]:
            points.append(p)
    print(f"  dropped {dropped_captive} zoo/aquarium observations")
    print(f"  -> {len(points)} unique map points")
    (DATA / "sightings.json").write_text(json.dumps(points, separators=(",", ":")))
    return points


# ---------------------------------------------------------------------------
# 3. Notable-sightings feed (rarest species seen recently)
# ---------------------------------------------------------------------------

def _datekey(d):
    """YYYY-MM-DD as a sortable int, for descending sorts inside a tuple."""
    return int((d or "0000-00-00").replace("-", ""))


def build_notable(census, points, ebird_points):
    """Rarity, judged by the source that can actually judge it.

    For mammals, reptiles and amphibians, iNaturalist is ~95% of the record, so
    counting its records is a fair measure of how seldom a species turns up.
    For birds it is not: eBird holds roughly 65 times more New York City bird
    records, so an iNaturalist count says more about who carries a camera than
    about how rare the bird is. Birds are therefore ranked by eBird's notable
    flag, which its regional reviewers maintain, and are left out of the
    count-based ranking entirely."""
    print("Ranking notable sightings...")
    counts = {s["sci"]: s["count"] for s in census}
    # One entry per species: the most recent sighting of each.
    latest = {}
    for p in points:
        if p["ubiquitous"] or not p["sci"]:
            continue
        if p["class"] == "birds":
            continue   # eBird decides this one
        cur = latest.get(p["sci"])
        if cur is None or (p["date"] or "") > (cur["date"] or ""):
            latest[p["sci"]] = p
    items = []
    for sci, p in latest.items():
        total = counts.get(sci, 0)
        if total == 0:
            continue
        # Rarity: fewer all-time NYC records => more notable.
        if total <= 25:
            tier, why = 3, f"Only {total} research-grade record{'s' if total != 1 else ''} in NYC, ever"
        elif total <= 100:
            tier, why = 2, f"Uncommon in NYC ({total} all-time records)"
        elif total <= 400:
            tier, why = 1, f"Not often reported ({total} all-time records)"
        else:
            continue  # common species aren't "notable"
        item = dict(p)
        item["total"] = total
        item["tier"] = tier
        item["why"] = why
        items.append(item)
    # Rarest first, then most recent.
    items.sort(key=lambda x: (-x["tier"], x["total"], x["date"] or ""), reverse=False)
    items.sort(key=lambda x: (x["total"], -(x["tier"])))

    # Birds: eBird's filters flag a record when it falls outside what its
    # reviewers expect for that county at that time of year. That catches
    # genuine rarities, but also common birds seen out of season or in
    # unusual numbers -- a Mute Swan flagged in all five boroughs is a
    # seasonal filter firing, not a rare bird. So these are labelled
    # "unexpected", never "rare", and the ones flagged in only one borough
    # (the likelier oddities) come first.
    seen_bird, boroughs = {}, {}
    for p in ebird_points or []:
        if not p.get("notable"):
            continue
        boroughs.setdefault(p["sci"], set()).add(p.get("borough"))
        cur = seen_bird.get(p["sci"])
        if cur is None or (p["date"] or "") > (cur["date"] or ""):
            seen_bird[p["sci"]] = p
    birds = sorted(
        seen_bird.values(),
        key=lambda x: (len(boroughs.get(x["sci"], ())), -_datekey(x.get("date"))),
    )[:12]
    for b in birds:
        n_bor = len(boroughs.get(b["sci"], ()))
        item = dict(b)
        item["tier"] = 2
        item["tierLabel"] = "Unexpected"
        item["total"] = None
        item["why"] = ("Outside what eBird expects in "
                       + (b.get("borough") or "this county")
                       + " right now, whether that is scarcity, an off-season date or an odd count"
                       + ("" if n_bor == 1 else f"; also flagged in {n_bor - 1} other borough"
                          + ("s" if n_bor > 2 else "")))
        items.append(item)
    if birds:
        print(f"  {len(birds)} bird species carried in from eBird's flagged list")

    items = items[:60]
    (DATA / "notable.json").write_text(json.dumps(items, separators=(",", ":")))
    print(f"  -> {len(items)} notable species in the feed")
    return items


# ---------------------------------------------------------------------------
# 4. NYC Urban Park Ranger animal responses
# ---------------------------------------------------------------------------

def build_rescues():
    print("Fetching Urban Park Ranger responses...")
    q = urllib.parse.urlencode({
        "$limit": 50000,
        "$order": "date_and_time_of_initial DESC",
    })
    url = f"https://data.cityofnewyork.us/resource/fuhs-xmg2.json?{q}"
    # A failed fetch must not masquerade as "no rescues on record" — that would
    # publish an empty rescue log over a good one. Let it raise.
    rows = get(url)
    if not rows:
        raise RuntimeError("ranger rescues returned no rows; refusing to "
                           "overwrite the existing rescue data")

    by_species = {}
    by_borough = {}
    by_status = {}
    recent = []
    for r in rows:
        sp = (r.get("species_description") or "Unknown").strip()
        bo = (r.get("borough") or "Unknown").strip()
        cond = (r.get("animal_condition") or "").strip()
        by_species[sp] = by_species.get(sp, 0) + 1
        by_borough[bo] = by_borough.get(bo, 0) + 1
        if cond:
            by_status[cond] = by_status.get(cond, 0) + 1
    for r in rows[:120]:
        recent.append({
            "date": (r.get("date_and_time_of_initial") or "")[:10],
            "species": (r.get("species_description") or "Unknown").strip(),
            "borough": (r.get("borough") or "").strip(),
            "property": (r.get("property") or "").strip(),
            "condition": (r.get("animal_condition") or "").strip(),
            "status": (r.get("species_status") or "").strip(),
            "action": (r.get("final_ranger_action") or "").strip(),
        })

    top_species = sorted(by_species.items(), key=lambda x: -x[1])[:25]
    out = {
        "total": len(rows),
        "by_species": top_species,
        "by_borough": sorted(by_borough.items(), key=lambda x: -x[1]),
        "by_status": sorted(by_status.items(), key=lambda x: -x[1]),
        "recent": recent,
    }
    (DATA / "rescues.json").write_text(json.dumps(out, separators=(",", ":")))
    print(f"  -> {len(rows)} ranger responses, {len(top_species)} top species")
    return out


# ---------------------------------------------------------------------------

def main():
    census, zoo_only, by_class = build_census()
    ebird = build_ebird(census)
    points = build_sightings()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    recent_points = [p for p in points if (p["date"] or "") >= cutoff]
    notable = build_notable(census, recent_points, ebird)
    rescues = build_rescues()

    wild = [s for s in census if not s["ubiquitous"]]
    meta = {
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "recent_days": RECENT_DAYS,
        "map_days": MAP_DAYS,
        "totals": {
            "species": len(census),
            "wild_species": len(wild),
            "zoo_only_species": len(zoo_only),
            "by_class": by_class,
            "map_points": len(points),
            "ebird_points": len(ebird),
            "ebird_species": len(set(p["sci"] for p in ebird)),
            "ebird_notable": sum(1 for p in ebird if p.get("notable")),
            "recent_points": len(recent_points),
            "notable": len(notable),
            "ranger_responses": rescues["total"],
        },
        "ebird_days": EBIRD_DAYS,
        "sources": {
            "inaturalist": f"iNaturalist API, place_id {PLACE_ID} (New York City), research-grade observations",
            "ebird": ("eBird API 2.0, the five New York City counties, last "
                      f"{EBIRD_DAYS} days. Latest record per species per borough, "
                      "not a copy of eBird's observation database. Data provided by "
                      "eBird (ebird.org), Cornell Lab of Ornithology."),
            "rangers": "NYC Open Data fuhs-xmg2 (Urban Park Ranger Animal Condition Response)",
        },
    }
    (DATA / "meta.json").write_text(json.dumps(meta, indent=2))
    print("\nDone.")
    print(json.dumps(meta["totals"], indent=2))


if __name__ == "__main__":
    main()
