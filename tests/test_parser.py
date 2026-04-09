"""Tests for the EMWIN parser and geo resolver."""

from datetime import datetime, timezone

from meshcore_weather.geodata import resolver
from meshcore_weather.parser.weather import WeatherStore


def _make_emwin(emwin_id: str, text: str, ts: str = "") -> dict:
    """Create a mock EMWIN product dict with proper filename."""
    if not ts:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return {
        "filename": f"A_XXXX00KWBC_{ts}_000000-2-{emwin_id}.TXT",
        "raw_text": text,
    }


class TestWeatherStore:
    def test_ingest_and_find_by_emwin_id(self):
        store = WeatherStore()
        count = store.ingest([
            _make_emwin("ZFPEWXTX", "TXZ192-\n.TODAY...Sunny. High 85."),
            _make_emwin("RWREWXTX", "SKY/WX TMP\nAUSTIN SUNNY 85 55 40 S10 30.05"),
        ])
        assert count == 2

        # Should find ZFP for EWX+TX
        p = store._find("ZFP", "EWXTX")
        assert p is not None
        assert p.product_type == "ZFP"

    def test_find_newest(self):
        store = WeatherStore()
        now = datetime.now(timezone.utc)
        old_ts = (now - __import__('datetime').timedelta(hours=2)).strftime("%Y%m%d%H%M%S")
        new_ts = (now - __import__('datetime').timedelta(hours=1)).strftime("%Y%m%d%H%M%S")
        store.ingest([
            _make_emwin("ZFPEWXTX", "old forecast", ts=old_ts),
            _make_emwin("ZFPEWXTX", "new forecast", ts=new_ts),
        ])
        p = store._find("ZFP", "EWXTX")
        assert "new forecast" in p.raw_text

    def test_zfp_zone_parsing(self):
        store = WeatherStore()
        zfp_text = """FPUS54 KEWX 072238
ZFPEWX

TXZ192-TXZ193-080100-
Travis-Williamson-
Including the cities of Austin and Round Rock
1038 PM CDT TUE APR 7 2026

.TONIGHT...Clear. Low around 55. North wind 5 to 10 mph.
.WEDNESDAY...Sunny. High near 82. South wind 5 to 10 mph.
$$"""
        store.ingest([_make_emwin("ZFPEWXTX", zfp_text)])
        forecast = store._parse_zfp_zone(zfp_text, "TXZ192")
        assert "TONIGHT" in forecast
        assert "Clear" in forecast

    def test_rwr_city_parsing(self):
        store = WeatherStore()
        rwr_text = """ASCA42 KEWX 072200
RWREWX

CITY           SKY/WX    TMP DP  RH WIND       PRES
AUSTIN         CLEAR     68  45  43 N5        30.12
SAN ANTONIO    PTCLDY    70  48  45 SE8       30.10
$$"""
        result = store._parse_rwr_city(rwr_text, "AUSTIN")
        assert "Clear" in result
        assert "68F" in result

    def test_get_summary_austin(self):
        store = WeatherStore()
        store.ingest([
            _make_emwin("ZFPEWXTX", """TXZ192-080100-
.TONIGHT...Clear. Low 55. North wind 5 mph.
$$"""),
            _make_emwin("RWREWXTX", """SOUTH TEXAS REGIONAL WEATHER ROUNDUP

CITY           SKY/WX    TMP DP  RH WIND       PRES
AUSTIN         CLEAR     68  45  43 N5        30.12
$$"""),
        ])
        summary = store.get_summary("Austin TX")
        assert "Austin" in summary
        assert "Clear" in summary or "FCST" in summary

    def test_get_summary_unknown_location(self):
        store = WeatherStore()
        summary = store.get_summary("Zzxqvw")
        assert "Unknown" in summary


class TestLocationResolver:
    def test_resolve_station(self):
        resolver.load()
        r = resolver.resolve("KAUS")
        assert r is not None
        assert any("TX" in z for z in r["zones"])

    def test_resolve_city_state(self):
        resolver.load()
        r = resolver.resolve("Austin TX")
        assert r is not None
        assert "EWX" in r["wfos"]

    def test_resolve_zone(self):
        resolver.load()
        r = resolver.resolve("NYZ010")
        assert r is not None

    def test_resolve_unknown(self):
        resolver.load()
        r = resolver.resolve("Zzxqvw")
        assert r is None
