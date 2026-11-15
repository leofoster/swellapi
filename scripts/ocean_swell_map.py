"""
Animated Ocean Swell Map
────────────────────────
Shows 4 frames at 6-hour intervals (–18h → –12h → –6h → now)
so you can watch swell systems move across the ocean.
"""

import requests
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import matplotlib.animation as animation
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

# ─────────────────────────────────────────────────────────────────
#  OCEAN DEFINITIONS
# ─────────────────────────────────────────────────────────────────

OCEANS = {
    "north_atlantic": {
        "label":   "North Atlantic Swell",
        "lat_min": 35,  "lat_max": 65,
        "lon_min": -50, "lon_max": 20,
        "grid":    (14, 12),
    },
    "north_pacific": {
        "label":   "North Pacific Swell",
        "lat_min": 15,  "lat_max": 65,
        "lon_min": 120, "lon_max": 240,
        "grid":    (16, 12),
    },
    "uk_ireland": {
        "label":   "UK & Ireland Swell",
        "lat_min": 48,  "lat_max": 62,
        "lon_min": -15, "lon_max": 4,
        "grid":    (10, 8),
    },
}

# ─────────────────────────────────────────────────────────────────
#  FRAME DEFINITIONS
#  4 frames stepping back in 6h increments from now.
#  Label is shown in the animation title.
# ─────────────────────────────────────────────────────────────────

FRAME_OFFSETS_H = [-18, -16, -14, -13, -12, -11, -10, -9, -8, -7, -6, -5, -4, -3, -2, -1, 0]   # hours relative to now


# ─────────────────────────────────────────────────────────────────
#  DATA FETCHING  —  hourly, past 24h
# ─────────────────────────────────────────────────────────────────

def fetch_point_hourly(lat: float, lon: float) -> dict | None:
    """
    Fetch the last 24 hours of hourly swell data at one grid point.
    Returns a dict keyed by ISO hour string → {height, dir, period}.
    """
    try:
        r = requests.get(
            "https://marine-api.open-meteo.com/v1/marine",
            params={
                "latitude":      lat,
                "longitude":     lon,
                "hourly":        [
                    "swell_wave_height",
                    "swell_wave_direction",
                    "swell_wave_period",
                ],
                "past_hours":    24,
                "forecast_days": 1,
                "timezone":      "UTC",
            },
            timeout=10,
        )
        r.raise_for_status()
        h = r.json().get("hourly", {})

        times   = h.get("time", [])
        heights = h.get("swell_wave_height", [])
        dirs    = h.get("swell_wave_direction", [])
        periods = h.get("swell_wave_period", [])

        readings = {}
        for i, t in enumerate(times):
            readings[t] = {
                "height": heights[i] if i < len(heights) else None,
                "dir":    dirs[i]    if i < len(dirs)    else None,
                "period": periods[i] if i < len(periods) else None,
            }

        return {"lat": lat, "lon": lon, "readings": readings}

    except Exception as e:
        return None


def fetch_grid_hourly(lats: np.ndarray, lons: np.ndarray,
                      max_workers: int = 12) -> list[dict]:
    tasks   = [(lat, lon) for lat in lats for lon in lons]
    results = []

    print(f"  Fetching {len(tasks)} grid points with 24h history "
          f"({max_workers} concurrent)...")

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_point_hourly, lat, lon): (lat, lon)
                   for lat, lon in tasks}

        done = 0
        for future in as_completed(futures):
            done += 1
            if done % 10 == 0 or done == len(tasks):
                print(f"    {done}/{len(tasks)}", end="\r")
            result = future.result()
            if result:
                results.append(result)

    print(f"\n  Got {len(results)}/{len(tasks)} grid points")
    return results


# ─────────────────────────────────────────────────────────────────
#  GRID BUILDING
# ─────────────────────────────────────────────────────────────────

def dir_to_uv(direction_from: float) -> tuple[float, float]:
    theta = np.radians((direction_from + 180) % 360)
    u = np.sin(theta)
    v = np.cos(theta)
    return -v, u
    #return np.sin(theta), np.cos(theta)


def fill_nan(arr: np.ndarray) -> np.ndarray:
    """Fill NaN gaps using nearest neighbours so streamplot doesn't break."""
    try:
        from scipy.ndimage import generic_filter
        def _fill(vals):
            c = vals[len(vals) // 2]
            if not np.isnan(c):
                return c
            valid = vals[~np.isnan(vals)]
            return float(valid.mean()) if len(valid) else 0.0
        return generic_filter(arr, _fill, size=3, mode="nearest")
    except ImportError:
        return np.nan_to_num(arr)


def build_grid_for_time(
    data:     list[dict],
    target_t: str,              # ISO string e.g. "2025-05-17T12:00"
    lats:     np.ndarray,
    lons:     np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Build U, V, H 2D arrays for a specific hour.
    Tries the exact hour first, then the nearest available hour
    within ±1h as a fallback.
    """
    lat_steps = len(lats)
    lon_steps = len(lons)

    U = np.full((lat_steps, lon_steps), np.nan)
    V = np.full((lat_steps, lon_steps), np.nan)
    H = np.full((lat_steps, lon_steps), np.nan)

    lat_idx = {round(lat, 4): i for i, lat in enumerate(lats)}
    lon_idx = {round(lon, 4): i for i, lon in enumerate(lons)}

    target_dt  = datetime.fromisoformat(target_t).replace(tzinfo=timezone.utc)
    # Build ±1h fallback keys to try if exact match is missing
    fallback_keys = [
        target_t,
        (target_dt - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
        (target_dt + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"),
    ]

    for pt in data:
        li = lat_idx.get(round(pt["lat"], 4))
        lj = lon_idx.get(round(pt["lon"], 4))
        if li is None or lj is None:
            continue

        reading = None
        for key in fallback_keys:
            reading = pt["readings"].get(key)
            if reading and reading["height"] is not None:
                break

        if not reading:
            continue

        if reading["dir"] is not None:
            U[li, lj], V[li, lj] = dir_to_uv(reading["dir"])
        if reading["height"] is not None:
            H[li, lj] = reading["height"]

    return fill_nan(U), fill_nan(V), fill_nan(H)


# ─────────────────────────────────────────────────────────────────
#  ANIMATED MAP
# ─────────────────────────────────────────────────────────────────

def draw_animated_swell_map(
    ocean_key:    str   = "north_atlantic",
    figsize:      tuple = (14, 8),
    cmap_name:    str   = "Blues",
    dark_mode:    bool  = True,
    interval_ms:  int   = 1200,     # ms per frame
    repeat_delay: int   = 2000,     # ms pause before looping
    save_path:    str | None = None,  # e.g. "swell.gif" or "swell.mp4"
) -> None:

    cfg = OCEANS[ocean_key]
    lon_steps, lat_steps = cfg["grid"]
    lats = np.linspace(cfg["lat_min"], cfg["lat_max"], lat_steps)
    lons = np.linspace(cfg["lon_min"], cfg["lon_max"], lon_steps)

    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    # ── 1. Fetch all hourly data once ─────────────────────────────
    raw_data = fetch_grid_hourly(lats, lons)

    # ── 2. Build a grid for each of the 4 frame timestamps ────────
    frames = []
    for offset_h in FRAME_OFFSETS_H:
        frame_dt  = now + timedelta(hours=offset_h)
        frame_key = frame_dt.strftime("%Y-%m-%dT%H:%M")

        if offset_h == 0:
            label = f"Now  —  {frame_dt.strftime('%d %b %Y  %H:%M UTC')}"
        else:
            label = (f"{abs(offset_h)}h ago  —  "
                     f"{frame_dt.strftime('%d %b  %H:%M UTC')}")

        print(f"  Building grid for {label}...")
        U, V, H = build_grid_for_time(raw_data, frame_key, lats, lons)
        frames.append({"U": U, "V": V, "H": H, "label": label})

    print(f"  All grids ready — drawing animation...\n")

    # ── 3. Figure setup ───────────────────────────────────────────
    bg  = "#0d1117" if dark_mode else "#f5f5f0"
    fg  = "#e0e0e0" if dark_mode else "#1a1a1a"
    sub = "#555555" if dark_mode else "#999999"

    fig, ax = plt.subplots(figsize=figsize, facecolor=bg)
    ax.set_facecolor(bg)

    # Shared colour scale across ALL frames so colour is comparable
    all_H    = np.stack([f["H"] for f in frames])
    h_global_min = float(np.nanmin(all_H))
    h_global_max = float(np.nanmax(all_H))
    norm = mcolors.Normalize(vmin=h_global_min, vmax=h_global_max)

    # Colourbar — draw once, never redrawn
    sm   = plt.cm.ScalarMappable(cmap=cmap_name, norm=norm)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, pad=0.02, fraction=0.025)
    cbar.set_label("Swell height (m)", color=fg, fontsize=9)
    cbar.ax.yaxis.set_tick_params(color=fg)
    plt.setp(cbar.ax.yaxis.get_ticklabels(), color=fg)
    cbar.outline.set_edgecolor(sub)

    # Static labels
    ax.set_xlabel("Longitude", color=fg, fontsize=8)
    ax.set_ylabel("Latitude",  color=fg, fontsize=8)
    ax.tick_params(colors=fg, labelsize=7)
    for spine in ax.spines.values():
        spine.set_edgecolor(sub)

    source_text = ax.text(
        0.01, 0.01, "Open-Meteo",
        transform=ax.transAxes,
        color=sub, fontsize=7, va="bottom"
    )

    # Title — updated each frame
    title = ax.set_title("", color=fg, fontsize=13, fontweight="bold", pad=12)

    # ── 4. Frame progress dots  ───────────────────────────────────
    # Small dots along the bottom showing which frame is active
    n_frames = len(frames)
    dot_texts = []
    for i in range(n_frames):
        dot = ax.text(
            0.5 + (i - n_frames / 2 + 0.5) * 0.04,
            -0.07, "●",
            transform=ax.transAxes,
            color=sub, fontsize=10, ha="center", va="bottom"
        )
        dot_texts.append(dot)

    # ── 5. Animation update function ─────────────────────────────
    def update(frame_idx: int):
        # Clear only the streamplot artists, not the whole axes
        # (avoids flickering ticks/labels)
        for coll in ax.collections[:]:
            coll.remove()
        for patch in ax.patches[:]:
            patch.remove()

        f = frames[frame_idx]
        H = f["H"]

        # Line width: thin for small swell, thick for large
        h_norm_lw = (H - h_global_min) / max(h_global_max - h_global_min, 0.1)
        linewidth  = 0.5 + h_norm_lw * 2.5

        ax.streamplot(
            lons, lats, f["U"], f["V"],
            color=H,
            cmap=cmap_name,
            norm=norm,
            linewidth=linewidth,
            density=1.4,
            arrowsize=0.0,
            arrowstyle="->",
            broken_streamlines=False,
        )

        # Update title with current frame label
        title.set_text(f"{cfg['label']}  ·  {f['label']}")

        # Update progress dots — active dot is bright
        for i, dot in enumerate(dot_texts):
            dot.set_color(fg if i == frame_idx else sub)
            dot.set_fontsize(12 if i == frame_idx else 9)

        return []

    # ── 6. Run ────────────────────────────────────────────────────
    anim = animation.FuncAnimation(
        fig,
        update,
        frames=n_frames,
        interval=interval_ms,
        repeat=True,
        repeat_delay=repeat_delay,
        blit=False,         # streamplot doesn't support blit
    )

    plt.tight_layout()

    if save_path:
        print(f"  Saving animation → {save_path}  (this may take a moment...)")
        if save_path.endswith(".gif"):
            writer = animation.PillowWriter(fps=1000 // interval_ms)
        else:
            writer = animation.FFMpegWriter(fps=1000 // interval_ms,
                                            bitrate=1800)
        anim.save(save_path, writer=writer,
                  savefig_kwargs={"facecolor": bg})
        print(f"  Saved ✓")
    else:
        plt.show()


# ─────────────────────────────────────────────────────────────────
#  RUN
# ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    draw_animated_swell_map(
        ocean_key    = "north_atlantic",
        dark_mode    = True,
        cmap_name    = "rainbow",
        interval_ms  = 100,    # speed between frames
        # save_path  = "swell.gif",   # uncomment to export
    )
