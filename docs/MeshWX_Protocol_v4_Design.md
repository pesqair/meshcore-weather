# MeshWX Protocol v4 Design

> **Status: implemented.** v4 frame header with sequence numbers, FEC/XOR parity (radar quadrants + AFD sections), discovery via ping-response on `#meshwx-discover`, new products (FWF, RTP, NOW, SFT, SPC WOU/SEL, QPF), and portal channel configuration are all implemented. The data channel is v4-only. Multi-bot roaming (auto-switching between bots based on signal/location) is not yet implemented but the discovery mechanism supports it.

---

## Why v4

v3 works but has structural limitations:

1. **Multi-chunk messages are all-or-nothing.** A 64×64 radar grid produces 5-7 chunks. Missing one = total failure. The client shows blank even though it received 85% of the data.
2. **No error correction.** Pure broadcast over lossy LoRa mesh. One-hop clients get everything; multi-hop clients get fragments.
3. **No discovery.** Clients must know the bot's channel name and pubkey in advance. No way to find nearby bots.
4. **No multi-bot.** One channel, one bot. Can't roam between deployments.
5. **No sequence numbers.** Client can't detect gaps or measure link quality.
6. **Single-message products waste frame capacity.** Observation is 15 bytes in a 136-byte frame. 89% wasted — could carry much richer data.

---

## Channel Architecture

```
#meshwx-discover           — beacon channel, all bots + all clients
#aus-meshwx-v4             — Austin deployment (bidirectional)
#dfw-meshwx-v4             — Dallas deployment
#hou-meshwx-v4             — Houston deployment
...
```

### Discovery channel (`#meshwx-discover`)

Ping-response model: the client sends a discovery ping, all bots respond with their beacon. No periodic broadcasting — zero airtime cost until a client asks.

**Client → bots: ping (0xF1)**

```
0xF1 MSG_DISCOVER_PING
  (no payload — any message on #meshwx-discover triggers all bots to respond)
```

**Bots → client: beacon response (0xF0)**

```
0xF0 MSG_BEACON (~25-35 bytes)
  byte 0     : 0xF0
  byte 1     : protocol_version (4)
  bytes 2-4  : bot_id (uint24, derived from pubkey hash — unique per deployment)
  byte 5     : beacon_flags
                 bit 0: accepting_requests
                 bit 1: has_radar
                 bit 2: has_warnings
                 bit 3: has_forecasts
                 bit 4: has_fire_weather
                 bit 5: has_nowcast
                 bit 6: has_qpf
  bytes 6-9  : coverage center (int16 lat × 100, int16 lon × 100)
  byte 10    : coverage_radius_km (uint8)
  byte 11    : active_warnings_count (uint8)
  byte 12    : channel_name_len (uint8)
  bytes 13+  : channel_name UTF-8 (e.g. "aus-meshwx-v4")
```

**Flow:**

1. Client joins `#meshwx-discover` and sends a 0xF1 ping
2. Each bot responds with its 0xF0 beacon after a random 1-5s delay (prevents air collisions)
3. Client collects responses for ~10 seconds, presents a list of nearby bots
4. User picks a bot → client joins that bot's data channel (from `channel_name`)
5. Client can leave `#meshwx-discover` to free the channel slot

The beacon flags tell the client what products each bot offers before joining. The `active_warnings_count` lets the client show "3 active warnings" next to the bot name in the picker UI.

### Deployment channel

One channel per bot deployment. Bidirectional:
- **Bot → clients**: all broadcast data (radar, warnings, observations, forecasts, etc.)
- **Clients → bot**: data requests (WXQ-prefixed, same as v3 but with v4 framing)

Why one channel (not separate TX/RX): meshcore devices have 8 channel slots max. Using 2 per bot wastes slots. The bot distinguishes its own echoes from client messages by checking the sender.

### Channel naming

Format: `#<city>-meshwx-v4`

- City prefix: 3-letter abbreviation (aus, dfw, hou, sjn, etc.) — short, unique, human-readable
- `meshwx` identifies it as a weather bot channel
- `v4` version suffix so old/new channels don't collide
- Total: 15-18 chars, well within meshcore channel name limits

---

## v4 Frame Format

Every message on a v4 deployment channel is wrapped in a 4-byte frame header:

```
byte 0     : 0x04 (protocol version — always first byte)
byte 1     : msg_type (same codes as v3: 0x10, 0x11, 0x20, etc.)
byte 2     : msg_flags
               bit 0: is_fec_unit (part of an FEC group)
               bit 1: is_parity (XOR parity unit)
               bit 2: is_base_layer (independently useful preview)
               bits 3-4: fec_group_id (0-3, links related units within a cycle)
               bits 5-7: unit_index (0-7, position within the group)
byte 3     : group_total_units (how many units in this FEC group, 0 if not FEC)
bytes 4-5  : sequence_number (uint16 BE, monotonic per bot, wraps at 65535)
bytes 6+   : message payload (same layout as v3 for that msg_type)
```

**6-byte overhead** per message. For a 15-byte observation: 21 bytes total (still tiny). For a 136-byte radar chunk: 6 bytes overhead = 4.4%.

### Sequence number (bytes 4-5)

**Included in every message.** Monotonic uint16 counter that increments by 1 for each message the bot sends on this channel. Wraps from 65535 to 0.

What it enables:
- **Gap detection**: if client sees seq 42, 43, 45 — it knows it missed seq 44
- **Link quality measurement**: "I received 847 of 900 messages this hour = 94% reception rate"
- **Duplicate detection**: same seq from same bot_id = echo/duplicate, skip it
- **Ordering**: client can reorder messages that arrive out of sequence (rare but possible over multi-hop)

Cost: 2 bytes per message. On a 15-byte observation that's 13% overhead. On a 136-byte radar chunk it's 1.5%. Worth it for the diagnostic value alone — knowing your link quality is essential for a mesh deployment.

### Auto-version detection

Byte 0 = `0x04` for v4. In v3, byte 0 is the message type (0x10-0x40 range). Since `0x04` doesn't collide with any v3 message type, a client can trivially detect which protocol a message uses:

```
if (byte[0] == 0x04) → v4 frame, parse accordingly
else                 → v3 raw message, legacy decode
```

### COBS encoding (unchanged)

The v4 frame sits INSIDE the COBS envelope. COBS is still required because the meshcore firmware's companion protocol truncates at null bytes — that's a hardware constraint, not a protocol choice. The encoding chain is:

```
message payload → v4 frame header → COBS encode → radio transmit
```

#### COBS vs length-prefixed framing (analysis)

**COBS (current)**:
- Pros: eliminates ALL null bytes (required by firmware), well-tested in v3, minimal overhead (~1 byte per 254), self-synchronizing (receiver can always find message boundaries)
- Cons: slight encoding/decoding overhead, every byte gets processed

**Length-prefixed (2-byte length header)**:
- Pros: simpler encode/decode (just read N bytes), no byte transformation
- Cons: does NOT solve the null-byte problem (payload can still contain 0x00 which firmware truncates), not self-synchronizing (if you miss the length header you lose sync), requires a separate null-byte escaping layer on top

**Decision: keep COBS.** The null-byte issue is a hard firmware constraint that length-prefixed framing doesn't solve. We'd need COBS (or SLIP or similar) anyway, so there's no benefit to adding length-prefixed on top.

If a future firmware update fixes the null-byte truncation, we could drop COBS entirely and use raw length-prefixed framing for v5. But that's not today's problem.

---

## FEC System: Spatial Quadrants + XOR Parity

### Design principles

1. **Every unit is independently useful.** No "chunk 3 of 7 is meaningless without the others." Every message the client receives adds to the display.
2. **XOR parity recovers any single missing unit** in a group. The most common failure mode (one lost packet) is fully corrected.
3. **Base layer always arrives.** A single-message preview at lower resolution that the client shows immediately while detail units stream in.
4. **No back-chatter.** Pure forward error correction. No retransmission requests, no ACKs, no per-client negotiation.
5. **Generic wrapper.** The same FEC frame fields work for radar, text, and any future multi-message product.

### How XOR parity works

All data unit payloads in a group are XOR'd together to produce a parity payload:

```
parity = unit_0_payload XOR unit_1_payload XOR unit_2_payload XOR unit_3_payload
```

If the client misses unit 2:

```
unit_2_payload = parity XOR unit_0_payload XOR unit_1_payload XOR unit_3_payload
```

**Padding requirement**: XOR is byte-by-byte, so all payloads must be the same length. Shorter payloads are zero-padded to match the longest. The parity message carries a **length map** — one uint8 per data unit giving the original (unpadded) length — so the client can truncate after recovery.

Example:
```
Unit 0 payload: 45 bytes (radar quadrant NW, 20 cells)
Unit 1 payload: 120 bytes (radar quadrant NE, 55 cells — storms)
Unit 2 payload: 10 bytes (radar quadrant SW, 3 cells)
Unit 3 payload: 7 bytes (radar quadrant SE, clear)

Parity computation:
  Pad all to 120 bytes (longest)
  parity = pad(U0, 120) XOR U1 XOR pad(U2, 120) XOR pad(U3, 120)
  parity payload: 120 bytes
  length map: [45, 120, 10, 7] = 4 bytes

Total parity message: 6 (v4 header) + 120 (parity) + 4 (length map) = 130 bytes
```

Recovery of Unit 2:
```
U2_padded = parity XOR pad(U0, 120) XOR U1 XOR pad(U3, 120)
U2_recovered = U2_padded[:10]  ← length map says U2 was 10 bytes
decode U2_recovered as sparse radar → missing quadrant restored
```

### Applied to radar (64×64)

```
Group 0, 6 units:

  Unit 0 [base_layer=1]:  0x11 radar, grid_size=32, full region sparse
                          → client displays immediately (32×32 preview)

  Unit 1 [fec_unit=1]:    0x11 radar, grid_size=32, quadrant NW sparse
  Unit 2 [fec_unit=1]:    0x11 radar, grid_size=32, quadrant NE sparse
  Unit 3 [fec_unit=1]:    0x11 radar, grid_size=32, quadrant SW sparse
  Unit 4 [fec_unit=1]:    0x11 radar, grid_size=32, quadrant SE sparse
                          → each independently renderable at 64×64 in its quadrant

  Unit 5 [parity=1]:      XOR of units 1-4 payloads + length map
                          → recovers any single missing quadrant
```

Quadrant bounds computed by client from `regions.json`:
```
mid_lat = (region.north + region.south) / 2
mid_lon = (region.west + region.east) / 2

Q0 (NW): north → mid_lat,  west → mid_lon
Q1 (NE): north → mid_lat,  mid_lon → east
Q2 (SW): mid_lat → south,  west → mid_lon
Q3 (SE): mid_lat → south,  mid_lon → east
```

Quadrant ID carried in the message (reuse the `unit_index` field):
- unit_index 1 = NW, 2 = NE, 3 = SW, 4 = SE

### Applied to text products (AFD)

```
Group 1, 4 units:

  Unit 0 [base_layer=1]:  0x40 text, subject=AFD, "[SYNOPSIS] Ridge builds..."
                          → client displays synopsis immediately

  Unit 1 [fec_unit=1]:    0x40 text, subject=AFD, "[SHORT TERM] Storms likely..."
  Unit 2 [fec_unit=1]:    0x40 text, subject=AFD, "[LONG TERM] Drier mid-week..."

  Unit 3 [parity=1]:      XOR of units 0-2 + length map
```

Each section is independently readable. Missing one? XOR-recover.

### Failure mode comparison

| Received | v3 monolithic chunks | v4 spatial + parity |
|---|---|---|
| All | ✅ full 64×64 | ✅ full 64×64 |
| Miss 1 | ❌ **BLANK** | ✅ full recovery via XOR |
| Miss 2 | ❌ **BLANK** | ⚠️ partial 64×64 + 32×32 base fills gaps |
| Miss 3+ | ❌ **BLANK** | ⚠️ some quadrants + base |
| Only base | ❌ **BLANK** | ✅ 32×32 radar (still useful) |
| Nothing | ❌ blank | ❌ blank |

### Products that don't need FEC

Single-message products just get the v4 frame header. No FEC group, no parity:

```
[v4: version=4, msg=0x30, flags=0, seq=N] + observation payload
```

These include: observation, forecast, TAF, warning polygon, warning zones, not-available, beacon.

---

## Expanded Observation (0x30) for v4

The current observation is **15 bytes** in a **136-byte frame** — 89% unused. In v4 we pack richer data into the same single message.

### Current v3 fields (10 data bytes + 5 header)

| Field | Size | What |
|---|---|---|
| location ref | 4B | zone or station |
| timestamp | 2B | minutes since midnight UTC |
| temp_f | 1B | int8 |
| dewpoint_f | 1B | int8 |
| wind_dir + sky | 1B | 4-bit compass + 4-bit sky code |
| wind_speed_mph | 1B | uint8 |
| wind_gust_mph | 1B | uint8, 0 = no gust |
| visibility_mi | 1B | uint8 |
| pressure | 1B | (inHg - 29.00) × 100 |
| feels_like_delta | 1B | int8, signed delta from temp |

### Separated cloud cover + weather type (replaces v3 sky_code nibble)

v3 jams cloud cover and precipitation into a single 4-bit nibble. You can't say "overcast with light freezing rain" — it's either `sky=overcast` or `sky=rain`. v4 separates them into two independent bytes.

**Cloud cover byte** (replaces the old sky_code's cloud values):

| Value | METAR | Meaning |
|---|---|---|
| 0x0 | SKC/CLR | Clear |
| 0x1 | FEW | Few clouds |
| 0x2 | SCT | Scattered |
| 0x3 | BKN | Broken |
| 0x4 | OVC | Overcast |
| 0x5 | VV | Obscured (vertical visibility) |

**Weather byte** (new — carries precip type + intensity + vicinity flag):

```
bits 7-3: weather_type (5 bits, 0-31)
bits 2-1: intensity (2 bits)
bit 0:    vicinity flag

weather_type values:
  0x00 = none (no significant weather)
  0x01 = rain (RA)
  0x02 = snow (SN)
  0x03 = drizzle (DZ)
  0x04 = ice pellets / sleet (PL)
  0x05 = hail (GR)
  0x06 = small hail / snow pellets (GS)
  0x07 = snow grains (SG)
  0x08 = ice crystals (IC)
  0x09 = thunderstorm (TS)
  0x0A = showers (SH — convective)
  0x0B = freezing rain (FZRA)
  0x0C = freezing drizzle (FZDZ)
  0x0D = rain+snow mix (RASN)
  0x0E = fog (FG)
  0x0F = mist (BR)
  0x10 = haze (HZ)
  0x11 = smoke (FU)
  0x12 = dust (DU)
  0x13 = sand (SA)
  0x14 = volcanic ash (VA)
  0x15 = squall (SQ)
  0x16 = funnel cloud (FC)
  0x17 = blowing snow (BLSN)
  0x18 = blowing dust (BLDU)
  0x19 = unknown precip (UP)

intensity values:
  0 = none / not applicable
  1 = light (METAR "-")
  2 = moderate (METAR no prefix)
  3 = heavy (METAR "+")

vicinity flag:
  0 = at the station
  1 = in the vicinity (METAR "VC")
```

Examples:
- "Light rain":          cloud=BKN(0x3), wx_type=RA(0x01), intensity=light(1), vc=0 → `0x03, 0x0A`
- "Heavy freezing rain": cloud=OVC(0x4), wx_type=FZRA(0x0B), intensity=heavy(3), vc=0 → `0x04, 0x5E`
- "Thunderstorm nearby": cloud=SCT(0x2), wx_type=TS(0x09), intensity=mod(2), vc=1 → `0x02, 0x4D`
- "Clear, no weather":  cloud=CLR(0x0), wx_type=none(0x00), intensity=0, vc=0 → `0x00, 0x00`

### Added v4 fields (+9 bytes → 24 bytes total, still easily single-message)

| Field | Size | What | Source |
|---|---|---|---|
| cloud_cover | 1B | uint8 (see cloud cover table above) | METAR/PFM |
| weather_byte | 1B | type(5) + intensity(2) + vicinity(1) — see above | METAR |
| humidity_pct | 1B | uint8 0-100, relative humidity | METAR/RWR |
| pressure_trend | 1B | hi nibble: direction (0=steady, 1=rising, 2=falling, 3=rapid_rise, 4=rapid_fall), lo nibble: change in 0.01 inHg over last 3h | METAR remarks |
| uv_index | 1B | uint8 0-15, EPA UV index | UVI product |
| ceiling_100ft | 1B | uint8, 0 = no ceiling, 1-250 = hundreds of feet | METAR cloud layers |
| precip_last_hour | 1B | uint8, 0.01 inch units (0-2.55"), 0xFF = trace | METAR precip |
| heat_index_f | 1B | int8, actual heat index (not delta). 127 = N/A | derived |
| wind_chill_f | 1B | int8, actual wind chill. 127 = N/A | derived |
| snow_depth_in | 1B | uint8, inches on ground. 0 = none | METAR/CLI |

Total: **24 bytes** payload + 6 bytes v4 frame = **30 bytes**. 78% of frame still unused. The old 1-byte `sky_code` nibble is replaced by `cloud_cover` + `weather_byte` (2 bytes) — one extra byte for dramatically richer weather representation.

---

## Expanded Forecast (0x31) for v4

Current: 7 bytes per period. In v4: **10 bytes per period** with wind gust, dewpoint, and humidity.

### Current v3 period (7 bytes)

| Byte | Field |
|---|---|
| 0 | period_id |
| 1 | high_f (int8, 127 = N/A) |
| 2 | low_f (int8, 127 = N/A) |
| 3 | sky_code (4-bit) |
| 4 | precip_pct (uint8) |
| 5 | wind_dir (hi nibble) \| wind_speed_5mph (lo nibble) |
| 6 | condition_flags (8-bit bitfield) |

### v4 period (12 bytes, +5)

| Byte | Field | Added |
|---|---|---|
| 0 | period_id | |
| 1 | high_f | |
| 2 | low_f | |
| 3 | **cloud_cover** (replaces sky_code — just clouds, no precip) | **changed** |
| 4 | **weather_byte** (type + intensity + vc — see observation section) | **new** |
| 5 | precip_pct | |
| 6 | wind_dir \| wind_speed_5mph | |
| 7 | condition_flags | |
| **8** | **wind_gust_5mph** (uint8, 0 = no gust) | **new** |
| **9** | **dewpoint_f** (int8) | **new** |
| **10** | **humidity_pct** (uint8 0-100) | **new** |
| **11** | **qpf_tenth_inch** (uint8, 0.1" units, quantitative precip forecast) | **new** |

7-day forecast: 7 header + 7 × 12 = **91 bytes** + 6 v4 frame = **97 bytes**. Still under 136. No FEC needed.

Each forecast period can now express "Day 3: broken clouds, moderate snow, 80% PoP, 0.8" QPF, dewpoint 28°F, NW 15 gusting 25" — all in 12 bytes.

---

## New v4 Products

v3 broadcasts observation (0x30), forecast (0x31), outlook (0x32), storm reports (0x33), rain obs (0x34), METAR (0x35), TAF (0x36), warnings near (0x37), warning polygons/zones (0x20/0x21), radar (0x10/0x11), and text chunks (0x40). All data sources are EMWIN products received via GOES satellite.

v4 adds six new products — all sourced from EMWIN:

| Product | EMWIN code | Msg type | Description |
|---|---|---|---|
| Fire Weather Forecast | FWF | 0x38 | Per-zone fire weather (RH, transport wind, mixing height, Haines) |
| State Forecast Table | SFT | 0x31 | Alternate forecast source — same wire format as ZFP/PFM |
| Regional Temp/Precip | RTP | 0x3A | Daily climate summary per city (hi/lo/precip/snow) |
| SPC Watch Products | WOU/SEL | 0x20/0x21 | Watch polygons — same wire format as existing warnings |
| QPF Precip Grid | QPF | 0x12 | Gridded precipitation forecast — same encoding as radar |
| Short Term Forecast | NOW | 0x3C | 1-3 hour nowcast, compact binary + optional text overflow |

### Fire Weather Forecast (0x38) — FWF

Fire weather is critical for the off-grid audience. The FWF product carries per-zone, per-period forecasts with fields not found in standard ZFP/PFM products.

EMWIN source: `FWF<wfo><st>` (e.g., `FWFEWXTX`). Issued 1-2x daily by WFOs with fire weather zones.

```
0x38 MSG_FIRE_WEATHER

  byte 0       : 0x38
  bytes 1-N    : location (zone reference)
  byte N+1     : issued_hours_ago (uint8)
  byte N+2     : period_count (uint8, 1-7)

  Per period (8 bytes):
    byte 0     : period_id (same scheme as 0x31: 0=tonight, 1=today, 2=tomorrow...)
    byte 1     : max_temp_f (int8)
    byte 2     : min_rh_pct (uint8, 0-100)
    byte 3     : transport_wind (hi nibble: 16-pt dir, lo nibble: speed in 5mph units)
    byte 4     : mixing_height_500ft (uint8, ×500ft → 0-127,500 ft range)
    byte 5     : haines_index (lo nibble: 2-6) | lightning_risk (hi nibble: 0=none, 1=dry, 2=wet)
    byte 6     : cloud_cover (v4 cloud codes, see observation section)
    byte 7     : weather_byte (v4 type+intensity+vc, see observation section)
```

**Size**: 3-day FWF for one zone: 5 loc + 2 header + 3×8 = **31 bytes** + 6 v4 frame = **37 bytes**. Single message, no FEC needed.

**Parser extraction targets** from FWF text:
- `.TODAY...` / `.TONIGHT...` period headers (same as ZFP)
- `MIN HUMIDITY...15 TO 25 PERCENT`
- `TRANSPORT WINDS...WEST 15 TO 25 MPH`
- `MIXING HEIGHT...8000 TO 10000 FT AGL`
- `HAINES INDEX...5 TO 6`
- `CHANCE OF WETTING RAIN...10 PERCENT` (ignore — already in precip %)
- `SKY/WEATHER...MOSTLY SUNNY` (standard sky classification)

### Regional Temp/Precip (0x3A) — RTP

Daily climate observation summary. Fills the gap between real-time METAR/RWR (current conditions) and forecasts. Gives yesterday's actual high, low, and precipitation — useful for tracking drought/flood conditions over time.

EMWIN source: `RTP<wfo><st>` (e.g., `RTPEWXTX`). Issued daily, typically mid-morning.

```
0x3A MSG_DAILY_CLIMATE

  byte 0       : 0x3A
  byte 1       : city_count (uint8, 1-18)
  byte 2       : report_day_offset (uint8, 0=today so far, 1=yesterday, 2=day before)

  Per city (7 bytes):
    bytes 0-2  : location ref (uint24 place_id from resolver)
    byte 3     : max_temp_f (int8, 127 = missing)
    byte 4     : min_temp_f (int8, 127 = missing)
    byte 5     : precip_hundredths (uint8, 0.01" units, 0-2.54", 0xFF = trace, 0xFE = missing)
    byte 6     : snow_tenths (uint8, 0.1" units, 0-25.5", 0xFF = trace, 0xFE = missing)
```

**Size**: 15 cities: 1 + 2 header + 15×7 = **108 bytes** + 6 v4 frame = **114 bytes**. Single message.

**Batch layout**: RTP is naturally a multi-city table, so batching cities into one message is more airtime-efficient than one message per city. 18 cities is the max that fits in a single 136-byte frame (3 + 18×7 = 129 < 130 payload bytes).

**Parser extraction targets** from RTP text:
```
CITY              MAX  MIN  PCPN  SNOW
AUSTIN            95   72   0.00
SAN ANTONIO       93   71   T
DEL RIO           97   74   0.15   0.0
```

### QPF Precipitation Grid (0x12) — QPF

Quantitative precipitation forecast rendered on the same grid as radar. Reuses the entire sparse/RLE compression pipeline from 0x11 — the only difference is what the 4-bit cell values represent.

EMWIN source: `QPF<wfo><st>` discussion text provides area amounts. For gridded data, the bot samples from the NWS QPF GRIB2 files available via EMWIN image products, or falls back to painting zones from the QPF text discussion.

```
0x12 MSG_QPF_GRID

  Same wire format as 0x11 (MSG_RADAR_COMPRESSED):
    byte 0     : 0x12
    byte 1     : grid_size (32 or 64)
    byte 2     : encoding (0=sparse, 1=RLE)
    byte 3     : region_id (same regions.json mapping as radar)
    byte 4     : valid_period (hi nibble: start offset in 6h units from 00Z,
                               lo nibble: duration in 6h units)
    byte 5     : chunk_seq (hi nibble) | total_chunks (lo nibble)
    bytes 6+   : compressed grid data (sparse or RLE, identical encoding)

  4-bit QPF levels (replaces reflectivity):
    0x0 = none
    0x1 = trace - 0.10"
    0x2 = 0.10 - 0.25"
    0x3 = 0.25 - 0.50"
    0x4 = 0.50 - 0.75"
    0x5 = 0.75 - 1.00"
    0x6 = 1.00 - 1.50"
    0x7 = 1.50 - 2.00"
    0x8 = 2.00 - 2.50"
    0x9 = 2.50 - 3.00"
    0xA = 3.00 - 4.00"
    0xB = 4.00 - 5.00"
    0xC = 5.00 - 7.00"
    0xD = 7.00 - 10.00"
    0xE = 10.00"+
```

**Advantages of reusing the radar pipeline**: no new compression code, clients already know how to render 4-bit grids, FEC spatial quadrants work identically for 64×64 QPF.

**`valid_period` byte**: differentiates "6h QPF starting at 12Z" from "24h QPF starting at 00Z". High nibble = start offset in 6h blocks from 00Z (0=00Z, 1=06Z, 2=12Z, 3=18Z). Low nibble = duration in 6h blocks (1=6h, 2=12h, 4=24h). Most common: Day 1 24h QPF = `0x04`.

### Short Term Forecast (0x3C) — NOW

The most tactically actionable product for off-grid users: what's happening in the next 1-3 hours. NOWcasts are short, urgent, and time-sensitive.

EMWIN source: `NOW<wfo><st>` (e.g., `NOWEWXTX`). Issued as needed by WFOs, typically during active weather.

```
0x3C MSG_NOWCAST

  byte 0       : 0x3C
  bytes 1-N    : location (zone or WFO reference)
  byte N+1     : valid_hours (uint8, how many hours this covers, typically 1-3)
  byte N+2     : urgency_flags
                   bit 0: has_thunder
                   bit 1: has_flooding
                   bit 2: has_winter
                   bit 3: has_fire
                   bit 4: has_wind
                   bits 5-7: reserved
  bytes N+3+   : text payload (UTF-8, truncated to fit frame)
```

**Size**: With a WFO location (4 bytes), 3 bytes of header fields, and 6 bytes v4 frame overhead: **123 bytes** available for text in a single message. A typical NOW summary fits in 1-2 messages.

**For longer NOWcasts**: if the text exceeds single-frame capacity, overflow uses the existing text chunk system (0x40 with `TEXT_SUBJECT_NOWCAST`). The 0x3C message carries the structured metadata + lead paragraph; additional text chunks carry the rest. This means the critical info arrives first and is independently useful.

**Parser extraction targets** from NOW text:
```
SHORT TERM FORECAST
NATIONAL WEATHER SERVICE AUSTIN/SAN ANTONIO TX
330 PM CDT SAT APR 12 2025

...STRONG THUNDERSTORMS MOVING EAST ACROSS THE HILL COUNTRY...

SCATTERED STRONG THUNDERSTORMS ARE MOVING EAST AT 30 MPH ACROSS
THE HILL COUNTRY. THESE STORMS MAY PRODUCE BRIEF HEAVY RAIN AND
WIND GUSTS TO 50 MPH THROUGH 5 PM.
```

Urgency flags are set by keyword scan of the text body (same approach as existing condition_flags in forecast encoder).

### State Forecast Table — SFT (data source for 0x31)

SFT is not a new message type — it's an alternate EMWIN data source that produces standard 0x31 forecast messages using the existing wire format.

EMWIN source: `SFT<wfo><st>` (e.g., `SFTUS4KWBC`). Issued by NCEP, covers major cities nationwide.

```
SFT format (tabular):

CITY              WED      WED NIGHT THU       THU NIGHT FRI
                  HI  WX   LO  WX    HI  WX   LO  WX    HI  WX
AUSTIN            95  SU   72  CL    93  SU    71  CL    90  MC
SAN ANTONIO       93  SU   71  CL    91  SU    70  CL    88  MC
```

The parser extracts per-city per-period `(high_f, low_f, sky_code, precip_pct)` and feeds them into the existing `pack_forecast()` encoder. SFT provides a nationwide fallback when the local WFO's ZFP or PFM is unavailable or stale.

**Parser priority** (from best to worst data): PFM → ZFP → SFT. The executor tries each in order and uses the first that returns data.

### SPC Watch Products — WOU/SEL (data source for 0x20/0x21)

Watch products are not a new message type — they feed into the existing warning polygon (0x20) and warning zone (0x21) wire formats.

EMWIN sources:
- **SEL** (Watch definition): polygon coordinates + watch type + expiration
- **WOU** (Watch Outline Update): zone list updates for existing watches

These map to existing encoding:
- `warning_type`: `WARN_TORNADO` (0x1) for tornado watches, `WARN_SEVERE_TSTORM` (0x2) for severe thunderstorm watches
- `severity`: `SEV_WATCH` (0x2) — distinguishes watches from warnings

```
SEL example:
   URGENT - IMMEDIATE BROADCAST REQUESTED
   TORNADO WATCH NUMBER 215
   NWS STORM PREDICTION CENTER NORMAN OK
   ...
   LAT...LON 35289680 35459614 33849495 33029517 33029605
```

The polygon coordinates use the existing vertex encoding (first vertex int24×10000, deltas int16×0.001°). The parser extracts:
1. Watch type (tornado vs severe thunderstorm) from header
2. Watch number (uint16, for tracking/cancellation)
3. Polygon vertices from `LAT...LON` line
4. Expiration time
5. Affected zones from the WOU product

**No new wire format** — just new EMWIN product parsers that feed into `pack_warning_polygon()` and `pack_warning_zones()`.

### Updated beacon flags

The beacon (0xF0) `beacon_flags` byte gains three new capability bits:

```
byte 5 : beacon_flags
           bit 0: accepting_requests
           bit 1: has_radar
           bit 2: has_warnings
           bit 3: has_forecasts
           bit 4: has_fire_weather    ← was: has_space_weather (renamed, EMWIN-sourced)
           bit 5: has_nowcast         ← new
           bit 6: has_qpf             ← new
           bit 7: reserved
```

`has_space_weather` is renamed to `has_fire_weather` since space weather data is not available through EMWIN.

### Updated single-message products list

These v4 products fit in a single 136-byte frame with no FEC:

```
Observation (0x30)         — 30 bytes
Forecast 7-day (0x31)      — 97 bytes (includes SFT-sourced data)
Fire Weather Forecast (0x38) — 37 bytes (3-day, one zone)
Daily Climate (0x3A)       — 114 bytes (15 cities)
Nowcast (0x3C)             — up to 136 bytes (structured header + text)
TAF (0x36)                 — unchanged
Warning polygon (0x20)     — unchanged (now also carries SPC watch polygons)
Warning zones (0x21)       — unchanged (now also carries WOU zone lists)
Not-available (0x03)       — unchanged
Beacon (0xF0)              — unchanged
```

QPF grid (0x12) follows the same chunking/FEC rules as radar (0x11).

---

## FEC does NOT span across product types

> Design question #2 from the discussion: "Should FEC groups span multiple products?"

**No.** Keep FEC groups within a single product type. Reasons:

- Spanning products (e.g., one parity covering radar + forecast + observation) ties unrelated data together. A client that only cares about radar would need to receive the forecast and observation too, just to have enough units for XOR recovery.
- Different products have different update cadences (radar every 15 min, forecast every 60 min). Cross-product FEC would force them onto the same schedule.
- The failure modes are independent — losing a radar chunk doesn't correlate with losing a forecast message. Per-product FEC already covers the common case (1 lost message per product per cycle).
- Implementation complexity explodes when products have different sizes, encodings, and semantics.

---

## Migration Path

### Phase 1: Current system (no changes)

- v3 on `#wx-broadcast`
- All current clients continue working
- Bot operates as today

### Phase 2: Implement v4 alongside v3

- Bot joins `#meshwx-discover` AND `#aus-meshwx-v4`
- Bot broadcasts on BOTH channels: v3 on `#wx-broadcast`, v4 on `#aus-meshwx-v4`
- New/updated clients listen on v4, get FEC + sequence numbers + richer fields
- Old clients stay on v3, still work
- Airtime roughly doubles during transition (dual broadcast)

### Phase 3: Deprecate v3

- Stop broadcasting on `#wx-broadcast`
- Old clients lose service (they need to update)
- Airtime returns to normal
- Bot can leave `#wx-broadcast`, freeing a channel slot

### Phase 4: Multi-bot

- Multiple operators deploy bots in different cities
- Each gets its own `#<city>-meshwx-v4` channel
- Clients roam via `#meshwx-discover` beacons
- A client traveling from Austin to Houston hears the Houston beacon and auto-joins `#hou-meshwx-v4`

---

## Summary of v4 vs v3

| Aspect | v3 (current) | v4 (proposed) |
|---|---|---|
| Frame header | none (raw message after COBS) | 6 bytes (version + type + flags + seq) |
| Sequence numbers | ❌ none | ✅ uint16 monotonic per bot |
| Multi-chunk radar | all-or-nothing monolithic | spatial quadrants + XOR parity |
| Multi-chunk text | all-or-nothing sequential | per-section + XOR parity |
| Miss 1 of N chunks | ❌ total failure | ✅ XOR recovery |
| Observation fields | 10 (temp, wind, sky, pressure, vis) | 18 (adds humidity, UV, ceiling, precip, trend, heat/wind chill, snow) |
| Forecast per-period | 7 bytes (no gust/dewpoint/humidity) | 10 bytes (adds gust, dewpoint, humidity) |
| Fire weather | ❌ not available | ✅ FWF per-zone (0x38): RH, transport wind, mixing height, Haines |
| Daily climate obs | ❌ not available | ✅ RTP batched cities (0x3A): hi/lo/precip/snow |
| QPF grid | ❌ not available | ✅ same grid pipeline as radar (0x12), 4-bit precip levels |
| Nowcast | ❌ not available | ✅ NOW structured + text (0x3C): 1-3 hour tactical forecast |
| Forecast sources | ZFP, PFM | ZFP, PFM, SFT (nationwide fallback) |
| Watch products | warnings only | warnings + SPC WOU/SEL watch polygons |
| Discovery | ❌ manual channel/pubkey config | ✅ beacon on `#meshwx-discover` |
| Multi-bot | ❌ one channel, one bot | ✅ per-city deployment channels |
| COBS encoding | ✅ required | ✅ still required (firmware constraint) |
| Backward compat | — | auto-detect by byte 0 (0x04 vs 0x10-0x40) |

---

## Implementation order (when we build this)

1. **v4 frame format + sequence numbers** — add the 6-byte header to all messages on a new channel. Sequence numbers alone give immediate diagnostic value.
2. **Expanded observation + forecast fields** — pack richer data into the same single messages. No FEC needed.
3. **New EMWIN parsers + encoders** — FWF, RTP, NOW, SFT, SPC (WOU/SEL), QPF. Each is independent:
   - a. **FWF parser + 0x38 encoder** — new message type, new parser
   - b. **RTP parser + 0x3A encoder** — new message type, new parser
   - c. **NOW parser + 0x3C encoder** — new message type, new parser
   - d. **SFT parser** — new parser, feeds existing 0x31 encoder
   - e. **SPC WOU/SEL parser** — new parser, feeds existing 0x20/0x21 encoders
   - f. **QPF grid (0x12)** — new message type, reuses radar compression pipeline
4. **Spatial quadrant radar with XOR parity** — the big reliability win for 64×64.
5. **FEC for text products** — per-section AFD with parity.
6. **Discovery beacon** — `#meshwx-discover` with 2-hour beacon interval.
7. **Multi-bot support** — per-city channels, client roaming.
