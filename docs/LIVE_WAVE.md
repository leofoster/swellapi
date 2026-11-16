# Live wave scene: integration notes for the web app

Reference implementation: [`simulator/livewave.js`](../simulator/livewave.js), one
framework-free ES module. The simulator's **Live wave** button runs exactly this file
(try it at `/sim/`, press `l`). Copy the file; don't re-implement it.

## 1. Deliverable shape

```js
import { buildGeo, paramsFromHour, draw, ATTRIBUTION, CAPTION } from "./livewave.js";

const geo    = await buildGeo({ lat, lon, width: 992, height: 430 });   // per spot + box size; never throws
const params = paramsFromHour(hour, spot, geo, dayHours);               // when the hour changes
drawBackdrop(bgCtx, w, h, geo, params);                                 // backdrop canvas: only on change
draw(waveCtx, w, h, geo, params, tSeconds, { backdrop: false });        // wave canvas: every frame
```

**Use two stacked canvases.** The backdrop (photo, grading, dark sea) goes on the bottom
canvas and is redrawn only when the spot, box size, grading or day/night changes. The wave
canvas on top is transparent; `draw` with `backdrop: false` clears and repaints only the
small box around the break. (Single canvas also works: `draw(ctx, ...)` without the option
paints both, but costs a full-canvas pass per frame.)

- `hour` is one item of `days[].hours[]` from `POST /forecast`. `spot` needs only
  `break_type`. `dayHours` (that day's hours) is optional; it lets the tide use the real range.
- `draw` cover-fits the scene into `w × h`. Your existing loop owns DPR, resizing,
  visibility and reduced motion; pass a monotonic `t` in seconds.
- No per-frame allocations in the steady state: shoreline data and stroke geometry live in
  reusable typed arrays, and the spray pool is fixed.
- Tuning: geometry options in `DEFAULTS` (passed to `buildGeo`), look and behaviour in the
  exported `STYLE` object (bump `STYLE._v` after changing it so params recompute).
- **What it assumes you have:**
  - Dynamic `import()` from jsDelivr for `@mapbox/vector-tile@2.0.3` and `pbf@4.0.1`
    (about 10 KB together). If Base44 lets you add npm packages, install those versions
    and change the two imports.
  - `fetch` to `tiles.openfreemap.org` (CORS `*`, no key).
  - Esri tiles loaded as plain `<img>`. **Do not set `crossOrigin`, and never read
    pixels:** the canvas is deliberately left "tainted".

## 2-4. Coastline: use OpenFreeMap ocean polygons, not Overpass

**Recommendation:** skip Overpass. `buildGeo` reads the `water` layer (`class: "ocean"`)
from OpenFreeMap's OpenMapTiles vector tiles at z14:

- **Closed polygons:** they're already closed per tile, with the water side defined, so
  there's nothing to close or orient (this answers question 4).
- **Fill rule:** use **nonzero**, not even-odd. Neighbouring tiles overlap in their
  buffer, and even-odd cancels the overlap out into stripes.
- **Shoreline:** the coastline is the polygon edges that aren't tile-clip edges.
- **Water side:** water is on the right of travel in the MVT ring order. `buildGeo`
  still verifies this by sampling `isPointInPath` and flips any run that disagrees.
- **When to run it:** on every scene open. There's nothing to store, no batch job, and
  no fair-use question (OpenFreeMap is free with no key; it's donation-funded, so be
  reasonable).
- **Licence line** (shown in `ATTRIBUTION`):
  `© OpenStreetMap contributors, © OpenMapTiles` next to the coastline, and
  `Imagery © Esri, Maxar, Earthstar Geographics` where you already credit Esri.
- **No coastline found** (open water, a lake, missing data): `geo.status === "synthetic"`.
  A straight beach is drawn across the lower third, facing the swell, so the scene still
  animates. Show a quiet note ("generic beach").
- **If you ever need Overpass instead,** this is the query (3 km radius, 25 s timeout):
  ```
  [out:json][timeout:25];
  way["natural"="coastline"](around:3000,{lat},{lon});
  out geom;
  ```
  - **When to run it:** once at break creation, stored on the record and fetched lazily
    if missing. That's fine under Overpass fair use (well under 10,000 requests a day).
  - **Licence:** stored OSM data falls under ODbL. Credit "© OpenStreetMap contributors",
    and share-alike applies if you redistribute the extract.
  - **Orientation:** OSM coastline ways run with **land on the left, water on the
    right**.
  - **Closing them:** clip each way to the box, then join consecutive pieces by walking
    the box edge clockwise from each exit point to the next entry point.
  - **The trap:** this is the fragile part, and it's the reason to prefer the vector tiles.

## 5. Shoreline orientation and peel direction

- **Use vectors, not angles,** to avoid any 0°/360° wrap handling.
- **Swell travel vector** on a north-up screen (y down): `θ = (dir_from + 180)°`,
  giving `tx = sin θ`, `ty = −cos θ`.
- **Peel:** each shoreline sample gets an offset `peel = ((x − pinX)·tx + (y − pinY)·ty) × breakPeel`.
  Samples further along the travel direction are reached later, so each crest runs
  obliquely along the coast and breaks progressively. That *is* the peel direction; no
  sign convention is needed.
- **Seaward normal:** the right-hand normal `(−dy, dx)` of the shoreline direction
  (water on the right), smoothed at 5 scales. Large offsets use the smoother normals.
- **Offshore, onshore or cross-shore:** compare the wind travel vector with the seaward
  normal nearest the pin. More than 0.25 is offshore, less than −0.25 is onshore,
  anything between is cross-shore. 5 mph or less is glassy.
- **If you do need an angle,** use `δ = ((a − b + 540) % 360) − 180`.

## 6. Tide → metres → pixels

1. **Height:** `tideM = (tide_level − 0.5) × range`, where `range` is the day's
   `max − min` of `tide_height_m` (default 1.0 m).
2. **Break depth:** depth ≈ `surf_height / 0.78 − tideM`. High tide means deeper water,
   so the wave breaks closer in.
3. **Distance from shore:** `depth / slope`, in metres.
4. **Pixels:** divide by `metresPerPixel(lat, zoom)`.
5. **Keep it on screen:** clamp to `[5 px, 0.3 × min(w, h)]` so the band can't slide
   off. Typical effect: about ±20 m on a beach (slope 0.025), ±6 m on a reef.

## 7. Break types

Two characters are done properly:

| Type | Slope | Peel | Character |
|---|---|---|---|
| `beach` | 0.025 | 0.08 | Wide surf zone, closes out, long whitewater |
| `point` | 0.05 | 0.35 | Peels down the line |
| `reef` | 0.08 | 0.30 | Point-like, breaks tight to the reef |
| `rivermouth` | 0.03 | 0.12 | Sandbar, beach-like |

The numbers live in `BREAKS`; tune them there.

## 8. Second swell

- **The dominant swell** (`surf_height_m`) gets the full treatment: lines, lip and whitewater.
- **The secondary swell** (`secondary_swell_*`) is drawn at 50% weight as **unbroken
  offshore lines only**, with no lip or foam. It adds crossed texture out the back
  without competing with the breaking line.
- **Blocked swell:** if `in_shadow && !is_wrap`, the dominant swell is drawn at 15% (and
  you can caption it "swell blocked here").
- **Size field:** `surf_height_m` from engine v2 already accounts for shadowing and the
  window, so don't re-derive size from `effective_height_m`.

## 9. Contrast recipe

Layers, in order:
1. **Photo.**
2. **Overall darkening:** `rgba(0,0,0,0.30)` everywhere (+0.25 at night).
3. **Sea:** `rgba(2,10,22,0.62)` filled inside the ocean mask.
4. **Clip:** everything after this is clipped to the sea.
5. **Unbroken crests:** each is two strokes.
   - A dark face `rgba(0,6,16,0.55a)`, width `2.2+3a`, offset 2.5 px **shoreward**.
   - Then the crest `rgba(170,210,240,0.6a)`, width `0.8+1.4a`.
6. **Breaking lip:**
   - Undershadow `rgba(0,10,24,0.6a)`, width `3+4a`, offset 3 px shoreward.
   - Then pure white `rgba(255,255,255,0.95)`, width `1.6+3a`.
7. **Whitewater:**
   - A bore `rgba(242,249,255,0.7a)`, width `3.5+8a`.
   - Plus a trailing haze `rgba(230,242,255,0.22a)`, width `4+9a`, offset 5 px seaward.
8. **Shore wash:** a faint line at the shoreline, pulsing with the period.

`a` is wave size (0.15–1) × set factor × distance fade. **The dark shoreward stroke
under every white line is what makes crests read**, including over foam and bright water.

## Only the break, not the whole coast

`buildGeo` keeps just the coastline at the pin: the stretch nearest it, `breakHalfWidthM`
(600 m) either side, smoothed over `smoothM` (60 m) so inlets, rocks and jetties don't make
squiggles, with the ends tapering out. Offshore, crests straighten into lines square to the
swell; near shore they bend to follow the coast. That reads as refraction and can't loop in bays.
Coast facing away from a swell gets no lines from it (shelter). Each swell is judged by its own
direction, and the one that actually reaches the break (size × exposure) is the breaking one:
e.g. a big swell blocked by an island plus a small one that wraps in.

Pin placement matters: the scene centres on the nearest coast to the pin, so put the pin on
the water just off the break.

## 10. Performance

- **Measured** (desktop, 1240×540, both swells, two-canvas setup, GPU work included by
  reading back once after 120 frames): **0.24–1.34 ms per frame** across Nazaré, Mundaka,
  Pipeline, Southlands, J-Bay, Hossegor and the generic beach. Strong onshore wind is the
  top end, because whitecaps use the whole canvas.
- What keeps it cheap: the break-only coastline (60–110 samples instead of ~900), crests binned
  so each point touches only the ~6 crests it's on, about 40 batched strokes from reusable
  typed arrays, a cached backdrop, and a dirty rectangle (typically ~300×400 px) for the
  clear, the sea-mask composite and the layer composite.
- **Phones:** roughly 3–5× slower, so expect about 1–4 ms. If over 2 ms, cut in this order:
  1. `STYLE.whitecaps = false` (it's the only full-canvas effect).
  2. `buildGeo({..., opts: { sampleSpacing: 8, breakHalfWidthM: 400 } })`: fewer samples.
  3. `STYLE.secondary = false`.
  4. Run at 30 fps on phones (waves move slowly; it's barely noticeable).
- **Building at the box size** (500×340 on phones) also shrinks the work.

## 11. Caption

Use `CAPTION`: **"Illustration drawn from the forecast — not a prediction of exactly
where waves break"**. In a tight footer, "Illustration drawn from the forecast" is enough.

## Not verified

- Phone timings (only desktop was measured).
- OpenFreeMap's long-term availability and terms (free, no key, donation-funded).
- Esri's terms for this kind of display-only use in a commercial app.
- How the break positions in `LW_PRESETS` in the simulator line up with real breaks
  (they're approximate).
