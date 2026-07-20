#!/usr/bin/env python3
"""
Build the data files for the NYC Wild Animal Census map.

Sources (all free, no API key required):
  1. iNaturalist API  -- research-grade wild-animal observations inside the
     NYC place boundary (place_id 674). Powers the species census, the map of
     recent sightings, and the notable-sightings feed.
  2. NYC Open Data (Socrata) -- Urban Park Ranger "Animal Condition Response"
     dataset (fuhs-xmg2): every rescue / response the Rangers logged, with
     species, condition, borough and outcome.

Outputs (written to data/):
  census.json   -- every wild vertebrate species recorded, with counts
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
    "Felis catus": "Feral / domestic cat",
    "Canis familiaris": "Domestic dog",
    "Canis lupus familiaris": "Domestic dog",
}

RECENT_DAYS = 120  # window for the map + notable feed


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
    return None


def photo_url(taxon, size="square"):
    dp = (taxon or {}).get("default_photo") or {}
    url = dp.get("square_url") or dp.get("url") or ""
    if size == "medium" and url:
        url = url.replace("square", "medium")
    return url


# ---------------------------------------------------------------------------
# 1. Species census
# ---------------------------------------------------------------------------

def build_census():
    print("Building species census...")
    species = []
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
                species.append({
                    "id": t["id"],
                    "common": t.get("preferred_common_name") or sci,
                    "sci": sci,
                    "class": label,
                    "count": r["count"],
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

    species.sort(key=lambda s: -s["count"])
    (DATA / "census.json").write_text(json.dumps(species, separators=(",", ":")))
    return species, by_class_count


# ---------------------------------------------------------------------------
# 2. Recent geotagged sightings for the map
# ---------------------------------------------------------------------------

def fetch_recent(params, cap_pages):
    """Fetch geotagged research-grade observations, newest first."""
    out = []
    d1 = (datetime.now(timezone.utc) - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    page = 1
    while page <= cap_pages:
        q = {
            "place_id": PLACE_ID,
            "quality_grade": "research",
            "geo": "true",
            "d1": d1,
            "order": "desc",
            "order_by": "observed_on",
            "per_page": 200,
            "page": page,
            **params,
        }
        url = f"{INAT}/observations?" + urllib.parse.urlencode(q)
        d = get(url)
        if not d or not d["results"]:
            break
        out.extend(d["results"])
        if len(out) >= d["total_results"]:
            break
        page += 1
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
        "place": o.get("place_guess") or "",
        "by": (o.get("user") or {}).get("login") or "",
        "uri": o.get("uri") or "",
        "ubiquitous": sci in UBIQUITOUS,
    }


def build_sightings():
    print(f"Fetching recent sightings (last {RECENT_DAYS} days)...")
    raw = []
    # Mammals, reptiles, amphibians: all of them.
    raw += fetch_recent({"iconic_taxa": "Mammalia,Reptilia,Amphibia"}, cap_pages=25)
    print(f"  mammals/reptiles/amphibians: {len(raw)} obs")
    # Charismatic birds only.
    before = len(raw)
    ids = ",".join(str(i) for i in NOTABLE_BIRD_ORDERS)
    raw += fetch_recent({"taxon_id": ids}, cap_pages=25)
    print(f"  charismatic birds: {len(raw) - before} obs")

    seen = set()
    points = []
    for o in raw:
        if o["id"] in seen:
            continue
        seen.add(o["id"])
        p = to_point(o)
        if p and p["lat"] and p["lon"]:
            points.append(p)
    print(f"  -> {len(points)} unique map points")
    (DATA / "sightings.json").write_text(json.dumps(points, separators=(",", ":")))
    return points


# ---------------------------------------------------------------------------
# 3. Notable-sightings feed (rarest species seen recently)
# ---------------------------------------------------------------------------

def build_notable(census, points):
    print("Ranking notable sightings...")
    counts = {s["sci"]: s["count"] for s in census}
    # One entry per species: the most recent sighting of each.
    latest = {}
    for p in points:
        if p["ubiquitous"] or not p["sci"]:
            continue
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
    try:
        rows = get(url)
    except Exception as e:  # noqa: BLE001
        print(f"  rangers fetch failed: {e}", file=sys.stderr)
        rows = []
    if not rows:
        rows = []

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
    census, by_class = build_census()
    points = build_sightings()
    notable = build_notable(census, points)
    rescues = build_rescues()

    wild = [s for s in census if not s["ubiquitous"]]
    meta = {
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "recent_days": RECENT_DAYS,
        "totals": {
            "species": len(census),
            "wild_species": len(wild),
            "by_class": by_class,
            "map_points": len(points),
            "notable": len(notable),
            "ranger_responses": rescues["total"],
        },
        "sources": {
            "inaturalist": f"iNaturalist API, place_id {PLACE_ID} (New York City), research-grade observations",
            "rangers": "NYC Open Data fuhs-xmg2 (Urban Park Ranger Animal Condition Response)",
        },
    }
    (DATA / "meta.json").write_text(json.dumps(meta, indent=2))
    print("\nDone.")
    print(json.dumps(meta["totals"], indent=2))


if __name__ == "__main__":
    main()
