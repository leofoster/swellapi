/**
 * livewave.js — stylised, forecast-driven wave scene for any coastal coordinate.
 *
 * Framework-free ES module for canvas 2D. Three steps:
 *
 *   const geo    = await buildGeo({ lat, lon, width, height });        // once per spot + box size
 *   const params = paramsFromHour(hour, spot, geo);                    // when the hour changes
 *   draw(ctx, width, height, geo, params, tSeconds);                   // every frame
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
 * Dependencies (runtime, from jsDelivr; or npm-install the same versions):
 *   @mapbox/vector-tile@2.0.3, pbf@4.0.1
 * Attribution to display: see ATTRIBUTION.
 */

export const ATTRIBUTION =
  "Imagery © Esri, Maxar, Earthstar Geographics · Coastline © OpenStreetMap contributors, © OpenMapTiles";
export const CAPTION = "Illustration drawn from the forecast — not a prediction of exactly where waves break";

export const DEFAULTS = {
  zoom: 16,                         // 15-17; imagery is sharp to 18, vector water tops out at 14 (scaled up)
  sampleSpacing: 6,                 // px between shoreline samples (8 on phones)
  maxShorePoints: 700,              // cap, nearest the pin first (400 on phones)
  shoal: 0.45,                      // nearshore wavelength = shoal * 1.56 * T^2 (deep water)
  setLen: 7,                        // waves per set
  seaDark: 0.62, landDark: 0.30, nightExtra: 0.25,
  tideRangeM: 1.0,                  // used when the day's tide range is unknown
  secondary: true,                  // draw the second swell (offshore lines only)
  spray: 160,                       // spray particle pool size (0 disables)
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
  if (!geo.shores.length) syntheticBeach(geo);
  return geo;
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
  return { xs, ys, nx, ny, n, peel: new Float32Array(n) };
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
    T: clamp(hour.swell_period_s ?? 10, 4, 22),
    dir: hour.swell_direction_deg ?? 270,                       // degrees the swell comes FROM
    h2: hour.secondary_swell_height_m ?? 0,
    T2: hour.secondary_swell_period_s ?? 0,
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
  p.wind = p.windMph <= 5 ? "glassy" : along > 0.25 ? "offshore" : along < -0.25 ? "onshore" : "cross-shore";
  // synthetic beach: turn it so it faces the swell
  if (geo.status === "synthetic") { p.tx = 0; p.ty = 1; }
  for (const s of geo.shores)
    for (let i = 0; i < s.n; i++) s.peel[i] = ((s.xs[i] - geo.pin.x) * p.tx + (s.ys[i] - geo.pin.y) * p.ty) * p.brk.peel;
  p._geo = geo;
  return p;
}

// scratch (module-level, grown once): no per-frame allocation
let bufX = new Float32Array(1024), bufY = new Float32Array(1024);
const spray = { x: new Float32Array(512), y: new Float32Array(512), vx: new Float32Array(512), vy: new Float32Array(512), life: new Float32Array(512) };
let lastT = null;

/** Paint one frame. t = seconds (monotonic). Cover-fits geo into w x h. */
export function draw(ctx, w, h, geo, params, t) {
  const o = geo.opts, W = geo.width, H = geo.height, p = params;
  if (p._geo !== geo) prepare(p, geo);
  const k = Math.max(w / W, h / H);
  ctx.save();
  ctx.translate((w - W * k) / 2, (h - H * k) / 2); ctx.scale(k, k);

  // 1. graded backdrop: everything a bit darker, sea much darker
  ctx.fillStyle = "#05080d"; ctx.fillRect(0, 0, W, H);
  if (geo.image) ctx.drawImage(geo.image, 0, 0);
  if (geo.status === "synthetic") { ctx.fillStyle = "#3a3226"; ctx.fillRect(0, H * 0.72, W, H * 0.28); }  // plain sand
  ctx.fillStyle = `rgba(0,0,0,${o.landDark + (p.daylight ? 0 : o.nightExtra)})`; ctx.fillRect(0, 0, W, H);
  if (geo.ocean) { ctx.fillStyle = `rgba(2,10,22,${o.seaDark})`; ctx.fill(geo.ocean, "nonzero"); }

  // 2. waves, clipped to the sea
  if (geo.ocean) {
    ctx.save(); ctx.clip(geo.ocean, "nonzero"); ctx.lineCap = "round"; ctx.lineJoin = "round";
    const dt = lastT == null ? 0 : Math.min(0.05, Math.max(0, t - lastT)); lastT = t;
    if (o.secondary && p.h2 > 0.2 && p.T2 > 0 && !p.blocked) train(ctx, geo, p, p.h2 * 1.2, p.T2, t, 0.5, true);
    train(ctx, geo, p, p.blocked ? p.hb * 0.15 : p.hb, p.T, t, 1, false);
    shoreWash(ctx, geo, p, t);
    if (p.wind === "offshore") sprayStep(ctx, dt);
    if (p.wind === "onshore" && p.windMph > 12) whitecaps(ctx, W, H, p, t);
    ctx.restore();
  }
  ctx.restore();
}

// One swell train. offshoreOnly: secondary swell = unbroken lines only (no lip/foam), so it
// adds texture without competing with the dominant swell's breaking line.
function train(ctx, geo, p, hb, T, t, weight, offshoreOnly) {
  const mpp = geo.mpp, b = p.brk;
  const lam = clamp(1.56 * T * T * geo.opts.shoal / mpp, 14, 220);       // px between crests
  const v = lam / T;                                                      // one crest per period
  const depth = Math.max(0.3, hb / 0.78 - p.tideM);                       // breaking depth, tide-adjusted
  const bd = clamp(depth / b.slope / mpp, 5, 0.3 * Math.min(geo.width, geo.height));
  const amp = clamp(hb / 2.5, 0.15, 1) * weight;
  const chop = p.wind === "onshore" ? clamp((p.windMph - 8) / 20, 0, 1) : 0;
  const travel = t * v, base = Math.floor(travel / lam), frac = travel - base * lam;
  const far = bd + 4 * lam;

  for (const s of geo.shores) {
    if (bufX.length < s.n) { bufX = new Float32Array(s.n * 2); bufY = new Float32Array(s.n * 2); }
    let pmin = Infinity, pmax = -Infinity;
    for (let i = 0; i < s.n; i++) { const q = s.peel[i]; if (q < pmin) pmin = q; if (q > pmax) pmax = q; }
    const j0 = Math.floor(-pmax / lam) - 1, j1 = Math.ceil((far - pmin) / lam) + 1;
    for (let j = j0; j <= j1; j++) {
      const D = j * lam - frac;
      const set = 0.55 + 0.45 * (0.5 + 0.5 * Math.sin((j + base) * 2 * Math.PI / geo.opts.setLen));
      const a = amp * set;
      let L = 0; while (L < LEVELS.length - 1 && LEVELS[L] * geo.opts.sampleSpacing < Math.abs(D) * 0.5) L++;
      const NX = s.nx[L], NY = s.ny[L];
      let kind = 0, n = 0, fadeQ = -1, nxr = 0, nyr = 0;           // kind: 1 swell, 2 lip, 3 foam
      for (let i = 0; i < s.n; i++) {
        let d = D + s.peel[i];
        if (chop && d > bd) d += Math.sin(i * 0.9 + t * 2.7 + j) * chop * 2.5;
        let kk = 0, fade = 1;
        if (d >= 0 && d <= far) {
          if (d > bd) { kk = 1; fade = clamp(1 - (d - bd) / (far - bd), 0, 1); }
          else if (offshoreOnly) kk = 0;
          else if (d > bd * (1 - 0.25)) kk = 2;
          else { kk = 3; fade = clamp(d / (bd * b.foam), 0.15, 1); }
        }
        const fq = Math.round(fade * 4);
        if (kk !== kind || fq !== fadeQ) {
          if (n > 1) strokeRun(ctx, kind, n, a * fadeQ / 4, nxr, nyr);
          n = 0; kind = kk; fadeQ = fq; nxr = NX[i]; nyr = NY[i];
          if (n === 0 && i > 0 && kk) {                              // join to the previous sample: no gaps
            const pd = Math.max(0, D + s.peel[i - 1]);
            bufX[n] = s.xs[i - 1] + NX[i - 1] * pd; bufY[n] = s.ys[i - 1] + NY[i - 1] * pd; n++;
          }
        }
        if (!kk) continue;
        const x = s.xs[i] + NX[i] * d, y = s.ys[i] + NY[i] * d;
        bufX[n] = x; bufY[n] = y; n++;
        if (kk === 2 && p.wind === "offshore" && (i & 3) === 0) sprayEmit(x, y, p, a);
      }
      if (n > 1) strokeRun(ctx, kind, n, a * fadeQ / 4, nxr, nyr);
    }
  }
}

function path(ctx, n, dx, dy) {
  ctx.beginPath(); ctx.moveTo(bufX[0] + dx, bufY[0] + dy);
  for (let i = 1; i < n; i++) ctx.lineTo(bufX[i] + dx, bufY[i] + dy);
}

// Contrast recipe: a dark face on the shoreward side under every crest, then the light line.
function strokeRun(ctx, kind, n, a, nx, ny) {
  if (a <= 0.01) return;
  if (kind === 1) {                                   // unbroken swell
    path(ctx, n, -nx * 2.5, -ny * 2.5);
    ctx.strokeStyle = `rgba(0,6,16,${0.55 * a})`; ctx.lineWidth = 2.2 + 3 * a; ctx.stroke();
    path(ctx, n, 0, 0);
    ctx.strokeStyle = `rgba(170,210,240,${0.6 * a})`; ctx.lineWidth = 0.8 + 1.4 * a; ctx.stroke();
  } else if (kind === 2) {                            // breaking lip
    path(ctx, n, -nx * 3, -ny * 3);
    ctx.strokeStyle = `rgba(0,10,24,${0.6 * a})`; ctx.lineWidth = 3 + 4 * a; ctx.stroke();
    path(ctx, n, 0, 0);
    ctx.strokeStyle = `rgba(255,255,255,${0.95 * Math.max(a, 0.3)})`; ctx.lineWidth = 1.6 + 3 * a; ctx.stroke();
  } else if (kind === 3) {                            // whitewater: bore + trailing foam seaward
    path(ctx, n, 0, 0);
    ctx.strokeStyle = `rgba(242,249,255,${0.7 * a})`; ctx.lineWidth = 3.5 + 8 * a; ctx.stroke();
    path(ctx, n, nx * 5, ny * 5);
    ctx.strokeStyle = `rgba(230,242,255,${0.22 * a})`; ctx.lineWidth = 4 + 9 * a; ctx.stroke();
  }
}

function shoreWash(ctx, geo, p, t) {
  ctx.strokeStyle = `rgba(235,245,255,${0.18 + 0.1 * Math.sin(t * 2 * Math.PI / p.T)})`; ctx.lineWidth = 3;
  for (const s of geo.shores) {
    ctx.beginPath(); ctx.moveTo(s.xs[0], s.ys[0]);
    for (let i = 1; i < s.n; i++) ctx.lineTo(s.xs[i], s.ys[i]);
    ctx.stroke();
  }
}

let sprayN = 0;
function sprayEmit(x, y, p, a) {
  if (sprayN >= Math.min(spray.x.length, DEFAULTS.spray) || Math.random() > 0.25 * a) return;
  const sp = 8 + Math.random() * 10;
  spray.x[sprayN] = x; spray.y[sprayN] = y; spray.vx[sprayN] = p.wx * sp; spray.vy[sprayN] = p.wy * sp; spray.life[sprayN] = 1; sprayN++;
}
function sprayStep(ctx, dt) {
  let w = 0;
  for (let i = 0; i < sprayN; i++) {
    const life = spray.life[i] - dt * 1.4;
    if (life <= 0) continue;
    spray.x[w] = spray.x[i] + spray.vx[i] * dt; spray.y[w] = spray.y[i] + spray.vy[i] * dt;
    spray.vx[w] = spray.vx[i]; spray.vy[w] = spray.vy[i]; spray.life[w] = life;
    ctx.fillStyle = `rgba(255,255,255,${0.5 * life})`; ctx.fillRect(spray.x[w], spray.y[w], 2, 2);
    w++;
  }
  sprayN = w;
}

function whitecaps(ctx, W, H, p, t) {
  const n = Math.round((p.windMph - 12) * 6);
  for (let k = 0; k < n; k++) {
    const x = ((Math.sin(k * 12.9898) * 43758.5453) % 1 + 1) % 1 * W, y = ((Math.sin(k * 78.233) * 12345.678) % 1 + 1) % 1 * H;
    const a = 0.5 + 0.5 * Math.sin(t * 3 + k);
    if (a > 0.75) { ctx.fillStyle = `rgba(255,255,255,${(a - 0.75) * 1.6})`; ctx.fillRect(x, y, 3, 1.5); }
  }
}
