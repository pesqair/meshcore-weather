# Meshcore Weather

**Off-grid weather data infrastructure for [Meshcore](https://meshcore.co) LoRa mesh networks.** Fetches NWS EMWIN weather products (forecasts, warnings, radar, observations, storm reports, etc.), parses them with canonical NWS tooling, and broadcasts them on a LoRa mesh channel as compact structured binary messages that any subscribed client — phone apps, web clients, standalone hardware displays — can decode offline, without the internet.

```
┌──────────────┐     ┌─────────────────┐     ┌──────────────────┐
│  GOES-16     │     │                 │     │  #aus-meshwx-v4  │     ┌─────────────┐
│  (future SDR)├────►│  meshcore-      ├────►│  LoRa channel    ├────►│  iOS app    │
└──────────────┘     │  weather        │     │                  │     └─────────────┘
                     │                 │     │  0x11 Radar      │     ┌─────────────┐
┌──────────────┐     │  • fetch        │     │  0x12 QPF        ├────►│  Web client │
│  NOAA EMWIN  │     │  • parse (pyIEM)│     │  0x20 Warning    │     └─────────────┘
│  internet    ├────►│  • schedule     │     │  0x30 Obs        │     ┌─────────────┐
└──────────────┘     │  • broadcast    │     │  0x31 Forecast   ├────►│  E-ink      │
                     └────────┬────────┘     │  0x38 Fire Wx    │     │  dashboard  │
                              │              │  0x3C Nowcast     │     │  (future)   │
                              ▼              │  0xF0 Beacon      │
                    ┌─────────────────┐      └──────────────────┘
                    │ Web admin portal│
                    │ localhost:8080  │
                    └─────────────────┘
```

## What this gives you

- **A working operator node** that can run on a Raspberry Pi or any Linux/macOS box with a LoRa serial radio attached. Docker-compose one-liner.
- **A structured binary protocol** (MeshWX v4) designed for one-way broadcast to passive receivers. Small messages ship as single frames; large products (64x64 radar) are split into FEC-protected quadrants with XOR parity recovery. Every message is COBS-encoded so the firmware's companion protocol doesn't truncate at null bytes.
- **Discovery protocol** — bots announce themselves on `#meshwx-discover` with 0xF0 beacons containing their data channel name, coverage radius, and capability flags. Clients auto-discover and join without manual channel configuration.
- **A per-job broadcast schedule system** with a web admin UI. Operators define arbitrary `(product, location, interval)` jobs via the portal — e.g. "radar every 15 min", "Austin METAR every 30 min", "TX storm reports every 10 min", "EWX outlook every 12 hr". Jobs persist across restarts.
- **Preload bundle** (`client_data/`, ~9.9 MB) that ships with every client app — NWS zones, census places, METAR stations, WFO metadata, PFM forecast points, zone polygons. With this preloaded, broadcasts only carry compact IDs instead of full names, slashing airtime.
- **pyIEM-powered parsing** — the reference Python library for NWS text products (VTEC, UGC, CAP standards). Runs fully offline with a `legacy_dict` UGC provider built from bundled zones data.
- **Canonical NWS data quality**: forecasts from PFM (Point Forecast Matrix) tables, warnings with correct VTEC extraction and polygon winding, absolute expiry timestamps so clients always know exactly when data becomes invalid.
- **Optional DM-based request path** for clients that want to ask for specific data on demand (e.g. iOS city search). Responses go out on the broadcast channel so every client benefits from one request.
- **Legacy text-command interface** that lets a human user on the mesh DM the bot in plain English (`wx austin`, `forecast dallas tx`, `warn OK`) and get text replies. Secondary to the binary protocol but still works.

## Status

| Area | State |
|---|---|
| EMWIN data ingestion (internet) | ✅ production |
| EMWIN data ingestion (GOES SDR) | ⏳ stubbed, pending SDR hookup |
| pyIEM canonical product parsing | ✅ shipped |
| MeshWX v4 binary wire format (all products) | ✅ shipped |
| v4 FEC with XOR parity recovery | ✅ shipped |
| Discovery beacons (#meshwx-discover) | ✅ shipped |
| Broadcast schedule system + web portal UI | ✅ shipped |
| iOS client (DigitainoMesh) | ✅ shipped |
| Web client (meshwx-client) | ✅ shipped |
| RIDGE radar extraction (CONUS + single-site) | ✅ shipped |
| Text-command interface | ✅ shipped (legacy) |
| Standalone e-ink dashboard (consumer) | 💡 idea parked in `docs/Future_EInk_Dashboard.md` |

## Wire format at a glance

All broadcasts go on the configured data channel (e.g. `#aus-meshwx-v4`). All messages are COBS-encoded. V4 frames wrap inner message types in a 6-byte header with FEC support. Multi-byte integers are big-endian unless noted.

| Byte 0 | Name | Contents |
|---|---|---|
| `0x01` | Refresh Request | client → bot DM (legacy v1 path) |
| `0x02` | Data Request | client → bot DM, product + location |
| `0x03` | Not Available | bot → client, "data not available" response |
| `0x04` | v4 Frame | v4 wrapper with FEC flags, group total, sequence number |
| `0x10` | Radar Grid | 16×16 4-bit reflectivity, one per region (legacy) |
| `0x11` | Radar Compressed | 32×32 or 64×64 sparse/RLE grid, multi-chunk with FEC |
| `0x12` | QPF Grid | Quantitative precipitation forecast (same encoding as 0x11) |
| `0x20` | Warning Polygon | Storm-specific polygon + VTEC metadata + headline |
| `0x21` | Warning Zones | Multi-zone advisory, compact zone-coded |
| `0x30` | Observation | Current conditions (temp, dewpoint, wind, sky, vis, pressure) |
| `0x31` | Forecast | 7 daily periods with high/low/sky/PoP/wind/flags |
| `0x32` | Outlook | HWO hazards day-1 and days-2-7 |
| `0x33` | Storm Reports | Up to 16 confirmed LSRs with magnitude |
| `0x34` | Rain Obs | Cities currently reporting precipitation |
| `0x35` | METAR | Station observation (same format as 0x30) |
| `0x36` | TAF | Terminal aerodrome forecast snapshot |
| `0x37` | Warnings Near | List of active warnings affecting a location |
| `0x38` | Fire Weather | FWF forecast — wind, humidity, temp, Haines index, lightning risk |
| `0x3A` | Daily Climate | Daily high/low/precip/snowfall by city (RTP) |
| `0x3C` | Nowcast | Short-term forecast with urgency flags (NOW) |
| `0x40` | Text Chunk | Reserved for dictionary-compressed text fallback |
| `0xF0` | Beacon | Discovery response — data channel name, coverage, capabilities |
| `0xF1` | Discovery Ping | Client → all bots on #meshwx-discover |

Full byte-level specs: **`docs/MeshWX_Protocol_v3.md`** (v3 base format) and **`docs/MeshWX_Protocol_v4_Design.md`** (v4 frame wrapper + FEC).

## For client developers (iOS, web, embedded)

Start here: **`docs/v4_client_guide.md`**. It covers the v4 frame format, FEC recovery, and how to decode all message types.

Also see:
- **`docs/iOS_Developer_Brief.md`** — integration guide covering wire format, COBS decoding, data request flow, preload bundle layout, and debugging
- **`docs/MeshWX_Protocol_v3.md`** — canonical byte-level wire format spec for inner message types
- **`docs/MeshWX_Protocol_v4_Design.md`** — v4 frame header, FEC group assembly, quadrant recovery
- **`docs/Future_EInk_Dashboard.md`** — parked project idea for a standalone e-ink hardware display
- **`docs/puerto_rico_radar.md`** — notes on PR/Hawaii/Alaska single-site radar extraction

## For operators

### Configure your coverage once via `.env`

```bash
MCW_HOME_CITIES=Austin TX,San Antonio TX        # Cities to broadcast obs+forecast for
MCW_HOME_STATES=TX                              # States for warning filtering
MCW_HOME_WFOS=EWX,FWD,HGX,SJT                   # NWS offices — narrows radar + warnings
```

Coverage determines which radar regions are broadcast, which warnings get filtered to your area, and which home cities get proactive obs/forecast broadcasts. On first run, the bot synthesizes a default broadcast schedule from your coverage config.

### Then manage everything else from the web admin

Open **`http://localhost:8080/schedule`** and you'll see a live table of all broadcast jobs with their last-run / next-run / bytes-sent stats. From there:

- **Add a new job** — pick a product (radar, observation, forecast, outlook, storm_reports, rain_obs, metar, taf, warnings, warnings_near, fire_weather, nowcast, qpf), pick a location type (station, zone, wfo, pfm_point, region, coverage, city), enter the location ID, set the interval, save.
- **Enable/disable** jobs without deleting them
- **Edit** intervals, names, or targets
- **Run now** to force-broadcast a job immediately regardless of schedule
- **Delete** jobs

Changes take effect within 30 seconds (the scheduler picks up config changes on its next tick) — no restart required. The config persists at `data/broadcast_config.json` across deploys because `data/` is a Docker volume.

### Other portal pages

- `/` — dashboard with bot state, coverage summary, and live activity feed
- `/data` — live map of active warnings and latest radar
- `/schedule` — broadcast schedule management with CRUD
- `/status` — radio connection state, contact list, manual broadcast trigger
- `/config` — read-only view of coverage (edit via `.env` + restart)

### Example schedule you might configure

```
ID                Name                       Product        Location       Interval
─────────────────────────────────────────────────────────────────────────────────
radar-coverage    Radar (3 TX regions)       radar          coverage       15 min
warnings-coverage Active TX warnings         warnings       coverage       5 min
obs-austin-tx     Austin current wx          observation    city:Austin TX 30 min
obs-san-antonio   San Antonio current wx     observation    city:San...    30 min
forecast-austin   Austin 7-day forecast      forecast       city:Austin TX 2 hr
forecast-sa       San Antonio 7-day          forecast       city:San...    2 hr
ewx-hwo           EWX hazardous outlook      outlook        city:Austin TX 12 hr
tx-storm-reports  TX storm reports           storm_reports  city:Austin TX 10 min
kaus-taf          KAUS TAF snapshot          taf            station:KAUS   60 min
kdfw-taf          KDFW TAF snapshot          taf            station:KDFW   60 min
fire-wx-austin    Austin fire weather        fire_weather   city:Austin TX 6 hr
nowcast-austin    Austin nowcast             nowcast        city:Austin TX 1 hr
```

## Quick start

### With Docker (recommended)

```bash
git clone https://github.com/digitaino/meshwx.git
cd meshwx
cp .env.example .env
# Edit .env with your MCW_SERIAL_PORT, MCW_MESHWX_CHANNEL, MCW_HOME_*, etc.

docker compose up -d
```

The container will:

1. Connect to your configured serial radio (or TCP radio proxy)
2. Start fetching EMWIN data from NOAA every 2 minutes
3. Bootstrap a default broadcast schedule from your `.env` coverage config
4. Launch the web admin portal on `http://localhost:8080`
5. Start the broadcast scheduler

### Without Docker

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[radar,portal]"
cp .env.example .env
# Edit .env
meshcore-weather
```

### First-run verification

Once the bot is running, you should see log lines like:

```
INFO schedule.store: Bootstrap schedule: 4 default jobs (1 home cities → obs+forecast pairs)
INFO scheduler: Broadcast scheduler started: 4 jobs, tick every 30s
INFO portal.server: Portal running at http://0.0.0.0:8080
INFO radio: Listening on channel 3 (#digitaino-wx-bot)
INFO radio: Data channel 4 (#aus-meshwx-v4)
```

Visit `http://localhost:8080/schedule` in a browser and you should see your 4 default jobs ticking over with live stats.

## CLI tools

The bot ships with a `meshcore-weather-cli` helper for operations and debugging:

```bash
meshcore-weather-cli fetch              # Fetch EMWIN products from NOAA into local cache
meshcore-weather-cli query "Austin TX"  # Run the text-command parser against stored data (no radio)
meshcore-weather-cli interactive        # Simulate mesh commands in a local REPL
meshcore-weather-cli contacts           # List known contacts on the radio device
meshcore-weather-cli remove <name>      # Remove a contact by name
meshcore-weather-cli clear-contacts     # Remove all contacts (fresh start)
```

## Configuration reference

All settings are environment variables prefixed with `MCW_`. See `.env.example` for the full list. Most commonly adjusted:

| Variable | Default | Description |
|----------|---------|-------------|
| `MCW_SERIAL_PORT` | `/dev/cu.usbserial-0001` | Serial port or `tcp://host:port` for a networked radio |
| `MCW_SERIAL_BAUD` | `115200` | Serial baud rate |
| `MCW_MESHCORE_CHANNEL` | `#digitaino-wx-bot` | Channel for text commands (never `0`/public) |
| `MCW_MESHWX_CHANNEL` | *(empty)* | Channel for v4 binary data broadcasts (e.g. `#aus-meshwx-v4`) |
| `MCW_MESHWX_DISCOVER_CHANNEL` | `#meshwx-discover` | Discovery beacon channel |
| `MCW_MESHWX_RADAR_GRID_SIZE` | `32` | Default radar grid size (16, 32, or 64) |
| `MCW_HOME_CITIES` | *(empty)* | Comma-separated cities to seed the default schedule |
| `MCW_HOME_STATES` | *(empty)* | Comma-separated states for warning filtering |
| `MCW_HOME_WFOS` | *(empty)* | Comma-separated WFOs for coverage filtering |
| `MCW_EMWIN_SOURCE` | `internet` | `internet` or `sdr` (future) |
| `MCW_EMWIN_POLL_INTERVAL` | `120` | EMWIN refresh interval in seconds |
| `MCW_EMWIN_MAX_AGE_HOURS` | `12` | Expire products older than this |
| `MCW_PORTAL_ENABLED` | `false` | Set to `true` to enable the web admin portal |
| `MCW_PORTAL_HOST` | `0.0.0.0` | Portal bind address |
| `MCW_PORTAL_PORT` | `8080` | Portal port |
| `MCW_ADMIN_KEY` | *(empty)* | Pubkey prefix of the admin user for DM admin commands |
| `MCW_LOG_LEVEL` | `INFO` | Log level |

Once the bot is running, **the broadcast schedule is managed via `data/broadcast_config.json` and the portal**, NOT via environment variables. Env vars are bootstrap config only.

## Data sources

Every message broadcast is derived from an official NWS product ingested via EMWIN. Parsing is done by [pyIEM](https://github.com/akrherz/pyIEM) wherever possible, with custom parsers where pyIEM doesn't cover a specific product (notably the PFM column-position parser in `parser/pfm.py`).

Supported product types:

| Product | Source | Produces |
|---|---|---|
| **PFM** | Point Forecast Matrix | `0x31` Forecast (structured numeric data, daily aggregates) |
| **ZFP** | Zone Forecast Product | `0x31` Forecast fallback (narrative regex extraction) |
| **RWR** | Regional Weather Roundup | `0x30` Observation, `0x34` Rain Obs |
| **METAR** | SAH/aviation | `0x30` Observation, `0x35` METAR |
| **TAF** | Terminal Aerodrome Forecast | `0x36` TAF snapshot |
| **HWO** | Hazardous Weather Outlook | `0x32` Outlook (day-1 and days-2-7 hazards) |
| **LSR** | Local Storm Reports | `0x33` Storm Reports |
| **FWF** | Fire Weather Forecast | `0x38` Fire Weather (wind, RH, temp, Haines, lightning) |
| **NOW** | Short Term Forecast | `0x3C` Nowcast (urgency flags, text) |
| **RTP** | Regional Temp/Precip | `0x3A` Daily Climate (high/low/precip/snow) |
| **SVR/SVS/TOR/FFW/FLW/FLS/WSW/NPW/RFW/MWW/SPS/...** | NWS warnings | `0x20`/`0x21` Warning broadcasts with VTEC metadata |
| **NEXRAD composite** | IEM hosted PNG | `0x11` Radar Grid (32×32 or 64×64 per region, FEC-protected) |
| **NWS RIDGE** | GOES HRIT/EMWIN GIF | `0x11` Radar Grid (CONUS, PR, HI, AK single-site extraction) |
| **QPF** | Quantitative Precip Forecast | `0x12` QPF Grid (same encoding as radar) |

Warnings include canonical VTEC event tracking (phenomenon / significance / action / ETN / office), correct polygon winding, both zone (`TXZ192`) and county FIPS (`TXC029`) UGC support, and absolute expiry timestamps so clients never display stale warnings.

## Text-command interface (legacy)

The bot also supports a human-friendly text command interface via channel messages or DMs. This is the **original** interface and predates the binary protocol. It still works and is useful for debugging the data pipeline from a phone or terminal without needing a custom client, but the binary protocol is the primary integration path going forward.

### Overview commands

| Command | Description |
|---------|-------------|
| `wx` | National overview |
| `wx TX` or just `TX` | State overview |
| `wx Austin TX` | City-level conditions, observations, forecast |
| `help` | List commands |
| `more` | Next page of a truncated response |

### Detailed commands

| Command | Example | Description |
|---------|---------|-------------|
| `forecast <city ST>` | `forecast Miami FL` | Zone forecast or discussion summary |
| `warn` / `warn <ST>` / `warn <city ST>` | `warn KS` | Warning listing at various granularities |
| `outlook <city ST>` | `outlook Des Moines IA` | 1-7 day hazardous weather outlook |
| `rain` / `rain <ST>` | `rain FL` | Areas reporting rain |
| `storm` / `storm <ST>` | `storm SD` | Local storm reports |
| `metar <ICAO>` | `metar KJFK` | Raw METAR |
| `taf <ICAO>` | `taf KJFK` | Terminal aerodrome forecast text |

Both 3-letter (IATA/FAA) and 4-letter (ICAO) station codes work: `wx AUS` = Austin-Bergstrom, `wx KJFK` = JFK NYC, `wx SJU` = San Juan PR.

### Hybrid DM/channel transport

The bot uses a channel-with-DM-fallback routing system to keep channel spam low:

1. New users send commands on the channel and get a few free replies plus a prompt to send an advert
2. When a user adverts, the bot detects it, re-adverts itself, and sends a DM welcome
3. After that, responses go DM-first automatically
4. If DMs break (user deleted the bot contact), the bot detects the failure and falls back to channel with a nudge to re-advert

Text commands have a 5-second per-user rate limit. Binary data requests (`WXQ` / `MWX` prefixed DMs) bypass this because they have their own per-`(data_type, location)` 5-minute rate limit at the broadcaster level.

### Admin commands

Authenticated by `MCW_ADMIN_KEY` (pubkey prefix), available via DM only:

| Command | Description |
|---------|-------------|
| `admin` | Show admin help |
| `contacts` | List all known contacts |
| `remove <name>` | Remove a specific contact |
| `clear-contacts` | Remove ALL contacts from the device |
| `advert` | Send a flood advert + refresh contacts |
| `refresh` | Reload contacts from the device |

## Architecture

```
meshcore_weather/
├── config.py              # Settings loaded from env vars (pydantic-settings)
├── main.py                # Entry point, DM/channel routing, command dispatch
├── nlp.py                 # Typo-tolerant text command parser
├── activity.py            # In-memory activity log + SSE streaming for portal
├── cli.py                 # CLI helpers for testing + radio admin
│
├── emwin/
│   └── fetcher.py         # EMWIN ingestion (internet now, SDR stubbed)
│
├── parser/
│   ├── weather.py         # NWS text product parsing + text-command queries
│   └── pfm.py             # PFM column-position parser + daily downsampler
│
├── protocol/              # MeshWX v4 binary wire format
│   ├── meshwx.py          # Pack/unpack for every message type, COBS, v4 frame wrapper
│   ├── fec.py             # Forward error correction — XOR parity for multi-unit products
│   ├── encoders.py        # Product text → binary (encode_* helpers)
│   ├── coverage.py        # Operator coverage (cities/states/WFOs → zone set)
│   ├── warnings.py        # pyIEM-backed warning extraction
│   ├── radar.py           # IEM NEXRAD composite → 32×32/64×64 grids per region + FEC
│   ├── ridge.py           # NWS RIDGE radar image extraction (CONUS, PR, HI, AK)
│   └── broadcaster.py     # Reactive: responds to 0x02 DM data requests
│
├── schedule/              # Unified broadcast schedule system
│   ├── models.py          # BroadcastJob, BroadcastConfig (pydantic)
│   ├── store.py           # Atomic JSON persistence + env-var bootstrap
│   ├── executor.py        # Product → builder registry (data-driven)
│   └── scheduler.py       # Tick loop, per-job intervals, radio transmission
│
├── portal/                # FastAPI + Jinja2 web admin
│   ├── server.py
│   ├── routes/
│   │   ├── pages.py       # HTML endpoints (dashboard, schedule, config, status, data)
│   │   └── api.py         # JSON API (schedule CRUD, warnings, actions)
│   ├── templates/         # Jinja2 templates
│   └── static/            # CSS + bundled vendor libs (MapLibre, HTMX)
│
├── client_data/           # Preload bundle shipped to clients (package-data)
│   ├── zones.json         # NWS forecast zones
│   ├── places.json        # US Census places
│   ├── stations.json      # METAR stations
│   ├── wfos.json          # NWS Weather Forecast Offices
│   ├── state_index.json   # State/marine prefix → 1-byte index
│   ├── protocol.json      # Protocol version + enum reference
│   ├── pfm_points.json    # PFM forecast points (name, WFO, lat/lon, zone)
│   ├── regions.json       # Radar region definitions + bounds
│   ├── zones.geojson      # Simplified zone polygons for map rendering
│   └── weather_dict.json  # Reserved for future dict text compression
│
├── geodata/               # Source data for the client_data bundle
│   └── *.json
│
└── meshcore/
    └── radio.py           # Meshcore radio interface: channels, DMs, adverts
```

## Docs

- `docs/MeshWX_Protocol_v3.md` — canonical byte-level wire format spec for inner message types
- `docs/MeshWX_Protocol_v4_Design.md` — v4 frame header, FEC group assembly, quadrant recovery design
- `docs/v4_client_guide.md` — practical v4 client integration guide
- `docs/iOS_Developer_Brief.md` — integration guide for iOS / web / embedded client developers
- `docs/puerto_rico_radar.md` — notes on PR/HI/AK single-site radar extraction via RIDGE
- `docs/Future_EInk_Dashboard.md` — parked project idea for a standalone e-ink hardware display

## Safety

- **Channel isolation**: the bot will never transmit on channel 0 (public) or any channel other than its configured ones. Enforced at both the message handler and radio driver layers.
- **Text-command rate limit**: 5 seconds per user for text DMs (human-user protection)
- **Binary-request rate limit**: 5 minutes per `(data_type, location)` tuple on the broadcaster (multi-client broadcast amortization)
- **DM fallback**: if DMs fail, the bot detects it and falls back to channel responses gracefully
- **Channel spam limits**: unknown contacts get a small number of free channel replies then must advert
- **Input sanitization**: text commands are length-limited and stripped of control characters
- **Admin authentication**: admin commands require matching `MCW_ADMIN_KEY` pubkey prefix — cannot be spoofed via channel

## Roadmap

Shipped:

- [x] Internet-based EMWIN data fetching with disk cache
- [x] pyIEM canonical NWS product parsing (VTEC, UGC, polygons)
- [x] MeshWX v4 binary protocol (all message types wired)
- [x] v4 FEC with XOR parity recovery for multi-unit products
- [x] Discovery beacons on #meshwx-discover
- [x] COBS-encoded wire format (survives firmware null-byte truncation)
- [x] Absolute Unix-minute expiry timestamps (no client-side countdown drift)
- [x] PFM forecast source (structured numeric data, displacing ZFP narrative regex)
- [x] Compressed radar grids (32×32 and 64×64 with sparse/RLE encoding)
- [x] RIDGE radar extraction (CONUS composite + single-site for PR, HI, AK)
- [x] Fire weather forecasts (FWF → 0x38)
- [x] Nowcasts (NOW → 0x3C) with urgency flags
- [x] Daily climate summaries (RTP → 0x3A)
- [x] QPF precipitation grids (0x12)
- [x] Unified per-job broadcast schedule system (any product, any location, any interval)
- [x] Web admin portal with schedule management + CRUD API + activity feed
- [x] Preload bundle (`client_data/`) with PFM points, zone polygons, places, stations
- [x] Data request reactive path (`WXQ` DM + broadcast response)
- [x] Legacy text-command interface with typo-tolerant parser
- [x] Hybrid DM/channel routing with admin commands
- [x] Docker container with serial passthrough
- [x] iOS client (DigitainoMesh)
- [x] Web/desktop client (meshwx-client)

Planned:

- [ ] GOES-E SDR satellite downlink via goesrecv/goestools
- [ ] Proactive warning push (immediate broadcast when NEW warnings land, not waiting for next scheduled tick)
- [ ] H-VTEC hydrologic metadata (flood severity, river ID, stage forecast)
- [ ] Dictionary text compression for warning headlines
- [ ] 3-hourly hour-by-hour PFM forecast format
- [ ] Standalone e-ink weather display hardware product (see `docs/Future_EInk_Dashboard.md`)

## License

MIT

## Related

- **[meshwx-client](https://github.com/digitaino/meshwx-client)** — Desktop & web client for receiving and displaying MeshWX weather data. Connects via USB or Bluetooth.
- **[DigitainoMesh](https://github.com/digitaino/DigitainoMesh)** — iOS MeshCore client with built-in MeshWX weather decoding.
