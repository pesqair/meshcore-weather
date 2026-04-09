"""Tests for MeshWX binary wire format pack/unpack."""

from meshcore_weather.protocol.meshwx import (
    pack_radar_grid,
    unpack_radar_grid,
    pack_warning_polygon,
    unpack_warning_polygon,
    pack_refresh_request,
    unpack_refresh_request,
    region_for_location,
    MSG_RADAR,
    MSG_WARNING,
    MSG_REFRESH,
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
        vertices = [
            (30.50, -97.75),
            (30.60, -97.60),
            (30.40, -97.60),
        ]
        msg = pack_warning_polygon(
            WARN_TORNADO, SEV_WARNING, 45,
            vertices, "TORNADO WARNING take shelter"
        )
        assert len(msg) <= 136
        assert msg[0] == MSG_WARNING
        result = unpack_warning_polygon(msg)
        assert result["warning_type"] == WARN_TORNADO
        assert result["severity"] == SEV_WARNING
        assert result["expiry_minutes"] == 45
        assert len(result["vertices"]) == 3
        assert abs(result["vertices"][0][0] - 30.50) < 0.001
        assert abs(result["vertices"][0][1] - (-97.75)) < 0.001
        assert "TORNADO WARNING" in result["headline"]

    def test_max_size(self):
        # 20 vertices + long headline
        vertices = [(30.0 + i * 0.01, -97.0 + i * 0.01) for i in range(20)]
        msg = pack_warning_polygon(
            WARN_TORNADO, SEV_WARNING, 60,
            vertices, "X" * 200
        )
        assert len(msg) <= 136

    def test_no_vertices(self):
        msg = pack_warning_polygon(WARN_TORNADO, SEV_WARNING, 30, [], "TEST")
        result = unpack_warning_polygon(msg)
        assert result["vertices"] == []
        assert result["headline"] == "TEST"


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
