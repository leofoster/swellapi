/**
 * livewave.js — stylised, forecast-driven wave scene for any coastal coordinate.
 *
 * Framework-free ES module for canvas 2D. Three steps:
 *
 *   const geo    = await buildGeo({ lat, lon, width, height });        // once per spot + box size
 *   const params = paramsFromHour(hour, spot, geo);                    // when the hour changes
 *   draw(ctx, width, height, geo, params, tSeconds);                   // every frame
 *
 * Fastest setup: two stacked canvases. Backdrop canvas: drawBackdrop() only when spot / size /
 * grading / day-night changes. Wave canvas (transparent, on top): draw(..., { backdrop: false })
 * every frame; it only touches the small area around the break.
 *
 * - Backdrop: Esri World Imagery, display only. Pixels are never read (the canvas
 *   becomes cross-origin "tainted", which is fine: we never call getImageData).
 * - Water: OpenStreetMap ocean polygons from OpenFreeMap vector tiles. Closed
 *   polygons with a defined water side: no coastline-closing step needed.
 * - Waves: crests are the shoreline offset seaward. One wave per `period` seconds at
 *   real-time speed, spaced by a shoaled wavelength, breaking at a distance set by
 *   size / seabed slope / tide, staggered along the coast by the swell's travel
 *   direction (peel), then whitewater. Wind: glassy, offshore spray, onshore chop.
 *
 * Stylised illustration, not a refraction model.
 *
 * Tuning: geometry options in DEFAULTS (passed to buildGeo), look and behaviour in STYLE
 * (mutable; bump STYLE._v after changing peelScale or glassyMph so params recompute).
 *
 * Dependencies (runtime, from jsDelivr; or npm-install the same versions):
 *   @mapbox/vector-tile@2.0.3, pbf@4.0.1
 * Attribution to display: see ATTRIBUTION.
 */

export const ATTRIBUTION =
  "Imagery © Esri, Maxar, Earthstar Geographics · Coastline © OpenStreetMap contributors, © OpenMapTiles";
export const CAPTION = "Illustration drawn from the forecast — not a prediction of exactly where waves break";

// Geometry options for buildGeo (fixed per scene).
export const DEFAULTS = {
  zoom: 16,                         // 15-17; imagery is sharp to 18, vector water tops out at 14 (scaled up)
  sampleSpacing: 6,                 // px between shoreline samples (8 on phones)
  maxShorePoints: 700,              // cap, nearest the pin first (400 on phones)
  breakOnly: true,                  // only the stretch of coast at the pin, not every inlet in view
  breakHalfWidthM: 600,             // how much coast either side of the pin gets waves
  smoothM: 60,                      // smooth the coastline over this length first (removes rocks/inlets)
  tideRangeM: 1.0,                  // used when the day's tide range is unknown
};

// Look and behaviour. Live-tunable: change a value, bump STYLE._v, the next frame uses it.
export const STYLE = {
  // waves
  shoal: 0.45,          // nearshore wavelength = shoal * 1.56 * T^2 (deep-water wavelength)
  setLen: 7,            // waves per set
  setStrength: 0.45,    // how much bigger set waves are than lulls (0 = all the same)
  slopeScale: 1,        // x seabed slope: higher = breaks closer to shore
  peelScale: 1,         // x peel stagger: 0 = whole coast breaks at once, higher = longer peel
  foamScale: 1,         // x whitewater length
  lipBand: 0.25,        // fraction of the break distance drawn as the white breaking lip
  fadeWaves: 4,         // how many wavelengths out the swell lines fade to nothing
  secondary: true,      // draw the second swell (offshore lines only)
  shoreWash: true,      // faint pulsing line at the shoreline
  // look
  widthScale: 1,        // x all line widths
  crestColor: "#aad2f0", crestAlpha: 0.6,
  faceAlpha: 0.55, faceOffset: 2.5,     // dark shoreward stroke under each crest: what makes it read
  lipAlpha: 0.95, foamAlpha: 0.7, trailAlpha: 0.22,
  seaDark: 0.62, landDark: 0.30, nightExtra: 0.25,
  // wind
  glassyMph: 5,         // at or below: glassy, any direction
  chopAmp: 2.5,         // px wobble on swell lines in onshore wind
  sprayRate: 0.25,      // offshore spray emission (0 = off)
  whitecaps: true,      // flickering whitecaps in strong onshore wind
  // time
  timeScale: 1,         // x wave speed on top of the caller's clock
  _v: 0,
};

// Two characters done well; the others map onto them.
//   slope: seabed slope -> how far out it breaks; peel: stagger along the coast
//   (low = closes out together, high = peels down the line); foam: whitewater length
export const BREAKS = {
  beach:      { slope: 0.025, peel: 0.08, foam: 0.85 },   // wide surf zone, closes out, lots of whitewater
  point:      { slope: 0.05,  peel: 0.35, foam: 0.6 },    // peels down the headland
  reef:       { slope: 0.08,  peel: 0.30, foam: 0.5 },    // point-like, breaks tight to the reef
  rivermouth: { slope: 0.03,  peel: 0.12, foam: 0.8 },    // sandbar: beach-like
};

const TILE = 256;
const ESRI = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const OFM = "https://tiles.openfreemap.org/planet";
const LEVELS = [2, 6, 14, 30, 60];          // normal-smoothing half-windows, in samples
const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
const lonToX = (lon, z) => (lon + 180) / 360 * TILE * 2 ** z;
const latToY = (lat, z) => { const s = Math.sin(lat * Math.PI / 180); return (0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * TILE * 2 ** z; };
export const metresPerPixel = (lat, z) => 156543.03392 * Math.cos(lat * Math.PI / 180) / 2 ** z;

let libs = null;
async function vectorLibs() {
  if (!libs) {
    const [{ VectorTile }, { default: Pbf }] = await Promise.all([
      import("https://cdn.jsdelivr.net/npm/@mapbox/vector-tile@2.0.3/+esm"),
      import("https://cdn.jsdelivr.net/npm/pbf@4.0.1/+esm")]);
    const tj = await (await fetch(OFM)).json();
    libs = { VectorTile, Pbf, url: tj.tiles[0] };
  }
  return libs;
}
const loadImage = url => new Promise(ok => { const im = new Image(); im.onload = () => ok(im); im.onerror = () => ok(null); im.src = url; });

/**
 * Build everything that depends only on place and box size. Async; never throws.
 * geo.status: "ok" | "synthetic" (no coastline found: a straight beach is used so the
 * scene still animates) | "no-imagery".
 */
export async function buildGeo({ lat, lon, width, height, opts = {} }) {
  const o = { ...DEFAULTS, ...opts }, z = o.zoom, W = Math.round(width), H = Math.round(height);
  const ox = lonToX(lon, z) - W / 2, oy = latToY(lat, z) - H / 2;
  const geo = { width: W, height: H, lat, lon, zoom: z, mpp: metresPerPixel(lat, z), image: null, ocean: null,
                shores: [], status: "ok", opts: o, pin: { x: W / 2, y: H / 2 } };

  const image = document.createElement("canvas"); image.width = W; image.height = H;
  const ictx = image.getContext("2d"), jobs = [];
  for (let tx = Math.floor(ox / TILE); tx <= Math.floor((ox + W) / TILE); tx++)
    for (let ty = Math.floor(oy / TILE); ty <= Math.floor((oy + H) / TILE); ty++)
      jobs.push(loadImage(ESRI.replace("{z}", z).replace("{y}", ty).replace("{x}", tx))
        .then(im => im && ictx.drawImage(im, tx * TILE - ox, ty * TILE - oy)));

  const rings = [];
  try {
    const { VectorTile, Pbf, url } = await vectorLibs();
    const vz = Math.min(z, 14), f = 2 ** (z - vz);
    for (let tx = Math.floor(ox / f / TILE); tx <= Math.floor((ox + W) / f / TILE); tx++)
      for (let ty = Math.floor(oy / f / TILE); ty <= Math.floor((oy + H) / f / TILE); ty++)
        jobs.push(fetch(url.replace("{z}", vz).replace("{x}", tx).replace("{y}", ty))
          .then(r => (r.ok ? r.arrayBuffer() : null)).then(buf => {
            const layer = buf && new VectorTile(new Pbf(buf)).layers.water;
            if (!layer) return;
            for (let i = 0; i < layer.length; i++) {
              const ft = layer.feature(i);
              if (ft.properties.class !== "ocean") continue;
              for (const ring of ft.loadGeometry())
                rings.push(ring.map(p => ({
                  x: (tx * TILE + p.x / layer.extent * TILE) * f - ox,
                  y: (ty * TILE + p.y / layer.extent * TILE) * f - oy,
                  clip: p.x <= 0 || p.y <= 0 || p.x >= layer.extent || p.y >= layer.extent })));
            }
          }).catch(() => {}));
  } catch { /* coastline unavailable: synthetic fallback below */ }
  await Promise.all(jobs);
  geo.image = image;

  if (rings.length) {
    const ocean = new Path2D();
    for (const r of rings) { r.forEach((p, i) => (i ? ocean.lineTo(p.x, p.y) : ocean.moveTo(p.x, p.y))); ocean.closePath(); }
    geo.ocean = ocean;
    geo.shores = shorelines(rings, ocean, W, H, o);
  }
  if (geo.shores.length && o.breakOnly) isolateBreak(geo);
  if (!geo.shores.length) syntheticBeach(geo);
  return geo;
}

// Keep only the coastline at the break: the run nearest the pin, a window either side of the
// nearest point, smoothed (so inlets, rocks and jetties don't make squiggles), with ends that
// taper to nothing instead of stopping dead.
function isolateBreak(geo) {
  const o = geo.opts, { x: px, y: py } = geo.pin;
  let best = null;
  for (const s of geo.shores) for (let i = 0; i < s.n; i++) {
    const d = (s.xs[i] - px) ** 2 + (s.ys[i] - py) ** 2;
    if (!best || d < best.d) best = { s, i, d };
  }
  const { s, i: c } = best, sp = o.sampleSpacing;
  const half = Math.max(20, Math.round(o.breakHalfWidthM / geo.mpp / sp));
  const a = Math.max(0, c - half), b = Math.min(s.n - 1, c + half), n = b - a + 1;
  if (n < 8) return;
  // moving-average smoothing, twice (approximately Gaussian), window = smoothM
  const k = Math.max(1, Math.round(o.smoothM / geo.mpp / sp / 2));
  let xs = s.xs.slice(a, b + 1), ys = s.ys.slice(a, b + 1);
  for (let pass = 0; pass < 2; pass++) {
    const nx = new Float32Array(n), ny = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      let sx = 0, sy = 0, m = 0;
      for (let j = Math.max(0, i - k); j <= Math.min(n - 1, i + k); j++) { sx += xs[j]; sy += ys[j]; m++; }
      nx[i] = sx / m; ny[i] = sy / m;
    }
    xs = nx; ys = ny;
  }
  const sh = withNormals(xs, ys);
  // keep the original water side: compare with the unsmoothed normal near the pin
  if (sh.nx[3][c - a] * s.nx[3][c] + sh.ny[3][c - a] * s.ny[3][c] < 0)
    for (let L = 0; L < LEVELS.length; L++) for (let i = 0; i < n; i++) { sh.nx[L][i] *= -1; sh.ny[L][i] *= -1; }
  // taper the last 25% at each end
  sh.taper = new Float32Array(n);
  const edge = Math.max(1, Math.round(n * 0.25));
  for (let i = 0; i < n; i++) { const e = Math.min(i, n - 1 - i) / edge; sh.taper[i] = e >= 1 ? 1 : e * e * (3 - 2 * e); }
  sh.center = c - a;
  geo.shores = [sh];
}

// No coastline (open water, lake, missing data): a straight beach across the lower third,
// so a break with no coastline still animates. paramsFromHour turns it to face the swell.
function syntheticBeach(geo) {
  const { width: W, height: H } = geo, y = H * 0.72, n = Math.ceil(W / 6) + 1;
  const xs = new Float32Array(n), ys = new Float32Array(n);
  for (let i = 0; i < n; i++) { xs[i] = i * 6; ys[i] = y; }
  const ocean = new Path2D(); ocean.rect(0, 0, W, y);
  geo.ocean = ocean; geo.status = "synthetic";
  geo.shores = [withNormals(xs, ys, [{ x: 0, y: -1 }])];
}

function shorelines(rings, ocean, W, H, o) {
  const raw = [];
  for (const ring of rings) {                       // runs of non-clip vertices = real coastline
    const n = ring.length; if (n < 3) continue;
    let start = ring.findIndex(p => p.clip); if (start < 0) start = 0;
    let run = [];
    for (let k = 1; k <= n; k++) {
      const p = ring[(start + k) % n];
      if (p.clip) { if (run.length > 1) raw.push(run); run = []; } else run.push(p);
    }
    if (run.length > 1) raw.push(run);
  }
  const inBox = p => p.x > -60 && p.y > -60 && p.x < W + 60 && p.y < H + 60;
  const runs = [];
  for (const r of raw) {                            // resample, split where it leaves the box
    let seg = [];
    const push = p => { if (inBox(p)) seg.push(p); else { if (seg.length > 8) runs.push(seg); seg = []; } };
    for (let i = 1; i < r.length; i++) {
      const a = r[i - 1], b = r[i], steps = Math.max(1, Math.round(Math.hypot(b.x - a.x, b.y - a.y) / o.sampleSpacing));
      for (let s = 0; s < steps; s++) push({ x: a.x + (b.x - a.x) * s / steps, y: a.y + (b.y - a.y) * s / steps });
    }
    if (seg.length > 8) runs.push(seg);
  }
  // keep the coastline nearest the pin, up to the point budget
  const cx = W / 2, cy = H / 2, d = s => Math.min(...s.filter((_, i) => i % 4 === 0).map(p => Math.hypot(p.x - cx, p.y - cy)));
  runs.sort((a, b) => d(a) - d(b));
  const kept = []; let budget = o.maxShorePoints;
  for (const r of runs) { if (budget <= 0) break; kept.push(r.length > budget ? r.slice(0, budget) : r); budget -= r.length; }

  // MVT rings put water on the right of travel (y down); verify by sampling, flip per run.
  const test = document.createElement("canvas").getContext("2d");
  return kept.map(pts => {
    const xs = Float32Array.from(pts, p => p.x), ys = Float32Array.from(pts, p => p.y);
    const sh = withNormals(xs, ys);
    let wet = 0, dry = 0;
    for (let i = 0; i < xs.length; i += Math.max(1, xs.length >> 4))
      test.isPointInPath(ocean, xs[i] + sh.nx[1][i] * 8, ys[i] + sh.ny[1][i] * 8, "nonzero") ? wet++ : dry++;
    if (dry > wet) for (let L = 0; L < LEVELS.length; L++) for (let i = 0; i < xs.length; i++) { sh.nx[L][i] *= -1; sh.ny[L][i] *= -1; }
    return sh;
  });
}

// Seaward unit normals at several smoothing scales (bigger offsets need smoother normals).
function withNormals(xs, ys, fixed) {
  const n = xs.length, nx = [], ny = [];
  for (const w of LEVELS) {
    const ax = new Float32Array(n), ay = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      if (fixed) { ax[i] = fixed[0].x; ay[i] = fixed[0].y; continue; }
      const a = Math.max(0, i - w), b = Math.min(n - 1, i + w);
      const dx = xs[b] - xs[a], dy = ys[b] - ys[a], m = Math.hypot(dx, dy) || 1;
      ax[i] = -dy / m; ay[i] = dx / m;              // right-hand normal in y-down screen space
    }
    nx.push(ax); ny.push(ay);
  }
  return { xs, ys, nx, ny, n, peel: new Float32Array(n), u: new Float32Array(n), ex: new Float32Array(n).fill(1),
           taper: null, center: n >> 1 };
}

/**
 * Turn one hour of the forecast API (+ the spot record) into draw params.
 * hour: an item of days[].hours[]; spot: { break_type }; geo: from buildGeo.
 * Recompute when the hour changes, not per frame.
 */
export function paramsFromHour(hour, spot = {}, geo = null, dayHours = null) {
  const brk = BREAKS[spot.break_type] || BREAKS.beach;
  const surf = hour.surf_height_m ?? hour.effective_height_m ?? hour.swell_height_m ?? 0.5;
  // tide: 0-1 level -> metres above mid-tide using the day's range when we have it
  let range = DEFAULTS.tideRangeM;
  if (dayHours) {
    const hs = dayHours.map(h => h.tide_height_m).filter(v => v != null);
    if (hs.length > 3) range = Math.max(0.2, Math.max(...hs) - Math.min(...hs));
  }
  const p = {
    hb: clamp(surf, 0.1, 6),
    T: clamp(hour.swell_peak_period_s ?? hour.swell_period_s ?? 10, 4, 22),   // peak period when available
    dir: hour.swell_direction_deg ?? 270,                       // degrees the swell comes FROM
    h2: hour.secondary_swell_height_m ?? 0,
    T2: hour.secondary_swell_peak_period_s ?? hour.secondary_swell_period_s ?? 0,
    dir2: hour.secondary_swell_direction_deg ?? null,
    windMph: hour.wind_speed_mph ?? 0,
    windDeg: hour.wind_direction_deg ?? 0,                      // degrees the wind comes FROM
    gustMph: hour.wind_gusts_mph ?? null,
    tideM: ((hour.tide_level ?? 0.5) - 0.5) * range,           // metres above mid-tide
    daylight: hour.daylight !== false,
    blocked: !!hour.in_shadow && !hour.is_wrap,                 // swell can't reach this spot
    quality: hour.quality_key || null,
    brk,
  };
  return prepare(p, geo);
}

// Per-params precompute (direction-dependent): wind class at the pin, peel offsets per sample.
function prepare(p, geo) {
  const th = (p.dir + 180) * Math.PI / 180;
  p.tx = Math.sin(th); p.ty = -Math.cos(th);                   // swell travel, screen space (north up)
  const wth = (p.windDeg + 180) * Math.PI / 180;
  p.wx = Math.sin(wth); p.wy = -Math.cos(wth);                 // wind travel
  if (!geo || !geo.shores.length) return p;
  // shoreline normal nearest the pin decides offshore / onshore
  let best = null;
  for (const s of geo.shores) for (let i = 0; i < s.n; i += 4) {
    const d = Math.hypot(s.xs[i] - geo.pin.x, s.ys[i] - geo.pin.y);
    if (!best || d < best.d) best = { d, nx: s.nx[3][i], ny: s.ny[3][i] };
  }
  const along = p.wx * best.nx + p.wy * best.ny;               // >0: blowing out to sea
  p.wind = p.windMph <= STYLE.glassyMph ? "glassy" : along > 0.25 ? "offshore" : along < -0.25 ? "onshore" : "cross-shore";
  // synthetic beach: turn it so it faces the swell
  for (const s of geo.shores) { s.cx = s.xs[s.center]; s.cy = s.ys[s.center]; }
  // Two trains. The one that actually reaches this coast (size x exposure) breaks; the other is
  // drawn as faint offshore lines. E.g. a big swell blocked by the island + a smaller one that
  // wraps in: the smaller one is what you'd see breaking.
  const synth = geo.status === "synthetic";            // generic beach: swell always comes straight in
  const mk = (h, T, dir) => { const th = (dir + 180) * Math.PI / 180;
    return { h, T, dir, tx: synth ? 0 : Math.sin(th), ty: synth ? 1 : -Math.cos(th) }; };
  const a = mk(p.blocked ? p.hb * 0.15 : p.hb, p.T, p.dir);
  const b = p.h2 > 0.2 && p.T2 > 0 ? mk(p.h2 * 1.2, p.T2, p.dir2 ?? p.dir) : null;
  const ea = exposure(geo, a), eb = b ? exposure(geo, b) : 0;
  if (b && b.h * eb > a.h * ea) { p.trains = [{ ...a, w: 0.5, offshoreOnly: true }, { ...b, w: 1, offshoreOnly: false }]; p.breaking = "secondary"; }
  else { p.trains = b ? [{ ...b, w: 0.5, offshoreOnly: true }, { ...a, w: 1, offshoreOnly: false }] : [{ ...a, w: 1, offshoreOnly: false }]; p.breaking = "primary"; }
  p._geo = geo; p._v = STYLE._v;
  return p;
}

// ── drawing ────────────────────────────────────────────────────────────────
// Per frame, the GPU work is: one cached backdrop image, ~40 batched strokes onto a
// layer, one mask composite, one layer composite. No clip paths, no per-run strokes.
const NQ = 8;                       // alpha/width buckets per stroke type
const LAYERS = 6;                   // 0 swell face, 1 swell crest, 2 lip face, 3 lip, 4 foam, 5 foam trail
// Geometry for each (layer, bucket) goes into reusable typed arrays and is replayed with one
// beginPath/stroke per bucket (much cheaper than building Path2D objects point by point).
const pools = Array.from({ length: LAYERS * NQ }, () => ({ x: new Float32Array(2048), y: new Float32Array(2048), m: new Uint8Array(2048), n: 0 }));
let bx0 = 0, by0 = 0, bx1 = 0, by1 = 0;          // bounding box of this frame's geometry
function push(b, x, y, move) {
  if (x < bx0) bx0 = x; if (x > bx1) bx1 = x; if (y < by0) by0 = y; if (y > by1) by1 = y;
  if (b.n === b.x.length) {
    const g = (a, T) => { const z = new T(a.length * 2); z.set(a); return z; };
    b.x = g(b.x, Float32Array); b.y = g(b.y, Float32Array); b.m = g(b.m, Uint8Array);
  }
  b.x[b.n] = x; b.y[b.n] = y; b.m[b.n] = move; b.n++;
}
// crest binning scratch: which points each crest touches
let binCount = new Int32Array(256), binStart = new Int32Array(257), binList = new Int32Array(8192);
const spray = { x: new Float32Array(512), y: new Float32Array(512), vx: new Float32Array(512), vy: new Float32Array(512), life: new Float32Array(512) };
let lastT = null, crestRGB = "170,210,240";
const hexRGB = h => { const n = parseInt(h.slice(1), 16); return `${(n >> 16) & 255},${(n >> 8) & 255},${n & 255}`; };


// Backdrop (photo + grading + dark sea) and the sea mask, rendered once and re-rendered only
// when grading settings or day/night change.
function cacheBackdrop(geo, p) {
  const S = STYLE, W = geo.width, H = geo.height, key = `${S.landDark}|${S.seaDark}|${S.nightExtra}|${p.daylight}`;
  if (geo._bgKey === key) return;
  const mk = () => { const c = document.createElement("canvas"); c.width = W; c.height = H; return c; };
  const bg = geo._bg || (geo._bg = mk()), b = bg.getContext("2d");
  b.globalCompositeOperation = "source-over";
  b.fillStyle = "#05080d"; b.fillRect(0, 0, W, H);
  if (geo.image) b.drawImage(geo.image, 0, 0);
  if (geo.status === "synthetic") { b.fillStyle = "#3a3226"; b.fillRect(0, H * 0.72, W, H * 0.28); }   // plain sand
  b.fillStyle = `rgba(0,0,0,${S.landDark + (p.daylight ? 0 : S.nightExtra)})`; b.fillRect(0, 0, W, H);
  if (geo.ocean) { b.fillStyle = `rgba(2,10,22,${S.seaDark})`; b.fill(geo.ocean, "nonzero"); }
  if (!geo._mask && geo.ocean) {
    geo._mask = mk(); const m = geo._mask.getContext("2d"); m.fillStyle = "#fff"; m.fill(geo.ocean, "nonzero");
  }
  if (!geo._layer) geo._layer = mk();
  geo._bgKey = key;
}

// How exposed the coast at the pin is to a swell train: 1 = faces it, 0 = faces away.
function exposure(geo, tr) {
  let sum = 0, n = 0;
  for (const s of geo.shores) for (let i = Math.max(0, s.center - 10); i <= Math.min(s.n - 1, s.center + 10); i++) {
    sum += clamp(0.35 - (s.nx[3][i] * tr.tx + s.ny[3][i] * tr.ty), 0, 1); n++;
  }
  return n ? sum / n : 1;
}

const fit = (w, h, geo) => { const k = Math.max(w / geo.width, h / geo.height); return { k, ox: (w - geo.width * k) / 2, oy: (h - geo.height * k) / 2 }; };

/** Paint the graded backdrop (photo, darkening, dark sea). Cheap after the first call: one image.
 *  For best performance put it on its own canvas behind the waves and call it only when the
 *  spot, the box size, STYLE grading or day/night changes. */
export function drawBackdrop(ctx, w, h, geo, params) {
  cacheBackdrop(geo, params);
  const { k, ox, oy } = fit(w, h, geo);
  ctx.save(); ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.fillStyle = "#05080d"; ctx.fillRect(0, 0, w, h); ctx.restore();
  ctx.save(); ctx.translate(ox, oy); ctx.scale(k, k); ctx.drawImage(geo._bg, 0, 0); ctx.restore();
}

/** Paint one frame. t = seconds (monotonic). Cover-fits geo into w x h.
 *  opts.backdrop (default true): also paint the backdrop. With backdrop:false, ctx must be a
 *  transparent canvas used only for waves; draw() then clears just the area that changed. */
export function draw(ctx, w, h, geo, params, t, opts) {
  const S = STYLE, W = geo.width, H = geo.height, p = params, withBg = !opts || opts.backdrop !== false;
  if (p._geo !== geo || p._v !== S._v) prepare(p, geo);
  t *= S.timeScale;
  cacheBackdrop(geo, p);
  const { k, ox, oy } = fit(w, h, geo);
  if (withBg) drawBackdrop(ctx, w, h, geo, p);
  if (!geo.ocean) return;

  // build this frame's geometry and its bounding box
  const dt = lastT == null ? 0 : Math.min(0.05, Math.max(0, t - lastT)); lastT = t;
  crestRGB = hexRGB(S.crestColor);
  for (const b of pools) b.n = 0;
  bx0 = by0 = Infinity; bx1 = by1 = -Infinity;
  for (const tr of p.trains) if (!tr.offshoreOnly || S.secondary) train(geo, p, tr, t);
  const caps = S.whitecaps && p.wind === "onshore" && p.windMph > 12;
  const pad = 30 * S.widthScale + 20;
  let box = null;
  if (caps) box = [0, 0, W, H];
  else if (bx1 >= bx0) {
    const x0 = Math.max(0, Math.floor(bx0 - pad)), y0 = Math.max(0, Math.floor(by0 - pad));
    box = [x0, y0, Math.min(W, Math.ceil(bx1 + pad)) - x0, Math.min(H, Math.ceil(by1 + pad)) - y0];
  }
  // wave canvas without backdrop: clear last frame's box and this one's
  if (!withBg) {
    ctx.save(); ctx.setTransform(1, 0, 0, 1, 0, 0);
    for (const r of [geo._prevBox, box]) if (r) ctx.clearRect(Math.floor(ox + r[0] * k) - 1, Math.floor(oy + r[1] * k) - 1, Math.ceil(r[2] * k) + 2, Math.ceil(r[3] * k) + 2);
    ctx.restore();
  }
  geo._prevBox = box;
  if (!box || box[2] <= 0 || box[3] <= 0) return;

  // waves on the layer, inside the box only; masked to the sea; composited once
  const L = geo._layer.getContext("2d"), [x, y, bw, bh] = box;
  L.globalCompositeOperation = "source-over"; L.clearRect(x, y, bw, bh);
  L.lineCap = "round"; L.lineJoin = "round";
  strokeBuckets(L);
  if (S.shoreWash) shoreWash(L, geo, p, t);
  if (p.wind === "offshore") sprayStep(L, dt);
  if (caps) whitecaps(L, W, H, p, t);
  L.globalCompositeOperation = "destination-in"; L.drawImage(geo._mask, x, y, bw, bh, x, y, bw, bh);
  L.globalCompositeOperation = "source-over";
  ctx.save(); ctx.translate(ox, oy); ctx.scale(k, k);
  ctx.drawImage(geo._layer, x, y, bw, bh, x, y, bw, bh);
  ctx.restore();
}

// One swell train, added to the stroke buckets. offshoreOnly: the secondary swell is unbroken
// lines only (no lip/foam), so it adds texture without competing with the breaking line.
function train(geo, p, tr, t) {
  const S = STYLE, mpp = geo.mpp, b = p.brk, sp = geo.opts.sampleSpacing;
  const hb = tr.h, T = tr.T, weight = tr.w, offshoreOnly = tr.offshoreOnly, tx = tr.tx, ty = tr.ty;
  const peelF = b.peel * S.peelScale;
  const lam = clamp(1.56 * T * T * S.shoal / mpp, 14, 220);              // px between crests
  const v = lam / T;                                                      // one crest per period
  const depth = Math.max(0.3, hb / 0.78 - p.tideM);                       // breaking depth, tide-adjusted
  const bd = clamp(depth / (b.slope * S.slopeScale) / mpp, 5, 0.3 * Math.min(geo.width, geo.height));
  const amp = clamp(hb / 2.5, 0.15, 1) * weight;
  const chop = p.wind === "onshore" ? clamp((p.windMph - 8) / 20, 0, 1) * S.chopAmp : 0;
  const travel = t * v, base = Math.floor(travel / lam), frac = travel - base * lam;
  const far = bd + S.fadeWaves * lam, lipAt = bd * (1 - S.lipBand), foamLen = Math.max(1, bd * b.foam * S.foamScale);
  const off = S.faceOffset, sprayOn = p.wind === "offshore" && S.sprayRate > 0;

  for (const s of geo.shores) {
    // per-point, for this train's direction: peel stagger, position along a straight crest,
    // and shelter (coast facing the swell gets full waves, side-on less, facing away none)
    for (let i = 0; i < s.n; i++) {
      s.peel[i] = ((s.xs[i] - geo.pin.x) * tx + (s.ys[i] - geo.pin.y) * ty) * peelF;
      s.u[i] = (s.xs[i] - s.cx) * -ty + (s.ys[i] - s.cy) * tx;
      s.ex[i] = clamp(0.35 - (s.nx[3][i] * tx + s.ny[3][i] * ty), 0, 1) * (s.taper ? s.taper[i] : 1);
    }
    // Bin points by crest: point i is on crest j when 0 <= j*lam - frac + peel[i] <= far.
    let j0 = Infinity, j1 = -Infinity;
    for (let i = 0; i < s.n; i++) {
      const lo = Math.ceil((frac - s.peel[i]) / lam), hi = Math.floor((far + frac - s.peel[i]) / lam);
      if (lo < j0) j0 = lo; if (hi > j1) j1 = hi;
    }
    const nj = j1 - j0 + 1;
    if (!(nj > 0)) continue;
    if (binCount.length < nj) { binCount = new Int32Array(nj * 2); binStart = new Int32Array(nj * 2 + 1); }
    binCount.fill(0, 0, nj);
    let total = 0;
    for (let i = 0; i < s.n; i++) {
      const lo = Math.ceil((frac - s.peel[i]) / lam), hi = Math.floor((far + frac - s.peel[i]) / lam);
      for (let j = lo; j <= hi; j++) { binCount[j - j0]++; total++; }
    }
    if (binList.length < total) binList = new Int32Array(total * 2);
    binStart[0] = 0;
    for (let q = 0; q < nj; q++) binStart[q + 1] = binStart[q] + binCount[q];
    binCount.fill(0, 0, nj);
    for (let i = 0; i < s.n; i++) {                           // ascending i, so each bin stays in coastline order
      const lo = Math.ceil((frac - s.peel[i]) / lam), hi = Math.floor((far + frac - s.peel[i]) / lam);
      for (let j = lo; j <= hi; j++) { const q = j - j0; binList[binStart[q] + binCount[q]++] = i; }
    }

    for (let q = 0; q < nj; q++) {
      const j = q + j0, D = j * lam - frac;
      const set = 1 - S.setStrength + S.setStrength * (0.5 + 0.5 * Math.sin((j + base) * 2 * Math.PI / S.setLen));
      const a = amp * set;
      // normals: blend the two nearest smoothing levels by distance, so crests change shape
      // smoothly as they travel instead of snapping between levels
      const wq = Math.abs(D) * 0.5 / sp;
      let l0 = 0; while (l0 < LEVELS.length - 2 && LEVELS[l0 + 1] <= wq) l0++;
      const f = clamp((wq - LEVELS[l0]) / (LEVELS[l0 + 1] - LEVELS[l0]), 0, 1), g = 1 - f;
      const AX = s.nx[l0], AY = s.ny[l0], BX = s.nx[l0 + 1], BY = s.ny[l0 + 1];
      let cur = -1, last = -2, px = 0, py = 0, pnx = 0, pny = 0;
      for (let k = binStart[q], e = binStart[q + 1]; k < e; k++) {
        const i = binList[k];
        if (i !== last + 1) cur = -1;                          // gap in the coastline: new run
        let d = D + s.peel[i];
        if (chop && d > bd) d += Math.sin(i * 0.35 + t * 1.6 + j * 1.3) * chop;
        const nx = AX[i] * g + BX[i] * f, ny = AY[i] * g + BY[i] * f;   // close enough to unit
        // near shore crests follow the coast; far out they straighten into lines square to the
        // swell (offshore swell is straight). Blending between the two reads as refraction and
        // can't loop the way a pure offset curve does in bays.
        let x = s.xs[i] + nx * d, y = s.ys[i] + ny * d;
        const w = d <= bd ? 0 : d >= far ? 1 : ((d - bd) / (far - bd));
        if (w > 0) {
          const ws = w * w * (3 - 2 * w);
          const lx = s.cx - ty * s.u[i] - tx * D, ly = s.cy + tx * s.u[i] - ty * D;
          x += (lx - x) * ws; y += (ly - y) * ws;
        }
        let kind, fade;
        if (d > bd) { kind = 1; fade = 1 - (d - bd) / (far - bd); }
        else if (offshoreOnly) { kind = 0; fade = 0; }
        else if (d > lipAt) { kind = 2; fade = 1; }
        else { kind = 3; fade = d / foamLen; if (fade < 0.15) fade = 0.15; else if (fade > 1) fade = 1; }
        const av = a * fade * s.ex[i];
        const st = kind && av > 0.02 ? kind * NQ + Math.min(NQ - 1, (av * NQ) | 0) : -1;
        if (st >= 0) {
          const bq = st % NQ, l1 = kind === 1 ? 0 : kind === 2 ? 2 : 4;
          const o1 = kind === 1 ? -off : kind === 2 ? -off * 1.2 : 0, o2 = kind === 3 ? 5 : 0;
          const A = pools[l1 * NQ + bq], B = pools[(l1 + 1) * NQ + bq];
          if (st !== cur) {                                    // start a run, joined to the previous point
            const join = last === i - 1;
            const sx = join ? px : x, sy = join ? py : y, snx = join ? pnx : nx, sny = join ? pny : ny;
            push(A, sx + snx * o1, sy + sny * o1, 1); push(B, sx + snx * o2, sy + sny * o2, 1);
            cur = st;
          }
          push(A, x + nx * o1, y + ny * o1, 0); push(B, x + nx * o2, y + ny * o2, 0);
          if (kind === 2 && sprayOn && (i & 3) === 0) sprayEmit(x, y, p, a);
        } else cur = -1;
        last = i; px = x; py = y; pnx = nx; pny = ny;
      }
    }
  }
}

// Contrast recipe: a dark face on the shoreward side under every crest, then the light line.
function strokeBuckets(L) {
  const S = STYLE, k = S.widthScale;
  for (let layer = 0; layer < LAYERS; layer++)
    for (let q = 0; q < NQ; q++) {
      const b = pools[layer * NQ + q]; if (b.n < 2) continue;
      const a = (q + 0.5) / NQ;
      switch (layer) {
        case 0: L.strokeStyle = `rgba(0,6,16,${S.faceAlpha * a})`; L.lineWidth = (2.2 + 3 * a) * k; break;
        case 1: L.strokeStyle = `rgba(${crestRGB},${S.crestAlpha * a})`; L.lineWidth = (0.8 + 1.4 * a) * k; break;
        case 2: L.strokeStyle = `rgba(0,10,24,${Math.min(1, S.faceAlpha * 1.1) * a})`; L.lineWidth = (3 + 4 * a) * k; break;
        case 3: L.strokeStyle = `rgba(255,255,255,${S.lipAlpha * Math.max(a, 0.3)})`; L.lineWidth = (1.6 + 3 * a) * k; break;
        case 4: L.strokeStyle = `rgba(242,249,255,${S.foamAlpha * a})`; L.lineWidth = (3.5 + 8 * a) * k; break;
        case 5: L.strokeStyle = `rgba(230,242,255,${S.trailAlpha * a})`; L.lineWidth = (4 + 9 * a) * k; break;
      }
      L.beginPath();
      for (let i = 0; i < b.n; i++) b.m[i] ? L.moveTo(b.x[i], b.y[i]) : L.lineTo(b.x[i], b.y[i]);
      L.stroke();
    }
}

function shoreWash(L, geo, p, t) {
  L.strokeStyle = `rgba(235,245,255,${0.18 + 0.1 * Math.sin(t * 2 * Math.PI / p.T)})`; L.lineWidth = 3;
  L.beginPath();
  for (const s of geo.shores) {                       // only where the coast is exposed and not tapered out
    let on = false;
    for (let i = 0; i < s.n; i++) {
      if (s.ex[i] > 0.4) { on ? L.lineTo(s.xs[i], s.ys[i]) : L.moveTo(s.xs[i], s.ys[i]); on = true; } else on = false;
    }
  }
  L.stroke();
}

let sprayN = 0;
function sprayEmit(x, y, p, a) {
  if (sprayN >= spray.x.length || Math.random() > STYLE.sprayRate * a) return;
  const sp = 8 + Math.random() * 10;
  spray.x[sprayN] = x; spray.y[sprayN] = y; spray.vx[sprayN] = p.wx * sp; spray.vy[sprayN] = p.wy * sp; spray.life[sprayN] = 1; sprayN++;
}
function sprayStep(L, dt) {
  let w = 0;
  L.fillStyle = "#fff";
  for (let i = 0; i < sprayN; i++) {
    const life = spray.life[i] - dt * 1.4;
    if (life <= 0) continue;
    spray.x[w] = spray.x[i] + spray.vx[i] * dt; spray.y[w] = spray.y[i] + spray.vy[i] * dt;
    spray.vx[w] = spray.vx[i]; spray.vy[w] = spray.vy[i]; spray.life[w] = life;
    L.globalAlpha = 0.5 * life; L.fillRect(spray.x[w], spray.y[w], 2, 2);
    w++;
  }
  L.globalAlpha = 1;
  sprayN = w;
}

function whitecaps(L, W, H, p, t) {
  const n = Math.round((p.windMph - 12) * 6);
  L.fillStyle = "#fff";
  for (let k = 0; k < n; k++) {
    const a = 0.5 + 0.5 * Math.sin(t * 3 + k);
    if (a <= 0.75) continue;
    const x = ((Math.sin(k * 12.9898) * 43758.5453) % 1 + 1) % 1 * W, y = ((Math.sin(k * 78.233) * 12345.678) % 1 + 1) % 1 * H;
    L.globalAlpha = (a - 0.75) * 1.6; L.fillRect(x, y, 3, 1.5);
  }
  L.globalAlpha = 1;
}
