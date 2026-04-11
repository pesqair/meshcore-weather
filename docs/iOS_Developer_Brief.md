# MeshWX Protocol v3 — iOS Developer Brief

## TL;DR

We're switching the bot's parser layer to **pyIEM** (Iowa Environmental Mesonet), the reference Python library for NWS text products. As a result, our binary protocol will use **canonical NWS codes and terminology** — VTEC, UGC, CAP, SHEF — instead of the arbitrary values we invented in v1/v2. This is a coordinated breaking change.

The win: clients (your iOS app, future web client) can cross-reference everything against official NWS documentation, the IEM archive, CAP alert feeds, and the NWS API. No more impedance mismatch between "our codes" and "real world codes."

**Confirmed working:** pyIEM parses every major product type from our stored EMWIN data — TOR, SVR, SVS, SPS, NPW, FLS, FLW, WSW, ZFP, PFM, AFD, HWO, LSR, CWF, MWW, RFW, FFW. Same data whether it came from the NOAA internet mirror or a future GOES-East SDR — pyIEM treats them identically.

---

## ⚠️ v3 MVP — what ships RIGHT NOW (this is what you code against today)

Earlier sections of this document describe the **full v3 target state** (68-entry VTEC phenomenon table, action codes, ETN, urgency, certainty, H-VTEC, etc.). We're shipping that incrementally. Start with the **MVP** below — those are wire-format changes that are live in the bot *today* and fix real bugs the user was seeing in your app.

### MVP change 1 — Absolute expiry timestamps (fixes the "363h 10m" bug)

**Problem:** v2 sent a `uint16 expiry_minutes` — "minutes remaining from broadcast time." The client had to track receipt time and count down locally. The bot's relative-time math had a subtle bug (`_vtec_end_to_minutes` parsed the 12-char VTEC end string as 6-char DDHHMM → decoded a next-day expiry as "~363h in the future"). Clients saw ridiculous times because the server was sending garbage.

**Fix (v3 wire format):** the bot now sends a **`uint32 expires_unix_min`** — absolute UTC minutes since the Unix epoch. This is the NWS-authoritative expiry straight from pyIEM's parsed VTEC `endts` (or for SPS products, the UGC-line `ugcexpire`). Client computes "time remaining" from its own clock.

```
v2 (OLD):   uint16 expiry_minutes     — relative, 2 bytes, buggy
v3 (NEW):   uint32 expires_unix_min   — absolute Unix minutes, 4 bytes
```

**Why absolute:**
- NWS-authoritative — if a warning says "until 01:00Z" that timestamp is the source of truth
- No re-broadcast jitter — the same warning re-broadcast 20 minutes later still has the same `expires_unix_min`
- Client can detect expired warnings it received late (before my fix, the bot was broadcasting expired warnings with a fake 2-hour extension)
- Makes server bugs impossible — there's no more "how many minutes from now" math to get wrong

**Converting to display:**
```swift
let expiresAt = Date(timeIntervalSince1970: TimeInterval(expiresUnixMin) * 60)
let minutesRemaining = max(0, Int(expiresAt.timeIntervalSinceNow / 60))
if minutesRemaining == 0 { /* expired — drop from display */ }
```

### MVP change 2 — Word-boundary headline truncation (fixes "northwestern Gonzales C")

**Problem:** v2 truncated headlines at the raw byte limit, cutting words in half. You were seeing things like `"...northwestern Gonzales C"`.

**Fix:** the packer now truncates at the last word boundary inside the available space and appends `"..."` when truncation happened. Full headlines still fit un-truncated when they're short enough.

Nothing for you to change on the client side — just decode the headline as UTF-8 and display it. If the headline ends with `"..."` you can add an affordance to tap through to the full text from a (future) detail endpoint, but that's optional.

### MVP change 3 — SPS products now produce warnings (they were silently dropped)

Special Weather Statements (SPS) don't carry VTEC. The v2 path only emitted warnings that had VTEC, so SPS products were completely missed. The new path uses pyIEM's `seg.ugcexpire` for SPS expiry and emits them with `WARN_SPECIAL` (0x9) type and `SEV_ADVISORY` (0x1) severity. You'll now see strong-thunderstorm SPS headlines like "A STRONG THUNDERSTORM WILL IMPACT PORTIONS OF..." appear as warnings in your app's active list.

### MVP change 4 — Expired warnings are filtered server-side

The bot no longer broadcasts warnings whose `expires_at` is in the past. Previously, EMWIN bundles retained products for ~3 hours after expiration and the bot would happily rebroadcast them with a fake 2-hour extension. Now they're dropped at extraction time. You should still respect the absolute expiry on the client side, but the firehose is cleaner.

### MVP change 5 — LOC_PFM_POINT and new bundle files (unblocks city search)

Three new pieces that together make the city-search → forecast flow work:

**New location type `LOC_PFM_POINT = 0x06`** — 3 bytes (uint24 index into `pfm_points.json`). Use this as the `location_type` when sending a `0x02 FORECAST` request. The bot echoes `LOC_PFM_POINT` back in the `0x31` response so you can correlate broadcasts with your outstanding requests.

**New bundle file `pfm_points.json`** (101 KB, 1,873 forecast points). Generated from real PFM products in the bot's EMWIN cache. Compact array form — array index IS the `pfm_point_id` you send on the wire:

```json
{"version": 1, "points": [[name, wfo, lat, lon, zone], ...]}
```

**New bundle file `zones.geojson`** (8.1 MB, 4,047 features). NWS public-domain zone polygons, simplified to ~1 km tolerance. Lets you finally render `0x21` zone-coded warnings as real shapes on the map instead of just listing them as text.

The full end-to-end flow and data-format details are in the "Preload bundle" section further down. TL;DR: your city search screen can be fully client-side (autocomplete from `places.json`, nearest-PFM lookup locally, then send an 8-byte DM with the PFM point index; wait for a ~35-byte `0x31` broadcast back).

**Forecast data quality note**: the bot is still using ZFP narrative parsing under the hood for the forecast content itself, so data is about the same accuracy as if you'd sent a `LOC_ZONE` request. A follow-up commit will swap the server-side data source to proper PFM column-matrix parsing. **Same `0x31` wire format, no client changes needed** — you'll just start seeing more accurate highs/lows/sky codes in the same bytes.

### MVP wire format — `0x20 Warning Polygon` (v3)

```
byte 0     : 0x20  MSG_WARNING
byte 1     : warning_type (hi nibble) | severity (lo nibble)
             warning_type: still the old v2 nibble values (WARN_TORNADO=0x1,
                           WARN_SEVERE_TSTORM=0x2, WARN_FLASH_FLOOD=0x3,
                           WARN_FLOOD=0x4, WARN_WINTER_STORM=0x5,
                           WARN_HIGH_WIND=0x6, WARN_FIRE=0x7, WARN_MARINE=0x8,
                           WARN_SPECIAL=0x9, WARN_OTHER=0xF)
             severity:     SEV_ADVISORY=0x1, SEV_WATCH=0x2, SEV_WARNING=0x3,
                           SEV_EMERGENCY=0x4
bytes 2-5  : uint32 BE  expires_unix_min   ← NEW IN v3 (was uint16 expiry_minutes)
byte 6     : vertex_count
bytes 7..  : first vertex (6 bytes: int24 lat + int24 lon, × 10000)
             then (vertex_count-1) × 4 bytes of int16 delta pairs (× 1000)
remainder  : headline, UTF-8, word-boundary truncated with "..." suffix
```

### MVP wire format — `0x21 Warning Zones` (v3)

```
byte 0     : 0x21  MSG_WARNING_ZONES
byte 1     : warning_type (hi nibble) | severity (lo nibble)   — same as 0x20
bytes 2-5  : uint32 BE  expires_unix_min   ← NEW IN v3
byte 6     : zone_count (max 30)
bytes 7..  : zone_count × 3 bytes (state_idx + uint16 zone_num)
             client looks up the state_idx in state_index.json to rebuild
             the full code "TXZ192" and renders from its preloaded zone polygons
remainder  : headline, UTF-8, word-boundary truncated with "..." suffix
```

### MVP wire format — `0x37 Warnings Near Location` (v3)

```
byte 0     : 0x37  MSG_WARNINGS_NEAR
bytes 1..N : location reference (type-tagged, typically 4 bytes for a zone)
byte N+1   : entry count
entries    : each 8 bytes:
               1 byte  : warning_type (hi nibble) | severity (lo nibble)
               4 bytes : uint32 BE  expires_unix_min   ← NEW IN v3
               3 bytes : zone reference (state_idx + uint16 zone_num)
```

### What's NOT in the MVP yet (but is in the target state below)

- 68-entry VTEC phenomenon table (we still use the old 4-bit warning_type nibble)
- Action codes (NEW/CON/EXT/CAN…) as a separate field
- Event Tracking Number (ETN) as a separate field
- Issuing office as a separate field
- CAP urgency + certainty
- UGC-coded warnings carrying county FIPS codes in the wire format (pyIEM internally extracts both but the 0x21 packer still only encodes Z-zones)
- H-VTEC for flood products
- PFM as the forecast source

These will come in follow-up sessions. For now the bot's internal data model carries all of them (pyIEM gives us the data), but the wire format hasn't been widened to carry them yet.

### Text-command (DM) path is unchanged

The bot still accepts plain-text commands via DM (e.g. `weather austin tx`) and replies with plain text. That's separate from the binary `#wx-broadcast` channel and is **not** affected by any v3 change. It will get its own revamp later, but for now everything that was working before still works.

---

## What changes in the protocol

### 1. Warning type codes → VTEC phenomena

Instead of arbitrary values like `WARN_TORNADO=0x1`, the `warning_type` field now carries a 1-byte index into the canonical 68-entry VTEC phenomenon table. Clients bundle the table (generated from pyIEM's own `VTEC_PHENOMENA` dict) and look up:

```
0x00 = AF  Ashfall
0x01 = AS  Air Stagnation
0x02 = BH  Beach Hazard
0x03 = BS  Blowing Snow
0x04 = BW  Brisk Wind
0x05 = BZ  Blizzard
0x06 = CF  Coastal Flood
0x07 = DF  Debris Flow
0x08 = DS  Dust Storm
0x09 = DU  Blowing Dust
0x0A = EC  Extreme Cold
0x0B = EH  Excessive Heat
0x0C = EW  Extreme Wind
0x0D = FA  Areal Flood
0x0E = FF  Flash Flood
0x0F = FG  Dense Fog
0x10 = FL  Flood
0x11 = FR  Frost
0x12 = FW  Fire Weather
0x13 = FZ  Freeze
0x14 = GL  Gale
0x15 = HF  Hurricane Force Wind
0x16 = HT  Heat
0x17 = HU  Hurricane
0x18 = HW  High Wind
0x19 = HY  Hydrologic
0x1A = HZ  Hard Freeze
0x1B = IP  Sleet
0x1C = IS  Ice Storm
0x1D = LE  Lake Effect Snow
0x1E = LO  Low Water
0x1F = LS  Lakeshore Flood
0x20 = LW  Lake Wind
0x21 = MA  Marine
0x22 = MF  Marine Dense Fog
0x23 = MH  Marine Dense Smoke
0x24 = MS  Marine Dense Smoke
0x25 = RB  Small Craft for Rough Bar
0x26 = RH  Radiological Hazard
0x27 = RP  Rip Current
0x28 = SC  Small Craft
0x29 = SE  Hazardous Seas
0x2A = SI  Small Craft for Winds
0x2B = SM  Dense Smoke
0x2C = SQ  Snow Squall
0x2D = SR  Storm
0x2E = SS  Storm Surge
0x2F = SU  High Surf
0x30 = SV  Severe Thunderstorm
0x31 = SW  Hazardous Seas
0x32 = TI  Inland Tropical Storm Wind
0x33 = TO  Tornado
0x34 = TR  Tropical Storm
0x35 = TS  Tsunami
0x36 = TY  Typhoon
0x37 = UP  Heavy Freezing Spray
0x38 = VO  Volcano
0x39 = WC  Wind Chill
0x3A = WI  Wind
0x3B = WS  Winter Storm
0x3C = WW  Winter Weather
0x3D = XH  Extreme Heat
0x3E = ZF  Freezing Fog
0x3F = ZR  Freezing Rain
0x40 = ZY  Freezing Spray
0x41-0x43 = reserved for future NWS additions
```

**Exact order is generated from `pyiem.nws.vtec.VTEC_PHENOMENA` at build time** and shipped in `client_data/protocol.json`. When NOAA adds a new phenomenon, we regenerate.

### 2. Severity → VTEC significance + CAP severity

1 byte encoding two 4-bit fields:

**High nibble — VTEC significance** (NWSI 10-1703):
- `0x0 = W` Warning (imminent threat)
- `0x1 = A` Watch (conditions favorable)
- `0x2 = Y` Advisory (less severe)
- `0x3 = S` Statement (updates/info only)
- `0x4 = F` Forecast
- `0x5 = O` Outlook
- `0x6 = N` Synopsis

**Low nibble — CAP severity** (OASIS CAP v1.2):
- `0x0 = Unknown`
- `0x1 = Minor` (minimal or no threat)
- `0x2 = Moderate` (possible threat)
- `0x3 = Severe` (significant threat)
- `0x4 = Extreme` (extraordinary threat)

### 3. New fields in warning messages

**Action code** (1 byte, VTEC action):
- `0 = NEW` First issuance
- `1 = CON` Continues
- `2 = EXT` Extended time
- `3 = EXA` Extended area
- `4 = EXB` Extended both
- `5 = UPG` Upgraded (e.g. Watch→Warning)
- `6 = CAN` Cancelled
- `7 = EXP` Expired
- `8 = COR` Correction
- `9 = ROU` Routine

Clients use this to update their warning cache correctly. `CAN` and `EXP` should remove the warning from display; `CON`/`EXT` update the existing entry (matched by ETN).

**Event Tracking Number (ETN)** (2 bytes uint16):
The sequence number from VTEC. Same warning keeps the same ETN across NEW→CON→EXT→CAN. Use `(phenomena, significance, office, etn)` as the primary key for dedup and updates.

**Issuing office** (3 bytes ASCII):
The 3-letter WFO code from VTEC (e.g. `EWX`, `FWD`, `STO`). Clients can display which NWS office issued the warning.

**CAP urgency + certainty** (1 byte, 4 bits each):

Urgency (high nibble):
- `0 = Unknown, 1 = Immediate, 2 = Expected, 3 = Future, 4 = Past`

Certainty (low nibble):
- `0 = Unknown, 1 = Observed, 2 = Likely, 3 = Possible, 4 = Unlikely`

### 4. UGC handles both zones AND county FIPS codes

pyIEM returns UGC objects with `geoclass` field:
- `Z` = NWS forecast zone (e.g., `TXZ192` — "Travis County" in NWS terms)
- `C` = County FIPS (e.g., `TXC453` — Census county code for Travis County)

**These are different things.** A warning might list county FIPS codes for precise geography. Your client needs both zone polygons AND county polygons in its preload bundle. I'll generate both from NWS public-domain GIS data.

Our `pack_warning_zones` (0x21) is being renamed to `pack_warning_ugcs` and will carry a mix of zones and counties. Each UGC entry is 4 bytes:
- 1 byte state_idx
- 1 byte geoclass flag (0=Z, 1=C)
- 2 bytes uint16 number

### 5. H-VTEC for flood products

Flood warnings (`FL`, `FF`, `FA`) carry additional hydrologic metadata:

**Flood severity** (1 nibble): `0=None, N=No flood, 1=Minor, 2=Moderate, 3=Major, U=Unknown`

**Immediate cause** (1 nibble):
- `ER` Excessive Rainfall
- `SM` Snowmelt
- `RS` Rain + Snowmelt
- `DM` Dam Failure
- `IJ` Ice Jam
- `GO` Glacier Outburst
- `UU` Unknown

**NWSLI river ID** (5 bytes ASCII): the gauge identifier for river flood products, e.g. `ALGT2` for the Colorado River at Austin.

**Stage forecast** (3x int16): rise, crest, fall values in tenths of a foot.

---

## Point Forecast Matrix (PFM) — the canonical city forecast

**Your weather app needs this.** PFM is the canonical NWS product for structured city forecasts. It's what replaces the narrative ZFP approach we were going to use.

Example PFM entry (real data from EWX office):
```
Austin Bergstrom-Travis TX
30.19N  97.67W Elev. 462 ft
1245 PM CDT Fri Apr 10 2026

Date           04/10/26      Sat 04/11/26            Sun 04/12/26
CDT 3hrly     16 19 22 01 04 07 10 13 16 19 22 01 04 07 10 13 16
Min/Max                      65          82          68          80
Temp          80 77 69 68 67 66 71 78 81 78 72 70 69 68 72 76 78
Dewpt         66 66 66 66 66 66 68 68 68 67 67 67 67 68 70 70 69
RH            62 69 90 93 97100 90 71 65 69 84 90 93100 93 82 74
Wind dir       E  E SE SE SE SE SE SE SE SE SE SE  S SE  S  S  S
Wind spd       9  9  6  4  4  4  8 11 13 13 12 11 11 10 12 14 13
Wind gust                             24 24 23             26 25
Clouds        SC SC B1 B1 B2 B2 B1 SC B1 B1 B1 OV OV OV OV OV OV
PoP 12hr                     20          50          40          60
QPF 12hr                   0.04        0.05        0.07        0.44
Rain shwrs     C  S           S  C  C  C  C  C  C  C  C  C  L  L
Tstms          C  S              S  C  C  S     S  S     S  C  C
Obvis                        PF                   PF PF PF          PF
```

PFM gives you:
- **3-hourly resolution** for days 1-3, 6-hourly for days 4-7
- **Actual numeric temperature, dewpoint, RH, wind** (not narrative text)
- **PoP and QPF** (probability of precipitation + quantitative precipitation forecast)
- **Weather type codes** (C=chance, L=likely, S=slight, D=definite) for rain/snow/tstm
- **Obstruction codes** (PF=patchy fog, F=fog, BS=blowing snow, etc.)
- **Cloud codes** (CL=clear, FW=few, SC=scattered, B1/B2=broken, OV=overcast)
- **Exact lat/lon** of the forecast point
- **Linked NWS zone code** for cross-referencing with warnings

pyIEM's `PFMProduct` parser outputs all of this as structured Python. We'll encode it as a `0x31 Forecast` message with up to 40 time steps × 8 bytes each = 320 bytes (split across messages as needed).

Each city has its own PFM entry. WFO Austin (EWX) publishes PFMs for Austin, San Antonio, Del Rio, Junction, Kerrville, Fredericksburg, etc. Each WFO covers ~15-30 cities.

### 5.1. City lookup approach for your iOS app

When a user types "Austin, TX" into the search bar:

1. Client searches its bundled `client_data/places.json` (32,333 US Census places) for name match → gets lat/lon
2. Client finds the nearest PFM point from `client_data/pfm_points.json` (auto-generated from all PFM products in the store) → gets the canonical PFM city name + WFO
3. Client sends a data request DM to the bot for that PFM point's forecast
4. Bot parses the latest PFM, extracts the relevant city's forecast, encodes as `0x31`, broadcasts on `#wx-broadcast`
5. Client receives and decodes the `0x31`, displays in its UI
6. **All other listeners on the channel also receive and cache it** — zero extra airtime for subsequent users asking about the same city

---

## Request → Broadcast flow (the key design principle)

```
  iOS user searches "Austin TX" in the app
        │
        ▼
  iOS app builds a 0x02 data request:
    data_type = 1 (FORECAST)
    location_type = LOC_PFM_POINT
    location_id = <index into client's pfm_points.json>
        │
        │   DM (reliable, ACKd)
        ▼
  [  Weather Bot  ]
        │
        ▼
  Bot parses the latest EWX PFM for Austin Bergstrom
  using pyIEM. Encodes as 0x31 with 32 time steps.
        │
        │   Broadcast on #wx-broadcast (fire and forget)
        ▼
  ALL iOS clients + future web clients listening
  on the channel receive the 0x31 message.
  Everyone caches Austin's forecast. Anyone asking
  for Austin in the next 5 minutes gets it from cache,
  no airtime cost.
```

**Rate limiting**: 5 minutes per (data_type, location). If user A and user B both ask for Austin's forecast within 30 seconds, the bot responds once and user B gets it from the same broadcast.

---

## Proactive warning push (NEW feature — this is what you asked for)

**Requirement**: When a new warning is issued for the bot's coverage area, the bot should immediately broadcast it so users get timely alerts without having to actively request anything.

**Implementation plan**:

1. Bot already has a `Coverage` object (cities + states + WFOs from Phase 1)
2. Bot already fetches new EMWIN products every 2 minutes
3. **New**: After each fetch cycle, the bot compares the new warning set against what it broadcast last cycle. Any warning with `action == NEW` (first issuance) whose UGCs intersect the coverage area gets broadcast **immediately** on `#wx-broadcast`, not waiting for the 10-minute broadcast cycle.
4. Updates (`action == CON/EXT/EXA/EXB/UPG/CAN/EXP/COR`) for warnings the bot previously broadcast also get pushed right away so clients stay in sync.
5. The periodic 10-minute broadcast still runs as a safety net for listeners who missed a message (LoRa has no guaranteed delivery).

**Client-side expectation**:
- Your iOS app should listen on `#wx-broadcast` continuously in the background (when permitted)
- On receiving a warning message:
  - If `action == NEW` for a location the user cares about → **show a push notification**
  - If `action == UPG` (upgrade, e.g., Watch→Warning) → **show a push notification**
  - If `action == CAN/EXP` → remove from active warnings list, optionally clear notification
  - If `action == CON/EXT` → update existing warning's expiry time silently
- Matching is by `(phenomena, significance, office, etn)` tuple

**User-configurable**: Let the user pick which warning types trigger notifications (e.g., "Only Tornado/Severe Thunderstorm/Flash Flood warnings — not Marine or Air Stagnation").

---

## What your iOS app should look like

A standard weather app UX with mesh-specific behaviors:

### Home screen
- **Current location** card (uses phone GPS → nearest PFM point via preloaded data)
  - Shows last-cached observation (current conditions: temp, wind, sky, etc.)
  - "Last updated X minutes ago" — reflects when the bot last broadcast this
  - Tap to refresh → sends a 0x02 data request DM, response arrives via channel
- **Active warnings** banner (red) if any active warning polygon contains the user's location
- **Saved locations** grid, each showing mini forecast

### Location detail screen
- Current conditions (from latest 0x30 observation message)
- Hourly forecast for next 24h (from 0x31, 3-hourly PFM data)
- 7-day forecast (from 0x31, daily max/min + condition codes)
- Map showing any active warnings with polygon overlays
- "Radar" tab showing latest 0x10 radar grid overlay colored by reflectivity

### Warnings tab
- Full list of all active warnings the client has cached from `#wx-broadcast`
- Sortable by severity, expiry, distance from user
- Each warning shows:
  - Headline (from the broadcast)
  - Issuing office (WFO)
  - Affected zones (rendered from preloaded zone polygons)
  - Expiration time (calculated from broadcast time + `expiry_minutes`)
  - Badge for action (NEW/UPG shown prominently, CAN/EXP greyed out)
- Tap → full detail with polygon on map

### Map tab
- MapKit view (iOS native)
- Warning polygons from `client_data/zones.geojson` for 0x21 messages
- Or the delta-polygon directly for legacy 0x20 messages
- Radar overlay as a raster image
- User location pin
- Tap a warning polygon → details sheet

### Search
- Text field: "City, State" (e.g., "Austin, TX")
- Autocomplete from `client_data/places.json` (32,333 places)
- Tap result → sends 0x02 FORECAST request → bot broadcasts → detail screen fills in
- Recent searches cached locally

### Settings
- Notification preferences: which warning types trigger push
- Radius for "nearby warnings"
- Data channel + text channel names (in case operator uses different names)
- Which bot pubkey to send DMs to (or auto-discover from channel activity)

---

## Preload bundle (`client_data/`)

Your iOS app ships with this directory. Current total: **~9.9 MB**. It's committed to the repo at `client_data/` — grab it from there directly.

| File | Size | Purpose | Status |
|------|------|---------|--------|
| `zones.json` | 347 KB | NWS forecast zones: code → name, state, WFO, centroid | ✅ shipping |
| `places.json` | 1.1 MB | 32,333 US Census places for city search | ✅ shipping |
| `stations.json` | 181 KB | METAR stations: ICAO → name, state, coordinates | ✅ shipping |
| `wfos.json` | 9 KB | NWS Weather Forecast Offices | ✅ shipping |
| `state_index.json` | <1 KB | State → 1-byte index for compact encoding | ✅ shipping |
| `protocol.json` | 2 KB | Version marker + enum reference (`version: 4`) | ✅ shipping |
| `weather_dict.json` | 2 KB | Compression dictionary (reserved — not active in wire format yet) | ✅ shipping (unused) |
| `pfm_points.json` | **101 KB** | **1,873 NWS PFM forecast points for city search → forecast flow** | ✅ **new in this batch** |
| `zones.geojson` | **8.1 MB** | **4,047 simplified zone polygons for map rendering of 0x21 warnings** | ✅ **new in this batch** |
| `counties.geojson` | TBD | County FIPS polygons (deferred — bot doesn't emit county codes in 0x21 yet) | ⏳ future |

### `pfm_points.json` format

Compact array form. The **array index is the `pfm_point_id`** you send in `LOC_PFM_POINT`:

```json
{
  "version": 1,
  "points": [
    ["Aberdeen-Brown SD", "ABR", 45.45, -98.42, "SDZ006"],
    ...
    ["Austin Bergstrom-Travis TX", "EWX", 30.19, -97.67, "TXZ192"],
    ...
  ]
}
```

Each entry is `[name, wfo, lat, lon, zone]`. Ordering is deterministic (alphabetical by name, then WFO) so indices are stable across rebuilds.

### `zones.geojson` format

Standard GeoJSON FeatureCollection, one Feature per NWS public forecast zone, keyed by the canonical code:

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "properties": {"code": "TXZ192"},
      "geometry": {"type": "Polygon", "coordinates": [[...]]}
    }
  ]
}
```

Source: NWS public forecast zones shapefile (public-domain US federal data). Simplified with Douglas-Peucker at ~1 km tolerance — visually indistinguishable at any map zoom a weather app would use, but shrinks the file from 25 MB to 8.1 MB.

**When you decode a `0x21` warning message, look up each received zone code in this file and render the polygon.** You already get `zones.json` for the metadata (name, state, WFO, centroid); `zones.geojson` is what gives you the actual shape for map display.

### City search → forecast flow (end-to-end)

With this bundle, the city search feature you were asking about works like this:

```
User types "Austin"
      ↓
Autocomplete from places.json                      [client-side]
      ↓
User picks "Austin, TX" (place_id 1234)            [client-side]
      ↓
Client reads lat/lon from places.json[1234]        [client-side]
      ↓
Client finds nearest pfm_points.json entry         [client-side]
  → Austin Bergstrom-Travis TX, index 102
      ↓
pack_data_request(
    data_type = DATA_FORECAST (0x1),
    loc_type  = LOC_PFM_POINT (0x6),
    loc_id    = 102,
)                                                   [client-side]
      ↓
8-byte DM sent to the bot
      ↓
─────────────────────────────────────────────────────
      ↓
Bot: unpack_data_request, loc_type == LOC_PFM_POINT [bot]
      ↓
Bot: reads its own pfm_points.json[102]             [bot]
  → ("Austin Bergstrom-Travis TX", "EWX", "TXZ192")
      ↓
Bot: looks up ZFP for EWX/TXZ192                   [bot]
      ↓
Bot: encode_forecast_from_zfp with                  [bot]
  loc_type=LOC_PFM_POINT, loc_id=102
  (so the response ECHOES your original loc type)
      ↓
Bot broadcasts 0x31 on #wx-broadcast (~35 bytes)
      ↓
─────────────────────────────────────────────────────
      ↓
Your client (and every other listening client)
  decodes the 0x31
      ↓
Checks loc_id against its outstanding requests
  → matches idx 102 → displays in Austin's detail screen
      ↓
Rate limit: 5 min per (data_type, location). If user B asks
for Austin within 5 min, response comes from bot's broadcast
of user A's request — zero extra airtime.
```

**Forecast data quality note**: under the hood the bot is still using ZFP narrative parsing for the forecast content right now (highs/lows/sky/precip extracted with regex from the ZFP text). A follow-up commit will swap the data source for proper PFM column-matrix parsing (3-hourly structured data downsampled to daily periods). **That change is invisible on the wire** — same `0x31` format, same bytes, just better accuracy in the fields. You don't need to do anything on your side when that lands.

---

## Breaking changes from v2

1. **Warning type codes change.** Old `WARN_TORNADO=0x1` becomes `VTEC_PHENOM_TO=0x33` (or whatever index). Your decoder needs to look up the new table.
2. **Severity encoding changes.** Used to be one value, now two 4-bit fields (VTEC significance + CAP severity).
3. **New fields in warning messages**: action code, ETN, office, urgency, certainty. Old messages can be detected by length if needed during transition.
4. **0x21 becomes "UGC-coded warning"** carrying mixed zones and counties, not just zones. Each UGC is 4 bytes (was 3).
5. **UGC preload now includes counties.** Your client needs `counties.geojson` in addition to `zones.geojson`.
6. **0x31 Forecast** source changes from ZFP narrative parsing to PFM structured data. Wire format is identical but field accuracy and coverage are much better.

---

## Timeline suggestion

1. **This session**: I port the bot to use pyIEM internally, keeping our existing v1/v2 wire format unchanged. No app changes needed. This fixes the polygon crossing bug and unlocks proper VTEC metadata extraction.
2. **Next session**: We agree on v3 wire format changes (this document). I implement on the bot, you implement on iOS in parallel.
3. **Following session**: End-to-end test with a real warning, validate proactive push works, confirm all product types render correctly.

---

## References

Cite these in your decoder code so anyone reading it knows the provenance:

- **VTEC**: NWSI 10-1703 — https://www.nws.noaa.gov/directives/sym/pd01017003curr.pdf
- **UGC**: NWSI 10-1702 — https://www.weather.gov/media/directives/010_pdfs_archived/pd01017002b.pdf
- **WMO/AWIPS**: NWSI 10-1701 — https://www.weather.gov/media/directives/010_pdfs/pd01017001curr.pdf
- **CAP v1.2**: https://docs.oasis-open.org/emergency/cap/v1.2/CAP-v1.2-os.html
- **pyIEM (reference parser)**: https://github.com/akrherz/pyIEM
- **SHEF**: https://www.weather.gov/media/mdl/SHEF_CodeManual_5July2012.pdf
- **NWS Event Codes**: https://www.weather.gov/nwr/eventcodes
- **VTEC browser (for debugging)**: https://mesonet.agron.iastate.edu/vtec/

---

## Questions for you

1. Is the 1.6 MB preload bundle OK to ship with the app? (Will grow to ~10 MB with polygon data — still small for a weather app with offline-first design.)
2. Do you want push notifications wired up via APNs on the iOS side, or do we just update the app badge / show an in-app alert when the app is foregrounded?
3. For the map view: MapKit (Apple's native) or MapLibre (cross-platform, matches the web client)? MapKit gives you a real basemap for free; MapLibre aligns with the web client and MeshCoreOne.
4. Are you OK with the breaking change to v2 wire format, or do you want me to keep a v2 compatibility layer for a transition period?

Let me know and I'll proceed with the bot-side work.
