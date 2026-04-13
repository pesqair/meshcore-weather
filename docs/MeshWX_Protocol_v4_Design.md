# MeshWX Protocol v4 Design

> **Status: design document, not yet implemented.** This captures the architecture for the next-generation protocol. The current v3 system continues operating on existing channels. v4 will be implemented on new channels alongside v3 with no disruption to existing clients.

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

Every bot announces itself every **2 hours** with a small beacon. Clients join this one channel to find nearby bots.

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
                 bit 4: has_space_weather
  bytes 6-9  : coverage center (int16 lat × 100, int16 lon × 100)
  byte 10    : coverage_radius_km (uint8)
  byte 11    : active_warnings_count (uint8)
  byte 12    : channel_name_len (uint8)
  bytes 13+  : channel_name UTF-8 (e.g. "aus-meshwx-v4")
```

Beacon interval: **every 2 hours.** The discovery channel should be quiet — clients listen passively and may need to wait up to 2 hours to hear all nearby bots. This is acceptable because discovery is a one-time setup step, not a real-time operation.

No capabilities bitmap beyond the beacon_flags. Keep it simple — the flags tell the client "this bot has radar" vs "this bot is text-only." The client can query for detailed capabilities after joining the deployment channel.

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

### Added v4 fields (+8 bytes → 23 bytes total, still easily single-message)

| Field | Size | What | Source |
|---|---|---|---|
| humidity_pct | 1B | uint8 0-100, relative humidity | METAR/RWR |
| pressure_trend | 1B | hi nibble: direction (0=steady, 1=rising, 2=falling, 3=rapid_rise, 4=rapid_fall), lo nibble: change in 0.01 inHg over last 3h | METAR remarks |
| uv_index | 1B | uint8 0-15, EPA UV index | UVI product |
| ceiling_100ft | 1B | uint8, 0 = no ceiling, 1-250 = hundreds of feet | METAR cloud layers |
| precip_last_hour | 1B | uint8, 0.01 inch units (0-2.55"), 0xFF = trace | METAR precip |
| heat_index_f | 1B | int8, actual heat index (not delta). 127 = N/A | derived |
| wind_chill_f | 1B | int8, actual wind chill. 127 = N/A | derived |
| snow_depth_in | 1B | uint8, inches on ground. 0 = none | METAR/CLI |

Total: **23 bytes** payload + 6 bytes v4 frame = **29 bytes**. 79% of frame still unused but we've captured every field a weather app typically shows.

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

### v4 period (10 bytes, +3)

| Byte | Field | Added |
|---|---|---|
| 0 | period_id | |
| 1 | high_f | |
| 2 | low_f | |
| 3 | sky_code | |
| 4 | precip_pct | |
| 5 | wind_dir \| wind_speed_5mph | |
| 6 | condition_flags | |
| **7** | **wind_gust_5mph** (uint8, 0 = no gust) | **new** |
| **8** | **dewpoint_f** (int8) | **new** |
| **9** | **humidity_pct** (uint8 0-100) | **new** |

7-day forecast: 7 header + 7 × 10 = **77 bytes** + 6 v4 frame = **83 bytes**. Still well under 136. No FEC needed.

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
| Discovery | ❌ manual channel/pubkey config | ✅ beacon on `#meshwx-discover` |
| Multi-bot | ❌ one channel, one bot | ✅ per-city deployment channels |
| COBS encoding | ✅ required | ✅ still required (firmware constraint) |
| Backward compat | — | auto-detect by byte 0 (0x04 vs 0x10-0x40) |

---

## Implementation order (when we build this)

1. **v4 frame format + sequence numbers** — add the 6-byte header to all messages on a new channel. Sequence numbers alone give immediate diagnostic value.
2. **Expanded observation + forecast fields** — pack richer data into the same single messages. No FEC needed.
3. **Spatial quadrant radar with XOR parity** — the big reliability win for 64×64.
4. **FEC for text products** — per-section AFD with parity.
5. **Discovery beacon** — `#meshwx-discover` with 2-hour beacon interval.
6. **Multi-bot support** — per-city channels, client roaming.
