"""Suggest a spot's swell window and offshore wind from the coastline alone.

Data: OpenStreetMap ocean polygons from OpenFreeMap vector tiles (free, no key), the same
source the live wave uses. No extra dependencies: a minimal vector-tile decoder and a
scanline rasteriser turn each tile into a 256x256 water mask.

    suggest(lat, lon, break_type) -> dict of SpotRequest fields + diagnostics

Method:
  1. Snap the pin onto water (people drop it on the sand).
  2. Facing: sample rings 150 m - 3 km around the pin; the mean direction of the water
     samples is the way the shore faces (the bay mouth, in a bay).
  3. Fetch: walk a ray every 5 degrees across the seaward half; distance to the first land
     (detailed tiles near the pin, coarse ones out to FETCH_MAX_KM).
  4. Window: bearings with enough open water. Blocked bearings inside it are shadow zones
     (an island offshore). Ideal = facing, moved into the window if blocked.
  5. Offshore wind: from the land, facing + 180, +-45.
Coastline can't show reefs, sandbars or bathymetry; see `confidence` and `notes`.
"""
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import requests

TILEJSON = "https://tiles.openfreemap.org/planet"
ATTRIBUTION = "© OpenStreetMap contributors, © OpenMapTiles (via OpenFreeMap)"
RES = 256                    # mask pixels per tile side
STEP_DEG = 5
FETCH_MAX_KM = 150.0
OPEN_MIN_KM = 25.0           # a bearing needs at least this much open water ...
OPEN_FRAC = 0.4              # ... and this fraction of the sector's longest fetch
TILE_TTL = 86400
# (max distance km, zoom, step km): detailed near the pin, coarse far out
BANDS = [(3.0, 14, 0.04), (20.0, 11, 0.25), (FETCH_MAX_KM, 8, 1.0)]

_session = requests.Session()
_tiles: dict = {}
_lock = threading.Lock()
_url = {"t": -1e9, "v": None}


class CoastError(Exception):
    """Map tiles unavailable."""


# ── vector tile decoding (only what we need: polygons in the water layer) ──────
def _varint(buf, i):
    out = shift = 0
    while True:
        b = buf[i]; i += 1
        out |= (b & 0x7F) << shift
        if b < 0x80:
            return out, i
        shift += 7


def _fields(buf):
    """Yield (field, wire, value) for a protobuf message; length-delimited values as memoryview."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        f, w = key >> 3, key & 7
        if w == 0:
            v, i = _varint(buf, i)
        elif w == 2:
            ln, i = _varint(buf, i)
            v = buf[i:i + ln]; i += ln
        elif w == 1:
            v = buf[i:i + 8]; i += 8
        elif w == 5:
            v = buf[i:i + 4]; i += 4
        else:
            raise ValueError("bad wire type")
        yield f, w, v


def _packed(buf):
    out, i = [], 0
    while i < len(buf):
        v, i = _varint(buf, i)
        out.append(v)
    return out


def _ocean_rings(pbf: bytes):
    """Rings (lists of (x, y) in tile units) of ocean polygons, and the tile extent."""
    buf = memoryview(pbf)
    for f, _, layer in _fields(buf):
        if f != 3:
            continue
        name, feats, keys, values, extent = None, [], [], [], 4096
        for lf, _, v in _fields(layer):
            if lf == 1: name = bytes(v).decode()
            elif lf == 2: feats.append(v)
            elif lf == 3: keys.append(bytes(v).decode())
            elif lf == 4:
                val = None
                for vf, _, vv in _fields(v):
                    if vf == 1: val = bytes(vv).decode()
                values.append(val)
            elif lf == 5: extent = v
        if name != "water":
            continue
        rings = []
        for feat in feats:
            tags, gtype, geom = [], 0, []
            for ff, _, v in _fields(feat):
                if ff == 2: tags = _packed(v)
                elif ff == 3: gtype = v
                elif ff == 4: geom = _packed(v)
            if gtype != 3:
                continue
            props = {keys[tags[k]]: values[tags[k + 1]] for k in range(0, len(tags) - 1, 2)}
            if props.get("class") != "ocean":
                continue
            x = y = 0; ring = []; i = 0
            while i < len(geom):
                cmd, cnt = geom[i] & 7, geom[i] >> 3; i += 1
                if cmd == 7:
                    if len(ring) > 2: rings.append(ring)
                    ring = []
                    continue
                for _ in range(cnt):
                    dx, dy = geom[i], geom[i + 1]; i += 2
                    x += (dx >> 1) ^ -(dx & 1); y += (dy >> 1) ^ -(dy & 1)
                    if cmd == 1:
                        if len(ring) > 2: rings.append(ring)
                        ring = [(x, y)]
                    else:
                        ring.append((x, y))
            if len(ring) > 2: rings.append(ring)
        return rings, extent
    return [], 4096


def _rasterise(rings, extent) -> bytearray:
    """Nonzero-winding scanline fill -> RES*RES mask, 1 = water. Holes (islands) wind the other way."""
    s = RES / extent
    edges = []
    for ring in rings:
        pts = [(x * s, y * s) for x, y in ring]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
            if y0 != y1:
                edges.append((x0, y0, x1, y1))
    mask = bytearray(RES * RES)
    for row in range(RES):
        yc = row + 0.5
        xs = []
        for x0, y0, x1, y1 in edges:
            if (y0 <= yc < y1) or (y1 <= yc < y0):
                xs.append((x0 + (yc - y0) * (x1 - x0) / (y1 - y0), 1 if y1 > y0 else -1))
        if not xs:
            continue
        xs.sort()
        wind, base = 0, row * RES
        for (xa, d), (xb, _) in zip(xs, xs[1:] + [(RES, 0)]):
            wind += d
            if wind:
                a, b = max(0, int(round(xa))), min(RES, int(round(xb)))
                if b > a:
                    mask[base + a:base + b] = b"\x01" * (b - a)
    return mask


# ── tiles ──────────────────────────────────────────────────────────────────────
def _tile_url() -> str:
    if not _url["v"] or time.monotonic() - _url["t"] > TILE_TTL:
        try:
            _url["v"] = _session.get(TILEJSON, timeout=8).json()["tiles"][0]
            _url["t"] = time.monotonic()
        except Exception as e:
            if not _url["v"]:
                raise CoastError(f"tile index unavailable: {type(e).__name__}")
    return _url["v"]


def _mask(z: int, x: int, y: int) -> bytearray:
    key = (z, x % 2 ** z, y)
    with _lock:
        hit = _tiles.get(key)
    if hit and time.monotonic() - hit[0] < TILE_TTL:
        return hit[1]
    url = _tile_url().replace("{z}", str(z)).replace("{x}", str(key[1])).replace("{y}", str(y))
    try:
        r = _session.get(url, timeout=10)
    except Exception as e:
        raise CoastError(f"tile {key} unavailable: {type(e).__name__}")
    if r.status_code == 204 or (r.status_code == 200 and not r.content):
        m = bytearray(RES * RES)                     # empty tile: no water features = land
    elif r.status_code == 200:
        m = _rasterise(*_ocean_rings(r.content))
    else:
        raise CoastError(f"tile {key}: HTTP {r.status_code}")
    with _lock:
        if len(_tiles) > 2000:
            _tiles.clear()
        _tiles[key] = (time.monotonic(), m)
    return m


def _pix(lat, lon, z):
    n = 2 ** z * RES
    x = (lon + 180) / 360 * n
    s = math.sin(math.radians(max(-85.0, min(85.0, lat))))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def is_water(lat, lon, z) -> bool:
    x, y = _pix(lat, lon, z)
    tx, ty = int(x // RES), int(y // RES)
    m = _mask(z, tx, ty)
    return m[int(y - ty * RES) * RES + int(x - tx * RES)] == 1


def _dest(lat, lon, bearing, km):
    d, b = km / 6371.0, math.radians(bearing)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(b))
    l2 = l1 + math.atan2(math.sin(b) * math.sin(d) * math.cos(p1), math.cos(d) - math.sin(p1) * math.sin(p2))
    return math.degrees(p2), (math.degrees(l2) + 540) % 360 - 180


def _prefetch(lat, lon):
    """Fetch every tile the rays can touch, in parallel."""
    keys = set()
    for km, z, _ in BANDS:
        x0, y0 = _pix(*_dest(lat, lon, 315, km * 1.42), z)
        x1, y1 = _pix(*_dest(lat, lon, 135, km * 1.42), z)
        for tx in range(int(min(x0, x1) // RES), int(max(x0, x1) // RES) + 1):
            for ty in range(int(min(y0, y1) // RES), int(max(y0, y1) // RES) + 1):
                keys.add((z, tx, ty))
    with ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda k: _mask(*k), keys))


# ── geometry ───────────────────────────────────────────────────────────────────
def norm(a):
    return a % 360


def ang_diff(a, b):
    d = abs(a - b) % 360
    return 360 - d if d > 180 else d


def snap_to_water(lat, lon, max_km=1.0):
    if is_water(lat, lon, 14):
        return lat, lon, 0.0
    for r in [0.02 * i for i in range(1, int(max_km / 0.02) + 1)]:
        for b in range(0, 360, 10):
            p = _dest(lat, lon, b, r)
            if is_water(*p, 14):
                return p[0], p[1], r
    return None


FACING_RINGS_KM = {"beach": (0.15, 0.3, 0.6, 1.0, 2.0, 3.0),     # validated on 42 breaks 2026-11-16:
                   "rivermouth": (0.15, 0.3, 0.6, 1.0, 2.0, 3.0),  # wider rings help headland breaks,
                   "reef": (0.5, 1.0, 2.0, 3.0), "point": (0.5, 1.0, 2.0, 3.0)}   # local ones help beaches


def facing(lat, lon, break_type="beach"):
    """Mean direction of water around the pin, and how clear-cut it is (0..1)."""
    sx = sy = 0.0; n = water = 0
    for km in FACING_RINGS_KM.get(break_type, FACING_RINGS_KM["beach"]):
        for b in range(0, 360, STEP_DEG):
            n += 1
            if is_water(*_dest(lat, lon, b, km), 14):
                water += 1
                sx += math.sin(math.radians(b)); sy += math.cos(math.radians(b))
    if not water:
        return None, 0.0, 0.0
    # clarity: how much the water looks like one clean arc. A single arc covering fraction f of
    # the circle has mean resultant sin(pi f)/(pi f); scattered water (harbour, rocks) has less.
    f = water / n
    expect = math.sin(math.pi * f) / (math.pi * f) if f < 1 else 1e-9
    clarity = min(1.0, (math.hypot(sx, sy) / water) / max(expect, 1e-9)) if f < 0.97 else 0.0
    return norm(math.degrees(math.atan2(sx, sy))), clarity, f


def fetch_km(lat, lon, bearing) -> float:
    """Open water along a bearing before the first land, up to FETCH_MAX_KM."""
    start = 0.0
    for end, z, step in BANDS:
        d = start + step
        while d <= end + 1e-9:
            if not is_water(*_dest(lat, lon, bearing, d), z):
                return round(d, 2)
            d += step
        start = end
    return FETCH_MAX_KM


def _runs(flags):
    """Contiguous True runs as (i0, i1) inclusive index pairs (no wrap: the list is one arc)."""
    out, i = [], 0
    while i < len(flags):
        if flags[i]:
            j = i
            while j + 1 < len(flags) and flags[j + 1]:
                j += 1
            out.append((i, j)); i = j + 1
        else:
            i += 1
    return out


# ideal swell comes this far round from the reef normal, from up the line, so the wave peels.
# Points wrap swell more than reefs. Heuristic, checked against 8 known breaks (2026-11-16).
PEEL_SWELL_OFFSET = {"point": 45, "reef": 20, "beach": 20, "rivermouth": 20}


def sdiff(a, b):
    """Signed a - b in degrees, -180..180."""
    return ((a - b + 540) % 360) - 180


def suggest(lat: float, lon: float, break_type: str = "beach",
            peel_deg: Optional[float] = None, hand: Optional[str] = None) -> dict:
    """SpotRequest direction fields for a pin. Raises CoastError only if map tiles are unreachable.

    Reefs and points: put the pin on the peak and give either `peel_deg` (the compass bearing the
    wave peels toward, e.g. from a drag on the map) or `hand` ("left" / "right", as the surfer
    rides it). The reef is taken to run along the peel line, facing the sea; the ideal swell comes
    PEEL_SWELL_OFFSET degrees round from up the line, so the wave peels rather than closes out."""
    _prefetch(lat, lon)
    snapped = snap_to_water(lat, lon)
    if not snapped:
        return {"status": "inland", "notes": ["No sea within 1 km of the pin. Move it onto the water off the break."]}
    plat, plon, moved = snapped
    face, clarity, water_frac = facing(plat, plon, break_type)
    if face is None or water_frac > 0.97:
        return {"status": "open-water", "pin": {"lat": round(plat, 5), "lon": round(plon, 5)},
                "notes": ["No coastline within 1 km. Move the pin closer to the break."]}

    reef_face, target, marked = face, face, False
    if peel_deg is not None:
        peel_deg = norm(peel_deg)
        reef_face = min((norm(peel_deg + 90), norm(peel_deg - 90)), key=lambda c: ang_diff(c, face))
        marked = True
    elif hand in ("left", "right"):
        peel_deg = norm(face + 90) if hand == "left" else norm(face - 90)   # surfer faces the shore
        marked = True
    if marked:
        up = norm(peel_deg + 180)                              # the swell comes from up the line
        target = norm(reef_face + math.copysign(PEEL_SWELL_OFFSET.get(break_type, 20), sdiff(up, reef_face)))
        hand = "left" if ang_diff(peel_deg, norm(reef_face + 90)) < 90 else "right"

    # seaward half, every STEP_DEG, as one arc from reef_face-90 to reef_face+90
    offs = list(range(-90, 91, STEP_DEG))
    centre = round(reef_face / STEP_DEG) * STEP_DEG
    bearings = [norm(centre + o) for o in offs]
    fetch = [fetch_km(plat, plon, b) for b in bearings]
    longest = max(fetch)
    need = max(min(OPEN_MIN_KM, longest), OPEN_FRAC * longest)
    open_ = [f >= need for f in fetch]

    mid = len(offs) // 2
    runs = _runs(open_)
    if not runs:
        return {"status": "enclosed", "pin": {"lat": round(plat, 5), "lon": round(plon, 5)},
                "facing_deg": round(face), "notes": ["Every seaward direction meets land quickly (enclosed bay or lagoon)."]}
    # ideal: the target bearing if open, else the nearest open bearing
    want = mid + round(sdiff(target, centre) / STEP_DEG)
    ideal_i = min((i for i, o in enumerate(open_) if o), key=lambda i: abs(i - want))
    i0, i1 = runs[0][0], runs[-1][1]                      # outer extent of the open bearings
    shadows = [[bearings[a], bearings[b]] for a, b in _runs([not o for o in open_[i0:i1 + 1]])
               for a, b in [(a + i0, b + i0)] if (b - a + 1) * STEP_DEG >= 10]

    notes = []
    conf = clarity * (1.0 if moved < 0.3 else 0.7)
    if marked:
        conf *= 0.9
        notes.append(f"Using your marked peel ({hand}): reef faces {round(reef_face)}, ideal swell set "
                     f"{PEEL_SWELL_OFFSET.get(break_type, 20)} deg up the line so it peels. Heuristic; adjust if you know better.")
    elif break_type in ("reef", "point"):
        conf *= 0.6
        notes.append(f"{break_type.title()} breaks depend on the seabed, which the coastline doesn't show. "
                     "Mark the peel direction (peel_deg or hand) for a better answer.")
    if ideal_i != want:
        notes.append("Land blocks swell straight onto the shore; ideal moved to the nearest open direction.")
    if moved:
        notes.append(f"Pin moved {round(moved * 1000)} m onto the water.")
    if clarity < 0.5:
        notes.append("Coastline shape is ambiguous here (bay, harbour or headland); check the result.")

    return {
        "status": "ok",
        "pin": {"lat": round(plat, 5), "lon": round(plon, 5), "moved_m": round(moved * 1000)},
        "facing_deg": round(face),
        "reef_facing_deg": round(reef_face) if marked else None,
        "peel_deg": round(peel_deg) if marked else None,
        "hand": hand if marked else None,
        "swell_dir_ideal": bearings[ideal_i],
        "swell_dir_min": bearings[i0],
        "swell_dir_max": bearings[i1],
        "shadow_zones": shadows,
        "wind_offshore_min": norm(round(face) + 180 - 45),
        "wind_offshore_max": norm(round(face) + 180 + 45),
        "confidence": round(conf, 2),
        "fetch_km": {str(b): f for b, f in zip(bearings, fetch)},
        "notes": notes,
        "attribution": ATTRIBUTION,
    }
