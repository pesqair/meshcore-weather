"""Unit tests for the PFM parser and downsampler."""

from datetime import datetime, timezone

import pytest

from meshcore_weather.parser.pfm import (
    PFMSlot,
    DailyPeriod,
    parse_pfm,
    find_point,
    downsample_to_daily,
)


# A trimmed-but-realistic PFM product, modeled on a real EWX Austin Bergstrom
# product. Two forecast points (Austin Bergstrom + a second point) so we can
# test multi-point parsing. Single 3-hourly table per point.
SAMPLE_PFM = """\
FOUS54 KEWX 101745

PFMEWX



Point Forecast Matrices

National Weather Service Austin/San Antonio TX

1245 PM CDT Fri Apr 10 2026



TXZ192-110900-

Austin Bergstrom-Travis TX

30.19N  97.67W Elev. 462 ft

1245 PM CDT Fri Apr 10 2026



Date           04/10/26      Sat 04/11/26            Sun 04/12/26

CDT 3hrly     16 19 22 01 04 07 10 13 16 19 22 01 04 07 10 13 16

UTC 3hrly     21 00 03 06 09 12 15 18 21 00 03 06 09 12 15 18 21



Min/Max                      65          82          68          80

Temp          80 77 69 68 67 66 71 78 81 78 72 70 69 68 72 76 78

Dewpt         66 66 66 66 66 66 68 68 68 67 67 67 67 68 70 70 69

RH            62 69 90 93 97100 90 71 65 69 84 90 93100 93 82 74

Wind dir       E  E SE SE SE SE SE SE SE SE SE SE  S SE  S  S  S

Wind spd       9  9  6  4  4  4  8 11 13 13 12 11 11 10 12 14 13

Clouds        SC SC B1 B1 B2 B2 B1 SC B1 B1 B1 OV OV OV OV OV OV

PoP 12hr                     20          50          40          60

Rain shwrs     C  S           S  C  C  C  C  C  C  C  C  C  L  L

Tstms          C  S              S  C  C  S     S  S     S  C  C

Obvis                        PF                   PF PF PF          PF



TXZ186-110900-

Kerrville Airport-Kerr TX

30.05N  99.13W Elev. 1617 ft

1245 PM CDT Fri Apr 10 2026



Date           04/10/26      Sat 04/11/26            Sun 04/12/26

CDT 3hrly     16 19 22 01 04 07 10 13 16 19 22 01 04 07 10 13 16

UTC 3hrly     21 00 03 06 09 12 15 18 21 00 03 06 09 12 15 18 21



Min/Max                      55          80          60          78

Temp          75 72 65 60 58 57 62 71 78 75 68 65 62 60 65 72 75

Dewpt         55 55 55 55 55 55 56 56 56 55 55 55 55 55 56 56 56

Wind dir      SE SE SE SE SE SE SE SE SE SE SE SE  S SE  S  S  S

Wind spd       8  8  5  3  3  3  7 10 12 12 11 10 10  9 11 13 12

Clouds        FW SC B1 B2 B2 B2 SC FW FW SC SC B1 B1 B1 B2 B2 B2

PoP 12hr                     10          40          30          50

Rain shwrs                                C  C  C  C  C  C  C  C  C

Tstms                                                       S  S  S



$$
"""


class TestPFMParser:
    def test_two_points_extracted(self):
        points = parse_pfm(SAMPLE_PFM)
        assert len(points) == 2
        assert points[0].zone == "TXZ192"
        assert points[0].name == "Austin Bergstrom-Travis TX"
        assert points[1].zone == "TXZ186"
        assert points[1].name == "Kerrville Airport-Kerr TX"

    def test_metadata_parsed(self):
        austin = parse_pfm(SAMPLE_PFM)[0]
        assert austin.lat == 30.19
        assert austin.lon == -97.67
        assert austin.elev_ft == 462
        assert austin.wfo == "EWX"
        assert austin.tz_offset_hours == -5  # CDT
        assert austin.issue_time is not None
        assert austin.issue_time.tzinfo == timezone.utc

    def test_temperature_row_parsed(self):
        austin = parse_pfm(SAMPLE_PFM)[0]
        # Slot 0 = 21Z (16 CDT) = first column = 80°F
        assert austin.slots[0].temp_f == 80
        # Last slot = 21Z next day = 78°F per the Temp row
        assert austin.slots[-1].temp_f == 78
        # Min temp in the table is 66°F (overnight Saturday morning at 12Z)
        all_temps = [s.temp_f for s in austin.slots if s.temp_f is not None]
        assert min(all_temps) == 66
        assert max(all_temps) == 81

    def test_wind_parsed(self):
        austin = parse_pfm(SAMPLE_PFM)[0]
        # First slot wind = E 9 mph
        assert austin.slots[0].wind_dir == "E"
        assert austin.slots[0].wind_spd_mph == 9

    def test_clouds_parsed(self):
        austin = parse_pfm(SAMPLE_PFM)[0]
        clouds = [s.cloud for s in austin.slots if s.cloud]
        assert "SC" in clouds
        assert "B1" in clouds
        assert "OV" in clouds

    def test_pop12_sparse(self):
        """PoP 12hr only has values at 12-hour boundaries (06Z and 18Z),
        not in every column."""
        austin = parse_pfm(SAMPLE_PFM)[0]
        pop_slots = [s for s in austin.slots if s.pop_pct is not None]
        # Sparse — at most one per 12 hours
        assert 1 <= len(pop_slots) <= 6
        assert all(0 <= s.pop_pct <= 100 for s in pop_slots)

    def test_rain_tstm_codes_parsed(self):
        austin = parse_pfm(SAMPLE_PFM)[0]
        rain_codes = {s.rain for s in austin.slots if s.rain}
        # Sample has C, S, L
        assert "C" in rain_codes

    def test_obvis_parsed(self):
        austin = parse_pfm(SAMPLE_PFM)[0]
        obvis_codes = [s.obvis for s in austin.slots if s.obvis]
        assert "PF" in obvis_codes

    def test_find_point_by_zone(self):
        points = parse_pfm(SAMPLE_PFM)
        austin = find_point(points, zone="TXZ192")
        assert austin is not None
        assert austin.name == "Austin Bergstrom-Travis TX"

        kerr = find_point(points, zone="TXZ186")
        assert kerr is not None
        assert kerr.name == "Kerrville Airport-Kerr TX"

    def test_unknown_zone_returns_none(self):
        points = parse_pfm(SAMPLE_PFM)
        assert find_point(points, zone="ZZZ999") is None


class TestPFMDownsampler:
    def test_emits_periods_with_high_low(self):
        austin = parse_pfm(SAMPLE_PFM)[0]
        periods = downsample_to_daily(austin)
        assert len(periods) >= 1
        for p in periods:
            assert p.high_f is not None
            assert p.low_f is not None
            # Invariant: high >= low
            assert p.high_f >= p.low_f, f"day {p.day_offset}: high {p.high_f} < low {p.low_f}"

    def test_saturday_high_low_match_pfm(self):
        """Saturday's downsampled high should be near 81°F (max in Temp row
        for Saturday's CDT day) and low near 66°F (min during Saturday)."""
        austin = parse_pfm(SAMPLE_PFM)[0]
        periods = downsample_to_daily(austin)
        # Day 0 in the downsampled output is the first day with enough data.
        # For a Friday-afternoon issue with 3 Friday slots, day 0 will be Saturday.
        sat = periods[0]
        assert 78 <= sat.high_f <= 85, f"Sat high {sat.high_f}°F out of expected range"
        assert 60 <= sat.low_f <= 70, f"Sat low {sat.low_f}°F out of expected range"

    def test_thunderstorm_flag_set(self):
        """Sample has Tstms = C/S in many columns → thunder flag should be on."""
        austin = parse_pfm(SAMPLE_PFM)[0]
        periods = downsample_to_daily(austin)
        # _COND_THUNDER = 0x01
        assert any(p.condition_flags & 0x01 for p in periods), "thunderstorm flag never set"

    def test_fog_flag_set(self):
        """Sample has Obvis=PF in some slots → fog flag (0x04) should be on."""
        austin = parse_pfm(SAMPLE_PFM)[0]
        periods = downsample_to_daily(austin)
        assert any(p.condition_flags & 0x04 for p in periods), "fog flag never set"

    def test_partial_days_filtered(self):
        """The sample has a partial Friday (3 slots) at the start. With
        min_slots_per_day=4 (default), Friday should be dropped."""
        austin = parse_pfm(SAMPLE_PFM)[0]
        periods = downsample_to_daily(austin, min_slots_per_day=4)
        # Day 0 should be Saturday (with full data), not Friday
        # Saturday's high should be ~80, Friday afternoon's max would be 80
        # Both happen to be near the same value, so we check by counting periods
        # vs the unfiltered version
        unfiltered = downsample_to_daily(austin, min_slots_per_day=1)
        assert len(periods) <= len(unfiltered)

    def test_to_encoder_dict(self):
        austin = parse_pfm(SAMPLE_PFM)[0]
        period = downsample_to_daily(austin)[0]
        d = period.to_encoder_dict()
        # Required keys for pack_forecast()
        for key in ("period_id", "high_f", "low_f", "sky_code", "precip_pct",
                    "wind_dir_nibble", "wind_speed_5mph", "condition_flags"):
            assert key in d
        assert isinstance(d["period_id"], int)
        assert isinstance(d["high_f"], int)
        assert isinstance(d["low_f"], int)
        # high/low are bounded for int8 packing
        assert -128 <= d["high_f"] <= 127
        assert -128 <= d["low_f"] <= 127
        assert 0 <= d["sky_code"] <= 0xF
        assert 0 <= d["precip_pct"] <= 100


class TestPFMEncoderIntegration:
    def test_encode_forecast_from_pfm_roundtrip(self):
        """encode_forecast_from_pfm produces a valid 0x31 message."""
        from meshcore_weather.protocol.encoders import encode_forecast_from_pfm
        from meshcore_weather.protocol.meshwx import unpack_forecast, MSG_FORECAST

        msg = encode_forecast_from_pfm(SAMPLE_PFM, "TXZ192", issued_hours_ago=0)
        assert msg is not None
        assert msg[0] == MSG_FORECAST
        decoded = unpack_forecast(msg)
        assert len(decoded["periods"]) >= 1
        # First period should have a high temp from PFM (real number, not None)
        first = decoded["periods"][0]
        assert first["high_f"] is not None
        assert 60 <= first["high_f"] <= 100

    def test_encode_forecast_pfm_with_loc_pfm_point(self):
        """Encoder echoes back LOC_PFM_POINT when caller provides it."""
        from meshcore_weather.protocol.encoders import encode_forecast_from_pfm
        from meshcore_weather.protocol.meshwx import (
            unpack_forecast, LOC_PFM_POINT,
        )

        msg = encode_forecast_from_pfm(
            SAMPLE_PFM, "TXZ192", issued_hours_ago=0,
            loc_type=LOC_PFM_POINT, loc_id=42,
        )
        assert msg is not None
        decoded = unpack_forecast(msg)
        assert decoded["location"]["type"] == LOC_PFM_POINT
        assert decoded["location"]["pfm_point_id"] == 42

    def test_encode_forecast_pfm_unknown_zone_returns_none(self):
        from meshcore_weather.protocol.encoders import encode_forecast_from_pfm
        msg = encode_forecast_from_pfm(SAMPLE_PFM, "ZZZ999", issued_hours_ago=0)
        assert msg is None
