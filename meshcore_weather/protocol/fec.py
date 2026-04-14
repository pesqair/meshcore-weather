"""Forward Error Correction — XOR parity for v4 multi-unit products.

Design principles (from MeshWX_Protocol_v4_Design.md):
  1. Every unit is independently useful
  2. XOR parity recovers any single missing unit in a group
  3. Base layer always arrives first (single-message preview)
  4. No back-chatter (pure forward error correction)
  5. Generic wrapper — same FEC frame fields for radar, text, any product

Usage:
  # Sender side
  units = [radar_nw, radar_ne, radar_sw, radar_se]  # raw payloads
  base = radar_32x32_preview  # optional base layer
  messages = fec_build_group(units, base_layer=base, msg_type=0x11,
                             group_id=0, seq_counter=counter)

  # Receiver side
  received = {0: base, 1: nw, 3: sw, 4: se, 5: parity}  # missed unit 2 (NE)
  recovered = fec_recover_unit(received, missing_index=2)
"""

import struct

from meshcore_weather.protocol.meshwx import V4_VERSION


# -- v4 FEC flag bits (byte 2 of v4 frame header) --
FEC_FLAG_IS_UNIT = 0x01       # bit 0: part of an FEC group
FEC_FLAG_IS_PARITY = 0x02     # bit 1: XOR parity unit
FEC_FLAG_IS_BASE = 0x04       # bit 2: independently useful base layer
# bits 3-4: fec_group_id (0-3)
# bits 5-7: unit_index (0-7)


def _pack_fec_flags(
    is_unit: bool = False,
    is_parity: bool = False,
    is_base: bool = False,
    group_id: int = 0,
    unit_index: int = 0,
) -> int:
    """Pack FEC flag bits into the v4 frame's msg_flags byte."""
    flags = 0
    if is_unit:
        flags |= FEC_FLAG_IS_UNIT
    if is_parity:
        flags |= FEC_FLAG_IS_PARITY
    if is_base:
        flags |= FEC_FLAG_IS_BASE
    flags |= (group_id & 0x03) << 3
    flags |= (unit_index & 0x07) << 5
    return flags


# -- XOR parity computation and recovery --


def xor_parity(payloads: list[bytes]) -> tuple[bytes, list[int]]:
    """Compute XOR parity across a list of payloads.

    All payloads are zero-padded to the length of the longest one,
    then XOR'd together byte-by-byte.

    Returns:
      (parity_bytes, length_map)

    length_map is a list of original (unpadded) lengths, one per payload.
    The client needs this to truncate after recovery.
    """
    if not payloads:
        return b"", []

    length_map = [len(p) for p in payloads]
    max_len = max(length_map)

    # XOR all payloads together
    parity = bytearray(max_len)
    for p in payloads:
        for i, b in enumerate(p):
            parity[i] ^= b

    return bytes(parity), length_map


# -- FEC group assembly (sender side) --


def fec_build_group(
    data_units: list[bytes],
    msg_type: int,
    group_id: int,
    seq_counter,
    base_layer: bytes | None = None,
) -> list[bytes]:
    """Build a complete FEC group as a list of v4-framed messages.

    Args:
      data_units: list of raw payloads (e.g., 4 radar quadrants).
                  Each is a complete v3-format message (starts with msg_type byte).
      msg_type: the message type byte (e.g., 0x11 for radar)
      group_id: FEC group ID (0-3), links related units within a broadcast cycle
      seq_counter: V4SequenceCounter instance (incremented per message)
      base_layer: optional v3-format base layer message (independently useful preview).
                  Sent first with is_base=True. NOT included in parity computation
                  (it's a bonus, not a data unit).

    Returns:
      List of v4-framed messages in send order:
        [base_layer?, data_unit_0, data_unit_1, ..., parity]
    """
    messages: list[bytes] = []
    group_total = len(data_units) + 1  # data units + parity
    if base_layer is not None:
        group_total += 1

    # 1. Base layer (if provided) — unit_index 0
    unit_offset = 0
    if base_layer is not None:
        flags = _pack_fec_flags(
            is_unit=True, is_base=True,
            group_id=group_id, unit_index=0,
        )
        msg = _v4_frame(base_layer, msg_type, flags, group_total, seq_counter.next())
        messages.append(msg)
        unit_offset = 1

    # 2. Data units — unit_index starts after base (if present)
    # Extract payloads (strip msg_type byte) for parity computation
    payloads: list[bytes] = []
    for i, unit in enumerate(data_units):
        flags = _pack_fec_flags(
            is_unit=True,
            group_id=group_id,
            unit_index=i + unit_offset,
        )
        msg = _v4_frame(unit, msg_type, flags, group_total, seq_counter.next())
        messages.append(msg)
        # For parity, use the payload after the msg_type byte
        payloads.append(unit[1:] if len(unit) > 1 else b"")

    # 3. Parity unit
    parity_data, length_map = xor_parity(payloads)
    # Parity payload = length_map (1 byte per unit) + parity data
    parity_payload = bytearray()
    parity_payload.append(len(length_map))
    for length in length_map:
        parity_payload.extend(struct.pack(">H", length))
    parity_payload.extend(parity_data)

    parity_flags = _pack_fec_flags(
        is_unit=True, is_parity=True,
        group_id=group_id,
        unit_index=len(data_units) + unit_offset,
    )
    # Parity message: msg_type byte + parity payload
    parity_msg = bytes([msg_type]) + bytes(parity_payload)
    msg = _v4_frame(parity_msg, msg_type, parity_flags, group_total, seq_counter.next())
    messages.append(msg)

    return messages


def _v4_frame(
    v3_msg: bytes, msg_type: int, flags: int, group_total: int, seq: int
) -> bytes:
    """Build a v4 frame from a v3 message with explicit flags and group_total."""
    header = struct.pack(">BBBBH",
        V4_VERSION,
        msg_type,
        flags & 0xFF,
        group_total & 0xFF,
        seq & 0xFFFF,
    )
    # Payload is v3 message without its type byte (type is in the header)
    return header + v3_msg[1:]


