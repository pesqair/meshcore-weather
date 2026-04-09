# Meshcore Weather

Off-grid weather data service for [Meshcore](https://meshcore.co) LoRa radio networks. Downloads EMWIN (Emergency Managers Weather Information Network) data from GOES-East and serves it to mesh users via simple text commands — all within 136-byte message limits.

## How It Works

```
GOES-16 Satellite ──► SDR Antenna ──► This Service ──► Meshcore Radio ──► Mesh Users
         (future)        (future)          │
                                           │
NOAA Internet ─────────────────────────────┘ (current)
```

1. **EMWIN data** is fetched from NOAA internet servers (with a future path to direct satellite downlink via SDR)
2. **Weather products** (forecasts, warnings, observations, storm reports) are parsed and indexed by location
3. **Meshcore radio** receives user requests over the configured channel and responds with paginated weather data
4. **Product cache** persists to disk so data survives restarts

## Mesh Commands

Users on the Meshcore network can navigate weather data hierarchically:

### Overview & Navigation

| Command | Description |
|---------|-------------|
| `wx` | National weather overview — warnings, rain, storm reports |
| `wx TX` or just `TX` | State overview — what's happening in Texas |
| `wx Austin TX` | City-level conditions, observations, forecast |
| `help` | List available commands |
| `more` | Next page of any truncated response |

### Detailed Queries

| Command | Example | Description |
|---------|---------|-------------|
| `forecast <city ST>` | `forecast Miami FL` | Zone forecast or discussion summary |
| `warn` | `warn` | Warning count + states with active warnings |
| `warn <ST>` | `warn KS` | Detailed warnings for a state |
| `warn <city ST>` | `warn Austin TX` | Warnings near a specific location |
| `outlook <city ST>` | `outlook Des Moines IA` | 1-7 day hazardous weather outlook |
| `rain` or `rain <ST>` | `rain FL` | Areas currently reporting rain |
| `storm` or `storm <ST>` | `storm SD` | Confirmed local storm reports (LSR) |
| `metar <ICAO>` | `metar KJFK` | Raw METAR observation |
| `taf <ICAO>` | `taf KJFK` | Terminal aerodrome forecast |

### Station Codes

Both 3-letter (IATA/FAA) and 4-letter (ICAO) codes are supported:

- `wx AUS` → Austin-Bergstrom, TX
- `wx KJFK` → JFK, NY
- `wx SJU` → San Juan, PR
- `wx HNL` → Honolulu, HI

### Example Session

```
user: wx
bot:  GOES-E EMWIN Weather:
      !! 16 warnings: CA FL HI KS OK SD WI
      168 rain | 33 storm rpts
      Send <ST> or wx <city ST>

user: KS
bot:  KS Weather:
      !! 2 warning(s) - send: warn KS
      Try: forecast/outlook/wx <city KS>

user: warn KS
bot:  !! 2 warn KS:
       SEVERE THUNDERSTORM WARNING til 615 PM CDT...
       STRONG THUNDERSTORM for ELLIS COUNTY thru 500 PM CDT

user: forecast Austin TX
bot:  Austin, TX forecast (41m):
      TONIGHT...Mostly clear. Lows around 60. SE winds around 5 mph. [more]

user: more
bot:  WEDNESDAY...Sunny. Highs in the mid 80s. South winds 5 to 10 mph.
```

## Quick Start

### With Docker (recommended)

```bash
cp .env.example .env
# Edit .env with your serial port and channel

docker compose up -d
```

### Without Docker

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
# Edit .env

meshcore-weather
```

### Testing without hardware

```bash
pip install -e ".[dev]"
meshcore-weather-cli interactive
```

## Configuration

All configuration is via environment variables (or `.env` file), prefixed with `MCW_`:

| Variable | Default | Description |
|----------|---------|-------------|
| `MCW_SERIAL_PORT` | `/dev/cu.usbserial-0001` | Serial port (or `tcp://host:port`) |
| `MCW_SERIAL_BAUD` | `115200` | Serial baud rate |
| `MCW_MESHCORE_CHANNEL` | `#digitaino-wx-bot` | Channel name or index (never use 0/public) |
| `MCW_EMWIN_SOURCE` | `internet` | Data source: `internet` or `sdr` (future) |
| `MCW_EMWIN_POLL_INTERVAL` | `120` | Refresh interval in seconds |
| `MCW_EMWIN_MAX_AGE_HOURS` | `12` | Expire products older than this |
| `MCW_LOG_LEVEL` | `INFO` | Log level |

## Safety

- **Channel isolation**: The bot will never transmit on channel 0 (public) or any channel other than its configured one. This is enforced at both the message handler and radio driver layers.
- **Rate limiting**: Max 1 request per sender per 5 seconds.
- **Input sanitization**: Commands are length-limited and stripped of control characters.

## Architecture

```
meshcore_weather/
├── config.py           # Settings from env vars
├── main.py             # App entry point, command routing, pagination
├── nlp.py              # Command parser with typo tolerance
├── emwin/
│   └── fetcher.py      # EMWIN data sources (internet / SDR), disk cache
├── geodata/
│   ├── __init__.py     # Offline location resolver (zones, places, stations)
│   ├── zones.json      # 4,029 NWS forecast zones
│   ├── places.json     # 32,333 US Census places
│   └── stations.json   # 2,237 METAR stations
├── meshcore/
│   └── radio.py        # Meshcore radio interface with channel guard
└── parser/
    └── weather.py      # NWS product parsing, queries, pagination
```

## Data Sources

The bot ingests ~90 types of EMWIN products including:

- **ZFP** — Zone Forecast Products (detailed local forecasts)
- **AFD** — Area Forecast Discussions (meteorologist analysis)
- **HWO** — Hazardous Weather Outlook (1-7 day threats)
- **RWR** — Regional Weather Roundup (current conditions tables)
- **SAH** — METAR observations
- **SPS/SVS/NPW/FLS/WSW** — Warnings, watches, advisories
- **LSR** — Local Storm Reports (confirmed severe weather)
- **TAF** — Terminal Aerodrome Forecasts

## Roadmap

- [x] Internet-based EMWIN data fetching
- [x] NWS product parsing and location-based queries
- [x] Meshcore serial interface
- [x] Docker container with serial passthrough
- [x] Hierarchical navigation (national → state → city)
- [x] Pagination for 136-byte LoRa messages
- [x] Warning filtering (cancelled/expired detection via VTEC)
- [x] Persistent product cache across restarts
- [x] 3-letter airport code support
- [ ] SDR satellite downlink via goesrecv/goestools
- [ ] Position-based auto weather (use Meshcore GPS data)
- [ ] Weather alerts push (broadcast warnings to channel)

## License

MIT
