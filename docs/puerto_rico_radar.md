# Puerto Rico Radar — Status and Plan

## Current state

Puerto Rico has a radar region defined (0x9) but **no data source populates it**.

- The IEM NEXRAD composite (`n0q`) only covers CONUS (24°N-50°N). PR is at 18°N — outside the bounds.
- The EMWIN internet mirror (`tgftp.nws.noaa.gov`) only serves text products, not GIF radar images.
- PR text products (ZFP, RWR, PFM, SFT, AFD, warnings) all work fine via the San Juan WFO (JSJ/SJU).

## GOES EMWIN satellite downlink

The GOES HRIT/EMWIN satellite feed carries these radar image products:

| Product | Coverage | Format |
|---|---|---|
| `RADALLUS.GIF` | CONUS composite | Palette-indexed GIF |
| `RADNEAST.GIF` | Northeast regional | Palette-indexed GIF |
| `RADSEAST.GIF` | Southeast regional | Palette-indexed GIF |
| **`RADSPRIC.GIF`** | **Puerto Rico / Caribbean** | **Palette-indexed GIF** |

`RADSPRIC.GIF` is the PR radar product. Same format as `RADALLUS.GIF` — the existing GIF radar pipeline in `radar.py` should handle it with correct georeferencing bounds for the Caribbean region.

## Internet-mode equivalent

For testing without the satellite downlink, the NCEP GeoServer has an equivalent layer:

```
https://opengeo.ncep.noaa.gov/geoserver/carib/carib_bref_qcd/wms
  ?service=WMS&version=1.1.1&request=GetMap
  &layers=carib_bref_qcd
  &bbox=-90,15,-60,30
  &width=512&height=256
  &srs=EPSG:4326
  &format=image/png
  &transparent=true
```

This returns RGBA PNG (not palette-indexed), so the conversion pipeline needs to map RGBA colors to 4-bit reflectivity levels instead of using palette index lookup.

Verified working: returned 1.6% non-transparent pixels (active rain) on 2026-04-13.

## Implementation plan

### SDR mode (`emwin_source=sdr`)
1. Watch for `RADSPRIC.GIF` alongside `RADALLUS.GIF` in the EMWIN file stream
2. Use the same `extract_region_grid()` pipeline with PR-specific georeferencing:
   - lat_north: 19.5, lat_south: 17.0
   - lon_west: -68.0, lon_east: -65.0
3. Feed into region 0x9 (Puerto Rico, scale=12km)

### Internet mode (`emwin_source=internet`)
1. Fetch from NCEP WMS `carib:carib_bref_qcd` for the PR bounding box
2. Convert RGBA pixel colors to dBZ → 4-bit reflectivity (different from palette index path)
3. Feed into the same region 0x9

### Region 0x9 configuration (already defined)
```python
0x9: {"name": "Puerto Rico", "n": 19.5, "s": 17.0, "w": -68.0, "e": -65.0, "scale": 12}
```

## NWS radar site

The San Juan NEXRAD site is **TJUA** (also referenced as JUA/KJUA). The NWS RIDGE standard image is available at `https://radar.weather.gov/ridge/standard/TJUA_0.gif` but this is a rendered image with map overlays baked in — not suitable for data extraction.
