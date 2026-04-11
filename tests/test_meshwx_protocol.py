"""Tests for MeshWX binary wire format pack/unpack."""

from meshcore_weather.protocol.meshwx import (
    cobs_encode,
    cobs_decode,
    pack_radar_grid,
    unpack_radar_grid,
    pack_warning_polygon,
    unpack_warning_polygon,
    pack_refresh_request,
    unpack_refresh_request,
    pack_location,
    unpack_location,
    pack_data_request,
    unpack_data_request,
    pack_observation,
    unpack_observation,
    pack_forecast,
    unpack_forecast,
    state_to_idx,
    idx_to_state,
    wind_dir_to_nibble,
    nibble_to_wind_dir,
    region_for_location,
    LOC_ZONE,
    LOC_STATION,
    LOC_PLACE,
    LOC_LATLON,
    LOC_WFO,
    DATA_WX,
    DATA_FORECAST,
    SKY_CLEAR,
    SKY_RAIN,
    MSG_RADAR,
    MSG_WARNING,
    MSG_REFRESH,
    MSG_OBSERVATION,
    MSG_FORECAST,
    MSG_DATA_REQUEST,
    WARN_TORNADO,
    SEV_WARNING,
)


class TestRadarGrid:
    def test_pack_size(self):
        grid = [[0] * 16 for _ in range(16)]
        msg = pack_radar_grid(0x3, 0, 720, 55, grid)
        assert len(msg) == 133

    def test_round_trip(self):
        grid = [[0] * 16 for _ in range(16)]
        grid[0][0] = 0xA  # heavy rain
        grid[7][7] = 0x4  # light rain
        grid[15][15] = 0xE  # extreme
        msg = pack_radar_grid(0x3, 2, 720, 55, grid)
        result = unpack_radar_grid(msg)
        assert result["type"] == MSG_RADAR
        assert result["region_id"] == 0x3
        assert result["frame_seq"] == 2
        assert result["timestamp_utc_min"] == 720
        assert result["scale_km"] == 55
        assert result["grid"][0][0] == 0xA
        assert result["grid"][7][7] == 0x4
        assert result["grid"][15][15] == 0xE
        assert result["grid"][0][1] == 0  # untouched cell

    def test_nibble_packing(self):
        grid = [[0] * 16 for _ in range(16)]
        grid[0][0] = 0xF
        grid[0][1] = 0x1
        msg = pack_radar_grid(0, 0, 0, 12, grid)
        assert msg[5] == 0xF1  # high nibble = col0, low nibble = col1


class TestWarningPolygon:
    def test_round_trip(self):
        # v3 wire format: absolute Unix-minute expiry (uint32)
        expires = 29_500_000  # arbitrary large value
        vertices = [
            (30.50, -97.75),
            (30.60, -97.60),
            (30.40, -97.60),
        ]
        msg = pack_warning_polygon(
            WARN_TORNADO, SEV_WARNING, expires,
            vertices, "TORNADO WARNING take shelter"
        )
        assert len(msg) <= 136
        assert msg[0] == MSG_WARNING
        result = unpack_warning_polygon(msg)
        assert result["warning_type"] == WARN_TORNADO
        assert result["severity"] == SEV_WARNING
        assert result["expires_unix_min"] == expires
        assert len(result["vertices"]) == 3
        assert abs(result["vertices"][0][0] - 30.50) < 0.001
        assert abs(result["vertices"][0][1] - (-97.75)) < 0.001
        assert "TORNADO WARNING" in result["headline"]

    def test_max_size(self):
        vertices = [(30.0 + i * 0.01, -97.0 + i * 0.01) for i in range(20)]
        msg = pack_warning_polygon(
            WARN_TORNADO, SEV_WARNING, 29_500_000,
            vertices, "X" * 200
        )
        assert len(msg) <= 136

    def test_no_vertices(self):
        msg = pack_warning_polygon(WARN_TORNADO, SEV_WARNING, 29_500_000, [], "TEST")
        result = unpack_warning_polygon(msg)
        assert result["vertices"] == []
        assert result["headline"] == "TEST"

    def test_word_boundary_truncation(self):
        # Headline longer than available space should truncate at a word
        # boundary and end with "..." — never mid-word.
        vertices = [(30.5, -97.75), (30.6, -97.6), (30.4, -97.6)]
        long_headline = (
            "strong thunderstorm will impact portions of south central "
            "Caldwell, east central Guadalupe and northwestern Gonzales County"
        )
        msg = pack_warning_polygon(
            WARN_TORNADO, SEV_WARNING, 29_500_000, vertices, long_headline
        )
        result = unpack_warning_polygon(msg)
        # Either the full headline fits, or it ends with "..."
        if result["headline"] != long_headline:
            assert result["headline"].endswith("..."), (
                f"truncated headline should end with '...': {result['headline']!r}"
            )
            # And the last word before "..." should be complete (present in original)
            before_dots = result["headline"][:-3].rstrip()
            last_word = before_dots.split()[-1] if before_dots else ""
            assert last_word in long_headline, (
                f"mid-word truncation: {last_word!r} not in original"
            )


class TestRefreshRequest:
    def test_round_trip(self):
        msg = pack_refresh_request(0x3, 0x1, 720)
        assert len(msg) == 4
        assert msg[0] == MSG_REFRESH
        result = unpack_refresh_request(msg)
        assert result["region_id"] == 0x3
        assert result["request_type"] == 0x1
        assert result["client_newest"] == 720

    def test_empty_cache(self):
        msg = pack_refresh_request(0x0, 0x3, 0)
        result = unpack_refresh_request(msg)
        assert result["client_newest"] == 0


class TestRegionLookup:
    def test_austin_tx(self):
        rid = region_for_location(30.27, -97.74)
        assert rid == 0x3  # Southern

    def test_nyc(self):
        rid = region_for_location(40.71, -74.01)
        assert rid == 0x0  # Northeast

    def test_hawaii(self):
        rid = region_for_location(21.3, -157.8)
        assert rid == 0x8  # Hawaii

    def test_outside_all(self):
        rid = region_for_location(10.0, -50.0)
        assert rid is None


class TestCOBS:
    def test_no_nulls_in_output(self):
        data = bytes([0x20, 0x13, 0x00, 0x3C, 0x05])
        encoded = cobs_encode(data)
        assert 0x00 not in encoded

    def test_round_trip_simple(self):
        data = bytes([0x20, 0x13, 0x00, 0x3C, 0x05])
        assert cobs_decode(cobs_encode(data)) == data

    def test_round_trip_radar(self):
        grid = [[0] * 16 for _ in range(16)]
        grid[5][5] = 0xA
        msg = pack_radar_grid(0x3, 0, 720, 55, grid)
        encoded = cobs_encode(msg)
        assert 0x00 not in encoded
        assert cobs_decode(encoded) == msg

    def test_round_trip_warning(self):
        vertices = [(30.5, -97.75), (30.6, -97.6), (30.4, -97.6)]
        msg = pack_warning_polygon(WARN_TORNADO, SEV_WARNING, 29_500_000, vertices, "TEST")
        encoded = cobs_encode(msg)
        assert 0x00 not in encoded
        assert cobs_decode(encoded) == msg

    def test_all_nulls(self):
        data = bytes(10)
        encoded = cobs_encode(data)
        assert 0x00 not in encoded
        assert cobs_decode(encoded) == data

    def test_no_nulls(self):
        data = bytes(range(1, 100))
        encoded = cobs_encode(data)
        assert 0x00 not in encoded
        assert cobs_decode(encoded) == data

    def test_overhead_is_minimal(self):
        # 133-byte radar with many nulls should add at most ~2 bytes
        grid = [[0] * 16 for _ in range(16)]
        msg = pack_radar_grid(0x0, 0, 0, 55, grid)
        encoded = cobs_encode(msg)
        assert len(encoded) <= len(msg) + 3


class TestStateIndex:
    def test_common_states(self):
        assert state_to_idx("TX") == 42
        assert state_to_idx("CA") == 4
        assert state_to_idx("NY") == 31
        assert state_to_idx("FL") == 8

    def test_roundtrip(self):
        for state in ["TX", "CA", "AK", "HI", "PR", "GU"]:
            idx = state_to_idx(state)
            assert idx != 0xFF
            assert idx_to_state(idx) == state

    def test_unknown(self):
        assert state_to_idx("ZZ") == 0xFF


class TestWindDirection:
    def test_cardinal_points(self):
        assert wind_dir_to_nibble(0) == 0     # N
        assert wind_dir_to_nibble(90) == 4    # E
        assert wind_dir_to_nibble(180) == 8   # S
        assert wind_dir_to_nibble(270) == 12  # W

    def test_labels(self):
        assert nibble_to_wind_dir(0) == "N"
        assert nibble_to_wind_dir(4) == "E"
        assert nibble_to_wind_dir(8) == "S"
        assert nibble_to_wind_dir(12) == "W"


class TestLocationEncoding:
    def test_zone_roundtrip(self):
        encoded = pack_location(LOC_ZONE, "TXZ192")
        assert len(encoded) == 4
        loc, off = unpack_location(encoded)
        assert loc["type"] == LOC_ZONE
        assert loc["zone"] == "TXZ192"
        assert off == 4

    def test_station_roundtrip(self):
        encoded = pack_location(LOC_STATION, "KAUS")
        assert len(encoded) == 5
        loc, _ = unpack_location(encoded)
        assert loc["station"] == "KAUS"

    def test_place_roundtrip(self):
        encoded = pack_location(LOC_PLACE, 12345)
        assert len(encoded) == 4
        loc, _ = unpack_location(encoded)
        assert loc["place_id"] == 12345

    def test_latlon_roundtrip(self):
        encoded = pack_location(LOC_LATLON, (30.27, -97.74))
        assert len(encoded) == 7
        loc, _ = unpack_location(encoded)
        assert abs(loc["lat"] - 30.27) < 0.0001
        assert abs(loc["lon"] - (-97.74)) < 0.0001

    def test_wfo_roundtrip(self):
        encoded = pack_location(LOC_WFO, "EWX")
        assert len(encoded) == 4
        loc, _ = unpack_location(encoded)
        assert loc["wfo"] == "EWX"

    def test_pfm_point_roundtrip(self):
        from meshcore_weather.protocol.meshwx import LOC_PFM_POINT
        for idx in [0, 42, 567, 9999, 16777215]:
            encoded = pack_location(LOC_PFM_POINT, idx)
            assert len(encoded) == 4
            loc, off = unpack_location(encoded)
            assert loc["type"] == LOC_PFM_POINT
            assert loc["pfm_point_id"] == idx
            assert off == 4

    def test_pfm_point_overflow(self):
        from meshcore_weather.protocol.meshwx import LOC_PFM_POINT
        import pytest
        with pytest.raises(ValueError):
            pack_location(LOC_PFM_POINT, 1 << 24)
        with pytest.raises(ValueError):
            pack_location(LOC_PFM_POINT, -1)


class TestDataRequest:
    def test_wx_request(self):
        msg = pack_data_request(DATA_WX, LOC_ZONE, "TXZ192")
        assert msg[0] == MSG_DATA_REQUEST
        assert len(msg) == 8  # 4 header + 4 zone
        decoded = unpack_data_request(msg)
        assert decoded["data_type"] == DATA_WX
        assert decoded["location"]["zone"] == "TXZ192"

    def test_forecast_request_with_newest(self):
        msg = pack_data_request(DATA_FORECAST, LOC_STATION, "KAUS", client_newest=720)
        decoded = unpack_data_request(msg)
        assert decoded["data_type"] == DATA_FORECAST
        assert decoded["client_newest"] == 720
        assert decoded["location"]["station"] == "KAUS"


class TestObservation:
    def test_simple_roundtrip(self):
        msg = pack_observation(
            LOC_ZONE, "TXZ192",
            timestamp_utc_min=720,
            temp_f=72,
            dewpoint_f=55,
            wind_dir_deg=90,
            sky_code=SKY_CLEAR,
            wind_speed_mph=10,
        )
        decoded = unpack_observation(msg)
        assert decoded["location"]["zone"] == "TXZ192"
        assert decoded["timestamp_utc_min"] == 720
        assert decoded["temp_f"] == 72
        assert decoded["dewpoint_f"] == 55
        assert decoded["wind_dir"] == "E"
        assert decoded["sky_code"] == SKY_CLEAR
        assert decoded["wind_speed_mph"] == 10

    def test_rain_with_gusts(self):
        msg = pack_observation(
            LOC_STATION, "KAUS",
            timestamp_utc_min=900,
            temp_f=65,
            dewpoint_f=63,
            wind_dir_deg=45,
            sky_code=SKY_RAIN,
            wind_speed_mph=15,
            wind_gust_mph=28,
            visibility_mi=3,
            pressure_inhg=29.85,
        )
        decoded = unpack_observation(msg)
        assert decoded["sky_code"] == SKY_RAIN
        assert decoded["wind_gust_mph"] == 28
        assert decoded["visibility_mi"] == 3
        assert abs(decoded["pressure_inhg"] - 29.85) < 0.01

    def test_negative_temp(self):
        msg = pack_observation(
            LOC_ZONE, "AKZ001",
            timestamp_utc_min=0,
            temp_f=-20,
            dewpoint_f=-25,
            wind_dir_deg=315,
            sky_code=SKY_CLEAR,
            wind_speed_mph=5,
        )
        decoded = unpack_observation(msg)
        assert decoded["temp_f"] == -20
        assert decoded["dewpoint_f"] == -25

    def test_size_budget(self):
        msg = pack_observation(
            LOC_ZONE, "TXZ192", 720, 72, 55, 90, 0, 10
        )
        # 1 type + 4 loc + 2 ts + 1 temp + 1 dew + 1 dir/sky + 1 speed
        # + 1 gust + 1 vis + 1 press + 1 feels = 15 bytes
        assert len(msg) == 15


class TestForecast:
    def test_multi_period_roundtrip(self):
        periods = [
            {"period_id": 0, "high_f": 85, "low_f": 65, "sky_code": 2,
             "precip_pct": 20, "wind_dir_nibble": 4, "wind_speed_5mph": 2, "condition_flags": 0},
            {"period_id": 1, "high_f": 88, "low_f": 68, "sky_code": 1,
             "precip_pct": 10, "wind_dir_nibble": 8, "wind_speed_5mph": 3, "condition_flags": 1},
            {"period_id": 2, "high_f": 92, "low_f": 72, "sky_code": 0,
             "precip_pct": 0, "wind_dir_nibble": 4, "wind_speed_5mph": 1, "condition_flags": 0},
        ]
        msg = pack_forecast(LOC_ZONE, "TXZ192", 2, periods)
        decoded = unpack_forecast(msg)
        assert decoded["issued_hours_ago"] == 2
        assert len(decoded["periods"]) == 3
        assert decoded["periods"][0]["high_f"] == 85
        assert decoded["periods"][0]["wind_speed_mph"] == 10  # nibble 2 * 5
        assert decoded["periods"][2]["precip_pct"] == 0

    def test_na_high_low(self):
        periods = [
            {"period_id": 0, "high_f": 127, "low_f": 65, "sky_code": 2,
             "precip_pct": 20, "wind_dir_nibble": 0, "wind_speed_5mph": 0, "condition_flags": 0},
        ]
        msg = pack_forecast(LOC_STATION, "KAUS", 1, periods)
        decoded = unpack_forecast(msg)
        assert decoded["periods"][0]["high_f"] is None
        assert decoded["periods"][0]["low_f"] == 65

    def test_size_budget_7_periods(self):
        periods = [
            {"period_id": i, "high_f": 80, "low_f": 60, "sky_code": 0,
             "precip_pct": 0, "wind_dir_nibble": 0, "wind_speed_5mph": 0, "condition_flags": 0}
            for i in range(7)
        ]
        msg = pack_forecast(LOC_ZONE, "TXZ192", 0, periods)
        # 1 type + 4 loc + 1 issued + 1 count + 7*7 = 56 bytes
        assert len(msg) == 56


class TestWarningPolygonWideSpan:
    """Regression test: wide polygons should not have crossed lines."""

    def test_wide_polygon_preserves_vertex_positions(self):
        """A warning polygon spanning 3+ degrees should not collapse to ±1.27°."""
        from meshcore_weather.protocol.meshwx import (
            WARN_SEVERE_TSTORM, SEV_WARNING,
        )
        # Realistic severe thunderstorm polygon spanning ~2 degrees lat/lon
        vertices = [
            (30.5, -98.5),
            (31.8, -97.0),
            (31.0, -95.5),
            (29.5, -96.0),
            (29.0, -97.5),
        ]
        msg = pack_warning_polygon(
            WARN_SEVERE_TSTORM, SEV_WARNING, 29_500_000, vertices, "TEST"
        )
        decoded = unpack_warning_polygon(msg)
        assert len(decoded["vertices"]) == 5
        # Each decoded vertex should be within ~0.01° of original
        for orig, got in zip(vertices, decoded["vertices"]):
            assert abs(orig[0] - got[0]) < 0.01, f"lat drift: {orig} vs {got}"
            assert abs(orig[1] - got[1]) < 0.01, f"lon drift: {orig} vs {got}"


class TestZoneCodedWarning:
    def test_basic_roundtrip(self):
        from meshcore_weather.protocol.meshwx import (
            pack_warning_zones, unpack_warning_zones, WARN_TORNADO, SEV_WARNING,
            MSG_WARNING_ZONES,
        )
        zones = ["TXZ192", "TXZ193", "TXZ205"]
        msg = pack_warning_zones(
            WARN_TORNADO, SEV_WARNING, 29_500_000, zones, "TAKE SHELTER NOW"
        )
        assert msg[0] == MSG_WARNING_ZONES
        decoded = unpack_warning_zones(msg)
        assert decoded["warning_type"] == WARN_TORNADO
        assert decoded["severity"] == SEV_WARNING
        assert decoded["expires_unix_min"] == 29_500_000
        assert decoded["zones"] == zones
        assert decoded["headline"] == "TAKE SHELTER NOW"

    def test_size_comparison(self):
        """Zone-coded should be much smaller than polygon for same coverage."""
        from meshcore_weather.protocol.meshwx import (
            pack_warning_zones, WARN_TORNADO, SEV_WARNING,
        )
        # v3: 7-byte header + zones + headline
        msg = pack_warning_zones(
            WARN_TORNADO, SEV_WARNING, 29_500_000,
            ["TXZ192", "TXZ193", "TXZ205", "TXZ206", "TXZ207"],
            "TORNADO WARNING until 915 PM CDT"
        )
        # 7 header bytes + 15 zone bytes + ~33 headline = ~55 bytes
        assert len(msg) < 64


class TestOutlook:
    def test_roundtrip(self):
        from meshcore_weather.protocol.meshwx import (
            pack_outlook, unpack_outlook,
            HAZARD_SEVERE_THUNDER, HAZARD_FLASH_FLOOD, HAZARD_TORNADO,
            MSG_OUTLOOK,
        )
        days = [
            {"day_offset": 1, "hazards": [
                (HAZARD_SEVERE_THUNDER, 3),
                (HAZARD_FLASH_FLOOD, 2),
            ]},
            {"day_offset": 2, "hazards": [
                (HAZARD_TORNADO, 4),
            ]},
        ]
        msg = pack_outlook(LOC_ZONE, "TXZ192", 720, days)
        assert msg[0] == MSG_OUTLOOK
        decoded = unpack_outlook(msg)
        assert decoded["issued_utc_min"] == 720
        assert len(decoded["days"]) == 2
        assert decoded["days"][0]["day_offset"] == 1
        assert len(decoded["days"][0]["hazards"]) == 2
        assert decoded["days"][0]["hazards"][0]["hazard_type"] == HAZARD_SEVERE_THUNDER
        assert decoded["days"][1]["hazards"][0]["risk_level"] == 4


class TestStormReports:
    def test_roundtrip(self):
        from meshcore_weather.protocol.meshwx import (
            pack_storm_reports, unpack_storm_reports,
            EVENT_TORNADO, EVENT_HAIL, EVENT_TSTM_WIND,
            MSG_STORM_REPORTS,
        )
        reports = [
            {"event_type": EVENT_TORNADO, "magnitude": 0, "minutes_ago": 15, "place_id": 1234},
            {"event_type": EVENT_HAIL, "magnitude": 6, "minutes_ago": 22, "place_id": 5678},  # 1.5" hail
            {"event_type": EVENT_TSTM_WIND, "magnitude": 65, "minutes_ago": 30, "place_id": 9999},
        ]
        msg = pack_storm_reports(LOC_ZONE, "TXZ192", reports)
        assert msg[0] == MSG_STORM_REPORTS
        decoded = unpack_storm_reports(msg)
        assert len(decoded["reports"]) == 3
        assert decoded["reports"][0]["event_type"] == EVENT_TORNADO
        assert decoded["reports"][1]["magnitude"] == 6
        assert decoded["reports"][2]["place_id"] == 9999


class TestRainObs:
    def test_roundtrip(self):
        from meshcore_weather.protocol.meshwx import (
            pack_rain_obs, unpack_rain_obs,
            RAIN_LIGHT, RAIN_HEAVY, RAIN_TSTORM,
            MSG_RAIN_OBS,
        )
        cities = [
            {"place_id": 100, "rain_type": RAIN_LIGHT, "temp_f": 68},
            {"place_id": 200, "rain_type": RAIN_HEAVY, "temp_f": 72},
            {"place_id": 300, "rain_type": RAIN_TSTORM, "temp_f": 65},
        ]
        msg = pack_rain_obs(LOC_ZONE, "TXZ192", 720, cities)
        assert msg[0] == MSG_RAIN_OBS
        decoded = unpack_rain_obs(msg)
        assert decoded["timestamp_utc_min"] == 720
        assert len(decoded["cities"]) == 3
        assert decoded["cities"][0]["rain_type"] == RAIN_LIGHT
        assert decoded["cities"][0]["temp_f"] == 68


class TestWarningsNear:
    def test_roundtrip(self):
        from meshcore_weather.protocol.meshwx import (
            pack_warnings_near, unpack_warnings_near,
            WARN_TORNADO, WARN_FLASH_FLOOD, SEV_WARNING, SEV_WATCH,
            MSG_WARNINGS_NEAR,
        )
        warnings = [
            {"warning_type": WARN_TORNADO, "severity": SEV_WARNING,
             "expires_unix_min": 29_500_030, "zone": "TXZ192"},
            {"warning_type": WARN_FLASH_FLOOD, "severity": SEV_WATCH,
             "expires_unix_min": 29_500_480, "zone": "TXZ205"},
        ]
        msg = pack_warnings_near(LOC_ZONE, "TXZ192", warnings)
        assert msg[0] == MSG_WARNINGS_NEAR
        decoded = unpack_warnings_near(msg)
        assert len(decoded["warnings"]) == 2
        assert decoded["warnings"][0]["warning_type"] == WARN_TORNADO
        assert decoded["warnings"][0]["zone"] == "TXZ192"
        assert decoded["warnings"][1]["expires_unix_min"] == 29_500_480
