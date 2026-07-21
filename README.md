# The other New Yorkers

A living census and map of the wild animals recorded in New York City: the coyotes, seals, owls, foxes, turtles and salamanders that share the five boroughs. Not the pigeons, rats, cats and dogs. The wild ones.

**Live:** https://joshgreenman1973.github.io/nyc-wild-census/

The page has four parts:

1. **The map** — every research-grade sighting from the last 120 days, colored by animal class. Mammals, reptiles and amphibians are shown in full; among birds, only the charismatic groups people actually notice (hawks, owls, herons, falcons, loons, cormorants) are plotted, because the full bird list runs to hundreds of species.
2. **Notable lately** — the rarest animals seen recently, ranked by how seldom they turn up in the city's records. This is where the mink, the river otter, the sei whale and the stray sea turtles surface.
3. **The full census** — every wild species on record, most-seen first, searchable and filterable by class.
4. **When the Rangers get called** — the NYC Urban Park Rangers' own animal-response log: what they respond to, and how those animals turn out.

## Sources

All data is free and requires no API key.

| Source | What it powers | Endpoint |
|---|---|---|
| [iNaturalist API](https://api.inaturalist.org/v1/docs/) | census, map, notable feed | `place_id=674` (New York City), `quality_grade=research` |
| [NYC Open Data — Urban Park Ranger Animal Condition Response](https://data.cityofnewyork.us/Environment/Urban-Park-Ranger-Animal-Condition-Response/fuhs-xmg2) | rescues section | Socrata dataset `fuhs-xmg2` |

**Research grade** on iNaturalist means an observation has a photo, a date, a location, and at least two-thirds of identifiers agreeing on the species. It is the community's confidence bar, not a scientific census.

## Methodology and honest limits

Following the project rule of no black boxes, here is exactly what the numbers mean and where they fall short.

- **Counts reflect effort, not abundance.** iNaturalist records show where people look and photograph, not where animals live. A heavily birded park (Central Park, Prospect Park, Jamaica Bay) will always out-report a quiet corner of Staten Island. Treat counts as *reporting frequency*, not population.
- **"Notable" means rarely reported.** The notable feed ranks by how few all-time NYC records a species has. Fewer than 25 records is tagged **Rare**; 26–100 **Uncommon**; 101–400 **Seldom seen**. A low count can mean a genuinely rare animal (river otter), a hard-to-spot one (most bats), an escaped or released pet (map turtles, green anoles, house geckos), or a species newly turning up. These are leads, not verdicts.
- **Escapees and strays are in the data.** Some entries are pets that got loose or were released. They are kept because they are part of the honest record, but they are why a "rare" tag is not proof of a wild population.
- **Zoo and aquarium records are pulled out everywhere, not just the map.** Captive exhibit animals are rarely flagged as captive by observers, so an exotic species logged inside the Bronx Zoo reptile house can pass as "research grade." Every record iNaturalist geocodes to a zoo or aquarium is removed from the map, the notable feed and the species counts, so the census number for each animal reflects its *wild* records only. Species whose records are two-thirds or more from a zoo (currently the flat-tailed house gecko, an exhibit animal) are lifted out of the wild census entirely and listed separately under "Behind the glass."
  - The zoo tally uses iNaturalist's own location labels, not a raw map box. The four zoos with their own place boundaries are counted directly; the Bronx Zoo and the New York Aquarium, which have no place record, are handled by pulling every observation in a box around them and keeping only the ones iNaturalist itself labels "Zoo" or "Aquarium." That distinction is deliberate: it keeps a wild Bronx River otter (which iNaturalist labels "Bronx") in the wild census while still catching an exhibit gecko (labeled "Bronx Zoo"). A raw bounding box would wrongly brand the otter, the beaver and every wild bird near the zoo as captive.
- **Location fuzzing.** iNaturalist obscures the precise coordinates of sensitive or threatened species, so some points are deliberately imprecise.
- **The excluded synanthropes are flagged, never dropped.** The ubiquitous animals the census sets aside — rock pigeon, house sparrow, European starling, brown rat, black rat, house mouse, feral cat and domestic dog — are marked `ubiquitous: true` and hidden by default. Toggle "include pigeons, rats & co." to bring them back.
- **The Rangers log is a response record, not a wildlife survey.** It counts calls the Urban Park Rangers answered (many for raccoons, injured birds, loose domestic animals), so it reflects human-animal conflict and rescue, which is a different lens than the iNaturalist sightings.

## How it is built

`build_data.py` fetches everything and writes five JSON files into `data/`:

- `census.json` — every wild vertebrate species with its all-time NYC research-grade count, photo and class
- `sightings.json` — recent geotagged observations for the map
- `notable.json` — the rarest species seen recently, for the feed
- `rescues.json` — Ranger responses: recent list plus aggregates
- `meta.json` — build timestamp, headline totals, source notes

`index.html` is a single self-contained page (Leaflet + vanilla JS) that reads those files. No build step, no framework.

```bash
python3 build_data.py        # refresh all data (a few minutes; polite rate limiting)
python3 -m http.server 8731  # then open http://localhost:8731
```

## Refresh

A GitHub Action (`.github/workflows/refresh.yml`) re-runs `build_data.py` once a day and commits the updated `data/` files, so the map and the notable feed stay current without manual work.

## Credit

Built by Josh Greenman. Observation data © iNaturalist contributors under their respective Creative Commons licenses. Rescue data courtesy of the City of New York (NYC Open Data). This project is not affiliated with iNaturalist or the City of New York.
