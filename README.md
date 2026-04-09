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
3. **Meshcore radio** receives user requests via channel or DM and responds with paginated weather data
4. **Product cache** persists to disk so data survives restarts

## Direct Messages

The bot supports a hybrid channel/DM system to reduce channel spam:

1. **New users** send commands on the channel and get a few free replies, along with a prompt to send an advert
2. **When a user adverts**, the bot detects it immediately, re-adverts itself (so both sides discover each other), and sends a DM welcome
3. **Once connected**, all responses go via DM automatically — even if the user sends commands on the channel
4. **If DMs break** (e.g. user deletes the bot from contacts), the bot detects the failure after 2 attempts and falls back to channel with a nudge to re-advert

The bot advertises itself on startup and every 15 minutes, and also immediately when an unknown user sends a channel message.

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

## Admin Commands

Admin commands are available via DM only, authenticated by pubkey prefix (`MCW_ADMIN_KEY`):

| Command | Description |
|---------|-------------|
| `admin` | Show admin help |
| `contacts` | List all contacts on the radio device |
| `remove <name>` | Remove a specific contact by name |
| `clear-contacts` | Remove ALL contacts from the device |
| `advert` | Send a flood advert + refresh contacts |
| `refresh` | Reload contacts from the device |

## Quick Start

### With Docker (recommended)

```bash
cp .env.example .env
# Edit .env with your serial port, channel, and admin key

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

### CLI Tools

```bash
meshcore-weather-cli fetch              # Fetch EMWIN products from NOAA
meshcore-weather-cli query "Austin TX"  # Query weather data
meshcore-weather-cli interactive        # Simulate mesh commands
meshcore-weather-cli contacts           # List radio contacts
meshcore-weather-cli remove <name>      # Remove a contact
meshcore-weather-cli clear-contacts     # Remove all contacts
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
| `MCW_ADMIN_KEY` | *(empty)* | Pubkey prefix of admin user (enables admin DM commands) |
| `MCW_LOG_LEVEL` | `INFO` | Log level |

## Safety

- **Channel isolation**: The bot will never transmit on channel 0 (public) or any channel other than its configured one. This is enforced at both the message handler and radio driver layers.
- **DM fallback**: If DMs fail, the bot detects it and falls back to channel responses gracefully.
- **Channel spam limits**: Unknown contacts get 3 free channel replies, then must advert for DM access.
- **Rate limiting**: Max 1 request per sender per 5 seconds.
- **Input sanitization**: Commands are length-limited and stripped of control characters.
- **Admin authentication**: Admin commands require matching pubkey prefix — cannot be spoofed via channel.

## Architecture

```
meshcore_weather/
├── config.py           # Settings from env vars
├── main.py             # App entry point, command routing, DM/channel hybrid
├── nlp.py              # Command parser with typo tolerance
├── cli.py              # CLI tools for testing and radio admin
├── emwin/
│   └── fetcher.py      # EMWIN data sources (internet / SDR), disk cache
├── geodata/
│   ├── __init__.py     # Offline location resolver (zones, places, stations)
│   ├── zones.json      # 4,029 NWS forecast zones
│   ├── places.json     # 32,333 US Census places
│   └── stations.json   # 2,237 METAR stations
├── meshcore/
│   └── radio.py        # Radio interface: channel, DM, adverts, contacts
└── parser/
    └── weather.py      # NWS product parsing, queries, pagination
```

## Data Sources

The bot ingests ~90 types of EMWIN products including:

- **ZFP** — Zone Forecast Products (detailed local forecasts)
- **AFD** — Area Forecast Discussions (meteorologist analysis, key messages)
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
- [x] Hybrid DM/channel messaging with auto-discovery
- [x] Admin commands via authenticated DM
- [x] Channel spam limiting with advert nudges
- [ ] SDR satellite downlink via goesrecv/goestools
- [ ] Position-based auto weather (use Meshcore GPS data)
- [ ] Weather alerts push (broadcast warnings to channel)

## License

MIT
