# MeshWX v4 Client Implementation Guide

Wire-level spec for iOS (or any) client consuming v4 messages. All values are big-endian unless noted. All messages arrive COBS-decoded.

---

## 1. Auto-detecting v4 vs v3

```
byte[0] == 0x04  →  v4 frame, parse as below
byte[0] >= 0x10  →  v3 raw message, legacy decode (unchanged)
```

v4 and v3 messages can arrive on different channels or even the same channel. Always check byte 0 first.

---

## 2. v4 Frame Header (6 bytes)

Every v4 message starts with this header:

```
byte 0     : 0x04             — protocol version (always 0x04)
byte 1     : msg_type         — same codes as v3 (0x10, 0x11, 0x20, 0x30, etc.)
byte 2     : msg_flags        — FEC bits (see below), 0x00 for non-FEC messages
byte 3     : group_total      — number of units in this FEC group, 0 if not FEC
bytes 4-5  : sequence_number  — uint16 BE, monotonic per bot, wraps at 65535
bytes 6+   : payload          — same as v3 payload WITHOUT the type byte
```

To get the equivalent v3 message: prepend `msg_type` to the payload:
```
v3_message = [msg_type] + payload
```

Then decode using existing v3 parsers.

### Sequence numbers

- Monotonic: increments by 1 for each message the bot sends
- Gap detection: if you see seq 42, 43, 45 → you missed seq 44
- Link quality: `received / expected` over a time window
- Wraps from 65535 → 0 (handle wraparound in gap detection)

---

## 3. FEC (Forward Error Correction)

### When FEC applies

`msg_flags != 0x00` means this message is part of an FEC group. FEC is used for multi-message products:
- **Radar 64x64**: base layer + 4 spatial quadrants + XOR parity (6 messages)
- **AFD text**: synopsis + section units + XOR parity

Single-message products (observations, forecasts, warnings, etc.) have `msg_flags = 0x00` and `group_total = 0`. No FEC handling needed.

### msg_flags bit layout

```
bit 0     : is_fec_unit   — this message is part of an FEC group
bit 1     : is_parity     — this is the XOR parity unit
bit 2     : is_base_layer — independently useful preview (display immediately)
bits 3-4  : group_id      — 0-3, links related units within a broadcast cycle
bits 5-7  : unit_index    — 0-7, position within the group
```

Parse in Swift:
```swift
let isUnit    = (flags & 0x01) != 0
let isParity  = (flags & 0x02) != 0
let isBase    = (flags & 0x04) != 0
let groupId   = (flags >> 3) & 0x03
let unitIndex = (flags >> 5) & 0x07
```

### FEC group structure (radar example)

The bot sends 6 messages for a 64x64 radar region:

```
msg 0: is_base=1, unit_index=0  → 32x32 downsampled radar (display immediately)
msg 1: is_unit=1, unit_index=1  → NW quadrant (32x32)
msg 2: is_unit=1, unit_index=2  → NE quadrant (32x32)
msg 3: is_unit=1, unit_index=3  → SW quadrant (32x32)
msg 4: is_unit=1, unit_index=4  → SE quadrant (32x32)
msg 5: is_parity=1, unit_index=5 → XOR parity of units 1-4
```

### Client FEC algorithm

```
1. Collect incoming messages that share (msg_type, group_id).
   Use group_total to know how many to expect.

2. If is_base: display it immediately as a low-res preview.

3. As data units arrive: render each quadrant into the 64x64 display.
   Each quadrant is independently decodable.

4. If you received all data units: done. Ignore parity.

5. If you're missing exactly 1 data unit AND you have the parity:
   → XOR-recover the missing unit (see below).

6. If you're missing 2+ data units: display what you have.
   The base layer fills gaps at lower resolution.
```

### XOR recovery

The parity unit's payload contains:

```
byte 0         : unit_count (number of data units)
bytes 1..N     : uint16 BE per unit — original payload length (before padding)
bytes N+1..end : XOR parity data
```

All data unit payloads were zero-padded to the length of the longest, then XOR'd together. To recover a missing unit:

```swift
// 1. Parse parity payload
let unitCount = parityPayload[0]
var lengthMap: [Int] = []
var offset = 1
for _ in 0..<unitCount {
    let len = UInt16(parityPayload[offset]) << 8 | UInt16(parityPayload[offset+1])
    lengthMap.append(Int(len))
    offset += 2
}
let xorData = parityPayload[offset...]

// 2. XOR parity with all RECEIVED data unit payloads
var recovered = Array(xorData)  // start with parity
for (_, payload) in receivedUnits {
    for i in 0..<min(payload.count, recovered.count) {
        recovered[i] ^= payload[i]
    }
}

// 3. Truncate to original length
let originalLength = lengthMap[missingIndex]
recovered = Array(recovered[0..<originalLength])
```

**Important**: the payloads used for XOR are the v3 payload bytes AFTER the msg_type byte (i.e., `v4_frame[6:]`, not `v4_frame[1] + v4_frame[6:]`). The msg_type byte is in the header, not in the parity computation.

---

## 4. New Message Types

### 0x38 Fire Weather Forecast (FWF)

```
byte 0       : 0x38
bytes 1..N   : location (type-tagged, see v3 location encoding)
byte N+1     : issued_hours_ago (uint8)
byte N+2     : period_count (uint8, 1-7)

Per period (8 bytes):
  byte 0     : period_id (0=tonight, 1=today, 2=tomorrow, etc.)
  byte 1     : max_temp_f (int8)
  byte 2     : min_rh_pct (uint8, 0-100)
  byte 3     : transport_wind
                 hi nibble: 16-point compass direction (0=N, 2=NE, 4=E, ...)
                 lo nibble: speed in 5mph units (0-15 → 0-75 mph)
  byte 4     : mixing_height_500ft (uint8, multiply by 500 for feet AGL)
  byte 5     : haines_lightning
                 hi nibble: lightning_risk (0=none, 1=dry, 2=wet)
                 lo nibble: haines_index (2-6)
  byte 6     : cloud_cover (0=CLR, 1=FEW, 2=SCT, 3=BKN, 4=OVC, 5=VV)
  byte 7     : weather_byte (same encoding as v4 observation weather byte)
```

Typical size: 37 bytes for 3-day forecast.

### 0x3A Daily Climate Summary (RTP)

```
byte 0       : 0x3A
byte 1       : city_count (uint8, 1-18)
byte 2       : report_day_offset (uint8, 0=today, 1=yesterday)

Per city (7 bytes):
  bytes 0-2  : place_id (uint24 BE, index into places database)
  byte 3     : max_temp_f (int8, 127 = missing)
  byte 4     : min_temp_f (int8, 127 = missing)
  byte 5     : precip_hundredths (uint8, 0.01" units, 0xFF=trace, 0xFE=missing)
  byte 6     : snow_tenths (uint8, 0.1" units, 0xFF=trace, 0xFE=missing)
```

Typical size: 114 bytes for 15 cities.

### 0x3C Short-Term Forecast / Nowcast (NOW)

```
byte 0       : 0x3C
bytes 1..N   : location (type-tagged, typically LOC_WFO = 0x05 + 3 ASCII)
byte N+1     : valid_hours (uint8, how many hours this covers, typically 1-3)
byte N+2     : urgency_flags
                 bit 0: has_thunder
                 bit 1: has_flooding
                 bit 2: has_winter
                 bit 3: has_fire
                 bit 4: has_wind
bytes N+3..  : text payload (UTF-8)
```

If the text exceeds single-frame capacity, overflow arrives as 0x40 text chunks with `subject_type = 0x08` (TEXT_SUBJECT_NOWCAST).

### 0x12 QPF Precipitation Grid

Same wire format as 0x11 (compressed radar), but:
- byte 0 is `0x12` instead of `0x11`
- 4-bit cell values represent precipitation amounts, not reflectivity:

```
0x0 = none         0x1 = trace-0.10"   0x2 = 0.10-0.25"
0x3 = 0.25-0.50"   0x4 = 0.50-0.75"    0x5 = 0.75-1.00"
0x6 = 1.00-1.50"   0x7 = 1.50-2.00"    0x8 = 2.00-2.50"
0x9 = 2.50-3.00"   0xA = 3.00-4.00"    0xB = 4.00-5.00"
0xC = 5.00-7.00"   0xD = 7.00-10.00"   0xE = 10.00"+
```

Uses the same sparse/RLE encoding, same chunking, same FEC quadrants as radar. Render with a blue/green color ramp instead of radar reflectivity colors.

---

## 5. Existing v3 types (unchanged on v4 channel)

These arrive with the v4 header but their payload is identical to v3. Unwrap the header, prepend msg_type, and use existing decoders:

| Type | Code | Notes |
|------|------|-------|
| Radar compressed | 0x11 | 32x32 non-FEC or individual FEC unit |
| Warning polygon | 0x20 | Now also carries SPC watch polygons |
| Warning zones | 0x21 | Now also carries SPC WOU zone lists |
| Observation | 0x30 | Unchanged |
| Forecast | 0x31 | May be sourced from SFT (nationwide) in addition to PFM/ZFP |
| Outlook | 0x32 | Unchanged |
| Storm reports | 0x33 | Unchanged |
| Rain obs | 0x34 | Unchanged |
| METAR | 0x35 | Unchanged |
| TAF | 0x36 | Unchanged |
| Warnings near | 0x37 | Unchanged |
| Text chunk | 0x40 | New subject_type 0x08 = nowcast overflow |

---

## 6. Discovery (finding bots)

The client finds nearby bots by pinging `#meshwx-discover`. All bots respond with their beacon.

### Step 1: Client sends a ping

Join `#meshwx-discover` and send any message (a single 0xF1 byte, or even just text — any message triggers all bots to respond).

### Step 2: Collect beacon responses (~10 seconds)

Each bot responds with a 0xF0 beacon after a random 1-5s delay:

```
byte 0     : 0xF0 (MSG_BEACON)
byte 1     : 0x04 (protocol version)
bytes 2-4  : bot_id (uint24 BE — unique per deployment)
byte 5     : beacon_flags
               bit 0: accepting_requests
               bit 1: has_radar
               bit 2: has_warnings
               bit 3: has_forecasts
               bit 4: has_fire_weather
               bit 5: has_nowcast
               bit 6: has_qpf
bytes 6-7  : lat (int16 BE, degrees × 100)
bytes 8-9  : lon (int16 BE, degrees × 100)
byte 10    : coverage_radius_km (uint8)
byte 11    : active_warnings_count (uint8)
byte 12    : channel_name_len (uint8)
bytes 13+  : channel_name (UTF-8, e.g. "aus-meshwx-v4")
```

Parse beacon_flags in Swift:
```swift
let accepting  = (flags & 0x01) != 0
let hasRadar   = (flags & 0x02) != 0
let hasWarnings = (flags & 0x04) != 0
let hasForecasts = (flags & 0x08) != 0
let hasFire    = (flags & 0x10) != 0
let hasNowcast = (flags & 0x20) != 0
let hasQPF     = (flags & 0x40) != 0
```

### Step 3: Present bot picker

Show the user a list:
```
Austin WX Bot — 3 warnings — radar, forecasts, fire weather
  [Join #aus-meshwx-v4]

Dallas WX Bot — 0 warnings — radar, forecasts
  [Join #dfw-meshwx-v4]
```

### Step 4: Join the data channel

User picks a bot → client joins `#<channel_name>` from the beacon → leave `#meshwx-discover` to free the slot.

---

## 7. Location encoding reference

All location refs are type-tagged (first byte = type):

```
0x01 LOC_ZONE     : 1 + 1 (state_idx) + 2 (zone_num BE) = 4 bytes
0x02 LOC_STATION  : 1 + 4 (ICAO ASCII) = 5 bytes
0x03 LOC_PLACE    : 1 + 3 (uint24 place_id BE) = 4 bytes
0x04 LOC_LATLON   : 1 + 3 (lat int24 * 10000) + 3 (lon int24 * 10000) = 7 bytes
0x05 LOC_WFO      : 1 + 3 (WFO ASCII) = 4 bytes
0x06 LOC_PFM_POINT: 1 + 3 (uint24 index BE) = 4 bytes
```

---

## 8. Recommended client implementation order

1. **Discovery** — join `#meshwx-discover`, send ping, collect beacons, join a bot's data channel.
2. **v4 header parsing + auto-detect** — unwrap to v3 and use existing decoders. This gives you sequence numbers and all existing products immediately.
3. **Sequence number tracking** — gap detection, link quality display.
4. **New message types** (0x38, 0x3A, 0x3C, 0x12) — add decoders and UI.
5. **FEC collection** — buffer FEC group units by (msg_type, group_id). Display base layer immediately.
6. **XOR recovery** — when group is complete minus 1, recover the missing unit.
7. **Radar quadrant rendering** — composite 4 quadrants into 64x64 display.

Steps 1-4 are independent. Steps 5-7 build on each other.

---

## 9. Quick reference: complete message flow

```
Client receives COBS-decoded bytes:
  │
  ├─ byte[0] == 0x04 → v4 frame
  │   ├─ Parse 6-byte header → (msg_type, flags, group_total, seq)
  │   ├─ Track seq for gap detection
  │   ├─ flags == 0x00 → single message
  │   │   └─ Prepend msg_type to payload → decode as v3
  │   └─ flags != 0x00 → FEC group member
  │       ├─ is_base → display immediately as preview
  │       ├─ is_unit → add to FEC group buffer
  │       ├─ is_parity → store for potential recovery
  │       └─ When group complete (or timed out):
  │           ├─ All units received → decode each, composite
  │           ├─ Missing 1 + have parity → XOR recover, then decode
  │           └─ Missing 2+ → display what you have + base
  │
  └─ byte[0] >= 0x10 → v3 message (legacy, decode as before)
```
