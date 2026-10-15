"""RAYCUE 480x320 CephaloBot dense live dashboard.

render_to_bytes(state) -> JPEG bytes for in-memory serving over HTTP.
"""
from __future__ import annotations

import datetime as _dt
import io
import pathlib as _pl

try:
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo("America/Los_Angeles")
except Exception:
    _LOCAL_TZ = None  # fallback: UTC (container without tzdata)

from PIL import Image, ImageDraw, ImageFont

W, H = 480, 320
PAD = 22  # bezel-safe inset; everything we draw stays within [L,R]×[T,B]
L, R, T, B = PAD, W - PAD, PAD, H - PAD

BG = (10, 12, 18)
FG = (235, 235, 235)
DIM = (130, 140, 155)
DIM2 = (90, 100, 115)
OK = (90, 200, 150)
WARN = (240, 180, 70)
BAD = (235, 90, 90)
ACCENT = (95, 165, 230)
RULE = (35, 40, 50)

# XP bar — MMORPG palette
# Used = warm (gold→orange→red as % climbs)
# Free = cool teal "mana" — always visible distinct from background
XP_FRAME = (60, 70, 90)    # bar outer frame
XP_USED_OK   = (255, 210, 40)  # gold (healthy)
XP_USED_WARN = (255, 140, 30)  # orange (>70%)
XP_USED_CRIT = (220, 50, 50)   # red   (>90%)
XP_FREE      = (40, 160, 180)  # cool teal — available capacity
XP_DIV       = (235, 245, 255) # bright divider between used/free
XP_GLOW      = (255, 235, 100) # shimmer strip on used portion

# TURN bar — dual-cap asymmetry palette (P3 — session window turns)
# Neutral (BALANCED): muted blue
# TURN_LIMITED (turns draining faster): orange warning
# TOK_LIMITED (tokens draining faster): cool cyan (informational only)
TURN_BALANCED    = (80, 120, 200)   # muted blue
TURN_TURN_LIM    = (255, 140, 30)   # orange — action needed
TURN_TOK_LIM     = (40, 180, 200)   # cyan — informational
TURN_FREE        = (35, 50, 70)     # dark bg for unused portion

WHEEL = ["◐", "◓", "◑", "◒"]

SHORTNAMES = {
    "openclaw-mcp-controller": "mcp-ctrl",
    "openclaw-mcp-registry": "mcp-reg",
    "openclaw-mcp-builder": "mcp-bld",
    "clawgig-webhook": "cwg-webhook",
    "clawgig-hunter": "cwg-hunter",
    "big-bear-pihole-unbound": "pihole",
    "ai-ollama-a2000": "ollama-a2000",
    "ai-ollama-6900xt": "ollama-6900xt",
    "ai-ollama-cpu": "ollama-cpu",
    "ollama-6900xt": "ollama-6900",
    "claude-queue-drain": "drain",
    "cloudflared": "cflare",
    "clawedgig-bot": "cwg-bot",
    "teach-worker": "teach",
    "mcp-controller": "ctrl",
    "mcp-registry": "reg",
    "mcp-builder": "bld",
    "watchdog-bot": "wdog",
    "kraken-bot": "krak",
    "router-health-watcher": "r-hlth",
    "router-mode-server": "r-mode",
    "state-freshness-watchdog": "fresh",
    "timeout-watcher": "tmout",
    "tesseract-bridge": "ts-brg",
    "tesseract-router": "ts-rtr",
    "service-control-server": "svc-ctl",
    "loki-cors": "l-cors",
    "promtail": "ptail",
}

CONTAINER_COLORS = {
    "vault":                   (235, 110, 110),
    "redis":                   (220,  80, 130),
    "ollama":                  ( 90, 200, 150),
    "ollama-6900xt":           (140, 220,  90),
    "ollama-cpu":              (200, 230, 120),
    "openclaw-mcp-controller": ( 95, 165, 230),
    "openclaw-mcp-builder":    (140, 190, 250),
    "openclaw-mcp-registry":   (180, 215, 250),
    "clawgig-webhook":         (240, 170,  80),
    "clawgig-hunter":          (220, 130,  60),
    "big-bear-pihole-unbound": (190, 120, 230),
}
_FALLBACK_PALETTE = [
    (255, 220, 100), (110, 220, 220), (250, 140, 200),
    (180, 180, 220), (220, 220, 180), (140, 140, 220),
]

# Linux font paths (DejaVu) with macOS fallbacks for dev convenience
_FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/SFNSMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
)
# Bold variant — much crisper at small sizes (9-10px)
_FONT_PATHS_BOLD = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/SFNSMono.ttf",
    "/System/Library/Fonts/Menlo.ttc",
)

# Sparkline glyphs (1/8 → 8/8 vertical block fill) — micro-bar in a single char
SPARK = "▁▂▃▄▅▆▇█"


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    paths = _FONT_PATHS_BOLD if bold else _FONT_PATHS
    for path in paths:
        if _pl.Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _spark(frac: float) -> str:
    """Return a single block char representing a 0..1 value."""
    if frac <= 0:
        return " "
    idx = min(7, max(0, int(frac * 8)))
    return SPARK[idx]


def _short(name: str) -> str:
    # Try full name first. Then try stripping common prefixes.
    # Prefixes are tried in order of specificity (longest first).
    if name in SHORTNAMES:
        return SHORTNAMES[name]
    for prefix in ("openclaw-ai-", "openclaw-"):
        if name.startswith(prefix):
            stripped = name[len(prefix):]
            if stripped in SHORTNAMES:
                return SHORTNAMES[stripped]
            # Even if no shortname exists for the stripped form, return it.
            # Better to show "ollama-a2000" than full "openclaw-ai-ollama-a2000".
            return stripped
    return name


def _fit_name(d, name: str, max_w_px: int, min_size: int = 8,
              max_size: int = 10):
    """Return (fitted_name, font) that fits within max_w_px.

    Tries font sizes max_size..min_size (bold); if none fit, tail-truncates
    at min_size with an ellipsis so the label stays inside its slot instead
    of overlapping the CPU/MEM/SHR columns.
    """
    for size in range(max_size, min_size - 1, -1):
        f = _font(size, bold=True)
        w = d.textbbox((0, 0), name, font=f)[2]
        if w <= max_w_px:
            return name, f
    # Doesn't fit even at min size — chop tail + ellipsis until it fits
    f = _font(min_size, bold=True)
    truncated = name
    while truncated and d.textbbox((0, 0), truncated + "…", font=f)[2] > max_w_px:
        truncated = truncated[:-1]
    return (truncated + "…") if truncated else "?", f


def _color_for(name: str) -> tuple:
    if name in CONTAINER_COLORS:
        return CONTAINER_COLORS[name]
    return _FALLBACK_PALETTE[sum(ord(c) for c in name) % len(_FALLBACK_PALETTE)]


def _tier_color(s: str, shp_color: str = None) -> tuple:
    # Prefer SHP semantic color (green/yellow/red = operational/probing/exhausted)
    # Fall back to circuit breaker state (closed/half_open/open)
    if shp_color:
        return {"green": OK, "yellow": WARN, "red": BAD}.get(shp_color, DIM2)
    return {"closed": OK, "half_open": WARN, "open": BAD}.get(s, DIM2)


def _tier_state_known(s: str, shp_color: str = None) -> bool:
    """True when the tier has a definitive state (green/yellow/red or CB state).
    False for unseen tiers (like T3 Sonnet cloud that isn't smoke-tested) —
    the dashboard should render these as hollow outlines rather than filled
    dots so they're not confused with a real red/BAD state."""
    if shp_color in ("green", "yellow", "red"):
        return True
    return s in ("closed", "half_open", "open")


def _service_color(s: str) -> tuple:
    return {"active": OK, "activating": WARN, "failed": BAD, "inactive": BAD}.get(s, DIM2)


def _hbar(d, x, y, w, h, frac, color):
    d.rectangle([x, y, x + w, y + h], fill=(30, 35, 45))
    fw = max(0, min(w, int(w * frac)))
    if fw > 0:
        d.rectangle([x, y, x + fw, y + h], fill=color)


def _ring(d, cx, cy, r_outer, r_inner, slices, bg=BG):
    total = sum(f for f, _ in slices)
    if total <= 0:
        d.ellipse([cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer], outline=DIM2, width=2)
        return
    start = -90.0
    for frac, color in slices:
        if frac <= 0:
            continue
        sweep = 360.0 * (frac / total)
        d.pieslice(
            [cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer],
            start, start + sweep, fill=color,
        )
        start += sweep
    d.ellipse([cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner], fill=bg)


def _xp_bar(d, x: int, y: int, w: int, h: int, frac: float,
            used_label: str, free_label: str, reset_label: str,
            font) -> None:
    """MMORPG-style XP bar — two segments:
       [warm USED fill | cool FREE fill] inside a frame, with bright divider.
       Above the bar: USED label (left) and FREE label + reset (right).
    """
    frac = max(0.0, min(1.0, frac))
    used_w = max(0, min(w, int(w * frac)))
    free_w = w - used_w

    # Pick used color based on usage
    used_color = XP_USED_OK if frac < 0.7 else (XP_USED_WARN if frac < 0.9 else XP_USED_CRIT)

    # Bar body — fill both segments end-to-end
    if used_w > 0:
        d.rectangle([x, y, x + used_w, y + h], fill=used_color)
        # Shimmer line on used portion
        if used_w > 3:
            d.line([(x + 1, y + 1), (x + used_w - 1, y + 1)], fill=XP_GLOW, width=1)
    if free_w > 0:
        d.rectangle([x + used_w, y, x + w, y + h], fill=XP_FREE)

    # Bright vertical divider at the boundary (only if both segments have width)
    if 0 < used_w < w:
        d.line([(x + used_w, y), (x + used_w, y + h)], fill=XP_DIV, width=1)

    # Outer frame (subtle)
    d.rectangle([x, y, x + w, y + h], outline=XP_FRAME)

    # Labels — drawn ABOVE the bar (caller leaves room)
    # Left: used_label in warm color, Right: free_label + reset_label
    label_y = y - 10
    d.text((x, label_y), used_label, font=font, fill=used_color)
    right_label = f"{free_label}  {reset_label}"
    bb = d.textbbox((0, 0), right_label, font=font)
    rw = bb[2] - bb[0]
    # Free text in cool teal, reset text in dim — split visually with colors
    # Draw right-anchored: compute positions backwards
    reset_bb = d.textbbox((0, 0), reset_label, font=font)
    reset_w = reset_bb[2] - reset_bb[0]
    d.text((x + w - reset_w, label_y), reset_label, font=font, fill=DIM)
    free_x = x + w - reset_w - 6  # 6px gap
    free_bb = d.textbbox((0, 0), free_label, font=font)
    free_w_text = free_bb[2] - free_bb[0]
    d.text((free_x - free_w_text, label_y), free_label, font=font, fill=XP_FREE)


def render_to_bytes(state: dict, tick: int = 0) -> bytes:
    # Use local timezone if available; fall back to UTC (container without tzdata)
    if _LOCAL_TZ:
        now = _dt.datetime.now(_dt.timezone.utc).astimezone(_LOCAL_TZ)
    else:
        now = _dt.datetime.now(_dt.timezone.utc)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    f_h = _font(18, bold=True)
    f_lbl = _font(9, bold=True)
    f_row = _font(10, bold=True)
    f_mono = _font(10, bold=True)
    f_small = _font(9)
    f_tiny = _font(8)
    f_wheel = _font(20)
    f_clock = _font(18, bold=True)

    # Header — OPENCLAW · spinner · uptime/events · date · local-time clock
    d.text((L, T), "CEPHALOBOT", font=f_h, fill=FG)
    try:
        d.text((L + 118, T - 2), WHEEL[tick % 4], font=f_wheel, fill=ACCENT)
    except Exception:
        d.text((L + 118, T), "|/-\\"[tick % 4], font=f_h, fill=ACCENT)
    up = state.get("uptime") or "?"
    ev = state.get("events_recent", 0)
    # Backlog chip: BL pending/(pending+completed) with trend arrow
    bl = state.get("backlog") or {}
    bl_chip = ""
    if bl:
        p_cur = bl.get("pending", 0)
        c_cur = bl.get("completed", 0)
        total = p_cur + c_cur
        spark_p = bl.get("spark_pending") or []
        if len(spark_p) >= 2:
            arrow = "↓" if spark_p[-1] < spark_p[0] else ("↑" if spark_p[-1] > spark_p[0] else "→")
        else:
            arrow = "·"
        bl_chip = f" · BL {p_cur}/{total} {arrow}"
    d.text((L + 144, T + 4), f"up {up} · {ev}ev{bl_chip}", font=f_tiny, fill=DIM)
    # Date + local-time (%-I avoids zero-pad; strip leading space on Linux)
    try:
        date_str  = now.strftime("%b %-d")
        clock_str = now.strftime("%-I:%M%p").lower()
    except ValueError:
        date_str  = now.strftime("%b %d").lstrip("0")
        clock_str = now.strftime("%I:%M%p").lstrip("0").lower()
    dt_str = f"{date_str} {clock_str}"
    bbox = d.textbbox((0, 0), dt_str, font=f_clock)
    dt_w = bbox[2] - bbox[0]
    d.text((R - dt_w, T), dt_str, font=f_clock, fill=FG)
    if not state.get("ssh_ok", True):
        d.text((R - 64, T + 18), "STALE", font=f_tiny, fill=BAD)
    d.line([(L, T + 22), (R, T + 22)], fill=RULE, width=1)

    COL_L_X = L
    COL_L_W = 250
    COL_R_X = L + COL_L_W + 6
    COL_R_W = R - COL_R_X
    BODY_TOP = T + 26

    # Containers — dense rows with inline sparklines
    containers = state.get("containers", [])
    # Merge K8s pod stats into the unified container list
    for kp in state.get("k8s_pod_stats", []):
        containers.append(kp)
    # For the health counter, ignore expected_idle containers — they're not
    # actually down, just ephemeral MCP tools between smoke runs. Otherwise
    # the header would flash red every time an MCP smoke test wrapped up.
    active_containers = [c for c in containers if not c.get("expected_idle")]
    up_n = sum(1 for c in active_containers if c["status"] == "up")
    total_n = len(active_containers)
    hdr_color = BAD if up_n < total_n else OK
    d.text((COL_L_X, BODY_TOP), "CONTAINERS", font=f_lbl, fill=DIM)
    d.text((COL_L_X + 66, BODY_TOP), f"{up_n}/{total_n}", font=f_lbl, fill=hdr_color)
    # Mini column headers
    d.text((COL_L_X + 110, BODY_TOP), "CPU", font=f_tiny, fill=DIM2)
    d.text((COL_L_X + 145, BODY_TOP), "MEM", font=f_tiny, fill=DIM2)
    d.text((COL_L_X + 185, BODY_TOP), "SHR", font=f_tiny, fill=DIM2)

    y = BODY_TOP + 11
    pie_total = sum(c["mem_mb"] for c in containers if c["status"] == "up") or 1
    # Available px for the container name column: bullseye ends at COL_L_X+9,
    # text starts at +11, CPU column begins at +108. Leave 2px breathing room
    # before CPU: 108 - 11 - 2 = 95 usable px.
    NAME_MAX_W = 95
    # Dynamic row height: pack all containers between BODY_TOP+11 and the SVC
    # row below (svc_y is computed later at line ~425 as B-25-18-10-12-14=219,
    # inlined here as SVC_TOP to avoid forward-refs). Prefer row_h=11, shrink
    # to fit up to 16+ containers without overlapping the SVC row.
    SVC_TOP = B - 25 - 18 - 10 - 12 - 14  # keep in sync with bottom-anchored layout
    avail_h = SVC_TOP - y - 5  # 5px cushion above SVC (was 2 — text descenders bled into SVC row)
    n = len(containers)
    row_h = max(8, min(11, avail_h // n)) if n else 11
    # Font size ceiling scales with row_h so text has vertical breathing room
    name_size_max = 10 if row_h >= 11 else (9 if row_h >= 10 else 8)
    max_rows = min(n, avail_h // row_h)
    for c in containers[:max_rows]:
        full_name = c["name"]
        raw_name = _short(full_name)
        name, name_font = _fit_name(d, raw_name, NAME_MAX_W,
                                    max_size=name_size_max)
        ident = _color_for(full_name)
        if c["status"] == "up":
            # Bullseye dot — outer ring = container color, inner = up/down
            d.ellipse([COL_L_X, y + 2, COL_L_X + 8, y + 10], fill=ident)
            d.ellipse([COL_L_X + 2, y + 4, COL_L_X + 6, y + 8], fill=OK)
            d.text((COL_L_X + 11, y), name, font=name_font, fill=FG)
            cpu_pct = c["cpu_pct"]
            cpu_bar = _spark(min(cpu_pct / 100.0, 1.0))
            d.text((COL_L_X + 108, y), f"{cpu_pct:4.1f}{cpu_bar}", font=f_mono, fill=DIM)
            mem_mb = c["mem_mb"]
            mem_str = f"{mem_mb/1024:.1f}G" if mem_mb >= 1024 else f"{int(mem_mb)}M"
            mem_bar = _spark(c.get("mem_pct", 0) / 100.0)
            d.text((COL_L_X + 145, y), f"{mem_str.rjust(5)}{mem_bar}", font=f_mono, fill=DIM)
            share = (mem_mb / pie_total) * 100
            d.text((COL_L_X + 188, y), f"{share:4.1f}%", font=f_mono, fill=ident)
        elif c.get("expected_idle"):
            # Ephemeral MCP tool sitting in exited state after a smoke run.
            # Hollow ring + IDLE label so it's not confused with a real DOWN.
            d.ellipse([COL_L_X, y + 2, COL_L_X + 8, y + 10],
                      outline=DIM, width=1)
            d.text((COL_L_X + 11, y), name, font=name_font, fill=DIM)
            d.text((COL_L_X + 145, y), "IDLE", font=f_mono, fill=DIM)
        else:
            d.ellipse([COL_L_X, y + 2, COL_L_X + 8, y + 10], fill=ident)
            d.ellipse([COL_L_X + 2, y + 4, COL_L_X + 6, y + 8], fill=BAD)
            d.text((COL_L_X + 11, y), name, font=name_font, fill=BAD)
            d.text((COL_L_X + 145, y), "DOWN", font=f_mono, fill=BAD)
        y += row_h

    # Ring (right column) — smaller for tighter layout
    ring_cx = COL_R_X + COL_R_W // 2
    ring_cy = BODY_TOP + 30
    r_outer, r_inner = 26, 14

    total_mem = state.get("mem_total_mb", 0) or 1
    running = [c for c in containers if c["status"] == "up"]
    total_container_mem = sum(c["mem_mb"] for c in running) or 1
    slices = []
    for c in sorted(running, key=lambda x: -x["mem_mb"]):
        frac = c["mem_mb"] / total_container_mem
        if frac > 0:
            slices.append((frac, _color_for(c["name"])))
    _ring(d, ring_cx, ring_cy, r_outer, r_inner, slices)
    sys_pct = (total_container_mem / total_mem) * 100
    label = f"{sys_pct:.1f}%" if sys_pct < 10 else f"{sys_pct:.0f}%"
    bbox = d.textbbox((0, 0), label, font=f_tiny)
    d.text((ring_cx - (bbox[2] - bbox[0]) // 2, ring_cy - 4), label, font=f_tiny, fill=FG)
    d.text((COL_R_X + 6, ring_cy + r_outer + 2), "CTR MEM", font=f_tiny, fill=DIM)

    # ── Tier ladder (right col) — compact ladder w/ live model + active arrow
    router_y = ring_cy + r_outer + 14
    # Router mode indicator — legacy = linear ladder ▼, tesseract = 4D field ∷
    router_mode = "legacy"
    try:
        import json as _json
        _mode_path = "/dev/shm/cephalobot/router_mode.json"
        with open(_mode_path) as _f:
            _mode_data = _json.load(_f)
            router_mode = (_mode_data.get("mode") or _mode_data.get("active") or "legacy").lower()
    except Exception:
        pass
    is_4d = router_mode in ("4d", "tesseract")
    tesseract = state.get("tesseract") or {}
    tess_weights = tesseract.get("weights", {})
    if is_4d:
        # 4D field: tiers as a superposition/mesh — magenta accent to match k8s-4d
        tess_races = tesseract.get("total_races", 0)
        ladder_title = f"∷ TESSERACT ({tess_races}r)" if tess_races else "∷ TESSERACT"
        ladder_accent = (0xe0, 0x7b, 0xff)  # magenta
    else:
        # Legacy: strict linear ladder
        ladder_title = "▼ TIER LADDER"
        ladder_accent = DIM
    d.text((COL_R_X, router_y), ladder_title, font=f_lbl, fill=ladder_accent)
    tiers = state.get("tiers", {})
    tier_models = state.get("tier_models", {})
    routes = state.get("recent_routes") or []
    active_tier = None
    for r in reversed(routes):
        t = r.get("tier")
        if t in ("t1", "t2", "t3", "t4", "t5"):
            active_tier = t
            break
    if active_tier is None:
        le = state.get("last_event") or {}
        if le.get("tier") in ("t1", "t2", "t3", "t4", "t5"):
            active_tier = le["tier"]

    ladder_top = router_y + 11
    ladder_row_h = 10
    shp = state.get("shp", {})
    shp_services = shp.get("services", {})
    # Pre-compute per-tier hit counts + success from routes (this replaces the
    # old bottom-strip TRACE row — activity now lives inline on each ladder row)
    tier_hits = {"t1": [], "t2": [], "t3": [], "t4": [], "t5": []}
    for r in routes[-12:]:  # look at last 12 to have meaningful signal
        t = (r.get("tier") or "").lower()
        if t in tier_hits:
            tier_hits[t].append(bool(r.get("success", True)))
    for i, name in enumerate(("t1", "t2", "t3", "t4", "t5")):
        st = tiers.get(name, "?")
        # Prefer SHP color (semantic) over circuit breaker state
        tier_svc_name = f"tier-{name}-*"  # e.g. tier-t1-ollama-local
        shp_color = None
        for svc_name, svc_data in shp_services.items():
            if svc_name.startswith(f"tier-{name}-"):
                shp_color = svc_data.get("color")
                break
        color = _tier_color(st, shp_color)
        known = _tier_state_known(st, shp_color)
        ry = ladder_top + i * ladder_row_h
        if name == active_tier:
            d.text((COL_R_X - 3, ry - 1), "▸", font=f_row, fill=ACCENT)
        # Hollow outline for unknown tiers (like T3 Sonnet cloud with no
        # smoke-test coverage) so they don't get read as red/BAD.
        if known:
            d.ellipse([COL_R_X + 6, ry + 2, COL_R_X + 12, ry + 8], fill=color)
            # 4D mode: add faint halo dots to suggest the racing/superposition dynamism
            if is_4d:
                for dx in (-3, 3):
                    for dy in (-2, 2):
                        d.point([(COL_R_X + 9 + dx, ry + 5 + dy)], fill=ladder_accent)
        else:
            d.ellipse([COL_R_X + 6, ry + 2, COL_R_X + 12, ry + 8],
                      outline=DIM, width=1)
        d.text((COL_R_X + 15, ry), name.upper(), font=f_row,
               fill=FG if known else DIM)
        model_label = tier_models.get(name, "?")
        d.text((COL_R_X + 35, ry), model_label[:15], font=f_mono,
               fill=DIM if known else DIM2)
        # Inline TRACE — mini success/fail chips for this tier's last few hits.
        # Draws right-aligned in the row so the model name is not overlapped.
        hits = tier_hits.get(name, [])
        if hits:
            chip_x = R - 4 - len(hits) * 4
            chip_x = max(chip_x, COL_R_X + 130)  # don't crowd model text
            for ok in hits[-6:]:  # show up to last 6 for this tier
                c = OK if ok else BAD
                d.rectangle([chip_x, ry + 3, chip_x + 2, ry + 6], fill=c)
                chip_x += 4

    # Tesseract source weights — compact bar below tier ladder when in 4D mode
    if tess_weights:
        wy = ladder_top + 5 * ladder_row_h + 2
        total_w = sum(tess_weights.values()) or 1
        bar_x = COL_R_X
        bar_w = COL_R_W
        TESS_SRC_COLORS = {
            "ollama-a2000": (90, 200, 150), "ollama-6900xt": (140, 220, 90),
            "ollama-cpu": (200, 230, 120), "mcp-cache": (95, 165, 230),
        }
        # Stacked horizontal bar showing win-rate proportions
        for src, pct in sorted(tess_weights.items(), key=lambda x: -x[1]):
            seg_w = max(1, int(bar_w * pct / total_w))
            sc = TESS_SRC_COLORS.get(src, _FALLBACK_PALETTE[sum(ord(c) for c in src) % len(_FALLBACK_PALETTE)])
            d.rectangle([bar_x, wy, bar_x + seg_w, wy + 3], fill=sc)
            bar_x += seg_w
        # Labels below bar
        lx = COL_R_X
        for src, pct in sorted(tess_weights.items(), key=lambda x: -x[1])[:3]:
            short = src.replace("ollama-", "").replace("mcp-", "")[:6]
            lbl = f"{short} {pct}%"
            sc = TESS_SRC_COLORS.get(src, DIM)
            d.text((lx, wy + 4), lbl, font=f_tiny, fill=sc)
            lx += len(lbl) * 5 + 4
            if lx > R - 10:
                break

    # Bottom strip (bottom-up): stamp → SYS bars+labels → TOK XP bar (w/ TURN shadow) → SVC
    # TRACE row REMOVED — the per-tier hit chips now live inline on each TIER LADDER
    # row (see block above). Freed vertical space redistributed to SVC + burndown.
    # Dedicated lanes so nothing overlaps. Heights are measured.
    sys_bar_y   = B - 16             # CPU/MEM/DSK/GPU bar tops
    sys_label_y = B - 25             # CPU/MEM/DSK/GPU labels above bars
    # TOK XP bar: 9px bar + 4px TURN shadow protrusion + 10px label-above
    # TURN now renders as a 3D drop-shadow below the XP bar (no separate lane).
    xp_bar_y    = sys_label_y - 18   # TOK bar (9px tall) — shadow protrudes 4px below
    xp_label_y  = xp_bar_y - 10      # USED/FREE/reset labels above TOK bar
    # Gemini bar: compact 5px bar + 9px labels above, stacked above Claude bar
    gem_bar_y   = xp_label_y - 6     # Gemini bar top (5px tall, sits just above Claude labels)
    gem_label_y = gem_bar_y - 9      # Gemini USED/reset labels
    svc_y       = gem_label_y - 14   # services single-row (moved up now that TRACE is gone)

    # K8S dot row removed — pods shown in unified CONTAINERS list

    # Burn-down sparkline — right edge of the SVC row (was on TRACE row).
    spark_p = (state.get("backlog") or {}).get("spark_pending") or []
    if spark_p:
        lo, hi = min(spark_p), max(spark_p)
        rng = max(1, hi - lo)
        spark_str = "".join(SPARK[min(7, int((v - lo) / rng * 7))] for v in spark_p)
        trend_col = OK if spark_p[-1] < spark_p[0] else (BAD if spark_p[-1] > spark_p[0] else DIM)
        sp_bbox = d.textbbox((0, 0), spark_str, font=f_tiny)
        sp_w = sp_bbox[2] - sp_bbox[0]
        lbl = "BL"
        lbl_bbox = d.textbbox((0, 0), lbl, font=f_tiny)
        lbl_w = lbl_bbox[2] - lbl_bbox[0]
        total_w = lbl_w + 3 + sp_w
        sx = R - total_w
        d.text((sx, svc_y), lbl, font=f_tiny, fill=DIM)
        d.text((sx + lbl_w + 3, svc_y), spark_str, font=f_tiny, fill=trend_col)

    cpu_load1 = (state.get("load") or [0])[0]
    cpu_frac = min(cpu_load1 / 24.0, 1.0)
    mem_used = state.get("mem_used_mb", 0)
    mem_total = state.get("mem_total_mb", 1) or 1
    mem_frac = mem_used / mem_total
    disk_pct = state.get("disk_used_pct", 0)
    gpu_pct = state.get("gpu_util_pct", 0)

    # SYS bars — compact labels + sparkline glyphs + value
    bars = [
        ("CPU", cpu_frac, f"{cpu_load1:.1f}"),
        ("MEM", mem_frac, f"{mem_used/1024:.1f}G"),
        ("DSK", disk_pct / 100.0, f"{disk_pct:.0f}%"),
        ("GPU", gpu_pct / 100.0, f"{gpu_pct:.0f}%"),
    ]
    bar_w = (R - L - 9) // 4
    bar_h = 4
    for i, (label, frac, value) in enumerate(bars):
        x = L + i * (bar_w + 3)
        color = OK if frac < 0.7 else (WARN if frac < 0.9 else BAD)
        d.text((x, sys_label_y), label, font=f_tiny, fill=DIM)
        spark_char = _spark(frac)
        val_with_spark = f"{value}{spark_char}"
        d.text((x + 18, sys_label_y), val_with_spark, font=f_tiny, fill=FG)
        _hbar(d, x, sys_bar_y, bar_w, bar_h, frac, color)

    # ── XP bar (Claude tokens used / available this 5-hour window) ─────
    quota      = state.get("quota") or {}
    is_stale   = quota.get("quota_stale", False)
    is_missing = quota.get("quota_missing", False)

    def _fmt_tok(n):
        if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
        if n >= 1_000:     return f"{n//1000}k"
        return str(n)

    if is_stale or is_missing:
        # Visible warning bar — dark red fill, bright red border + centred label
        STALE_BAR = (70, 20, 20)    # dark red — clearly not the normal teal/gold
        STALE_BRD = (200, 50, 50)   # bright red border
        STALE_TXT = (255, 90, 90)   # readable red label text
        stale_lbl = "QUOTA MISSING" if is_missing else f"STALE {quota.get('quota_age_s', 0)//60}m ago"
        d.rectangle([L, xp_bar_y, R, xp_bar_y + 9], fill=STALE_BAR)
        d.rectangle([L, xp_bar_y, R, xp_bar_y + 9], outline=STALE_BRD)
        label_y = xp_bar_y - 10
        # Show last-known used/free faintly above bar if we still have the numbers
        if quota.get("tokens_cap_est"):
            used = max(0, quota.get("tokens_used", 0))
            cap  = max(1, quota.get("tokens_cap_est", 1))
            free = max(0, cap - used)
            d.text((L, label_y), f"USED {_fmt_tok(used)}", font=f_tiny, fill=DIM2)
            fr_bb = d.textbbox((0, 0), f"FREE {_fmt_tok(free)}", font=f_tiny)
            d.text((R - (fr_bb[2] - fr_bb[0]), label_y), f"FREE {_fmt_tok(free)}", font=f_tiny, fill=DIM2)
        stale_bb = d.textbbox((0, 0), stale_lbl, font=f_tiny)
        d.text(((L + R - (stale_bb[2] - stale_bb[0])) // 2, xp_bar_y + 1), stale_lbl, font=f_tiny, fill=STALE_TXT)
        # Also show Gemini status in stale case (data may still be valid from last push)
        gem_used = quota.get("gemini_requests_today", 0) or 0
        gem_cap  = quota.get("gemini_rpd_cap", 1000) or 1000
        gem_reset = quota.get("gemini_reset_local", "?")
        gem_lbl = f"GEMINI {gem_used}/{gem_cap}  ↺ {gem_reset}"
        d.text((L, gem_label_y), gem_lbl, font=f_tiny, fill=DIM2)
        gem_frac = min(1.0, gem_used / max(1, gem_cap))
        gem_used_w = max(0, min(R - L, int((R - L) * gem_frac)))
        d.rectangle([L, gem_bar_y, R, gem_bar_y + 5], fill=(30, 40, 55))
        if gem_used_w > 0:
            d.rectangle([L, gem_bar_y, L + gem_used_w, gem_bar_y + 5], fill=(60, 100, 130))
        d.rectangle([L, gem_bar_y, R, gem_bar_y + 5], outline=(60, 70, 85))

    elif quota.get("tokens_cap_est"):
        used = max(0, quota.get("tokens_used", 0))
        cap  = max(1, quota.get("tokens_cap_est", 1))
        free = max(0, cap - used)
        frac = used / cap
        secs_left = quota.get("window_seconds_left", 0)
        reset_lbl = quota.get("next_reset_local", "?")
        h = secs_left // 3600
        m = (secs_left % 3600) // 60
        eta = f"{h}h{m:02d}m" if h else f"{m}m"

        # Burn-rate forecast (Phase 1: advanced budgeting)
        burn_hr   = quota.get("burn_tokens_per_hr", 0)
        verdict   = quota.get("verdict", "")
        wall_time = quota.get("projected_wall_local")
        will_wall = quota.get("will_wall_before_reset", False)

        # When burning fast enough to wall before reset → swap reset for wall warning.
        # Otherwise show reset clock as normal.
        if will_wall and wall_time and verdict in ("WATCH", "HOT", "WALL"):
            reset_lbl_full = f"🔥 wall ~{wall_time}"
        else:
            reset_lbl_full = f"↺ {reset_lbl} {eta}"

        # Compute TURN state up front so we can merge into XP labels + draw shadow.
        turns_used    = quota.get("turns_used", 0)
        turns_cap     = quota.get("turns_cap_est", 0)
        turns_pct     = quota.get("turns_pct_used", 0.0)
        cap_asymmetry = quota.get("cap_asymmetry", "BALANCED")

        if cap_asymmetry == "TURN_LIMITED":
            t_color = TURN_TURN_LIM
            asym_tag = "⚡"
        elif cap_asymmetry == "TOK_LIMITED":
            t_color = TURN_TOK_LIM
            asym_tag = "◇"
        else:
            t_color = TURN_BALANCED
            asym_tag = "·"
        turn_inline = ""
        if turns_cap and turns_cap > 0:
            turn_inline = f"  {asym_tag} {int(turns_pct * 100)}% T"

        # Append burn rate + inline TURN indicator to USED label
        if burn_hr >= 100_000:
            used_lbl = f"USED {_fmt_tok(used)} @ {_fmt_tok(burn_hr)}/h{turn_inline}"
        else:
            used_lbl = f"USED {_fmt_tok(used)}{turn_inline}"

        free_lbl = f"FREE {_fmt_tok(free)}"
        _xp_bar(d, L, xp_bar_y, R - L, 9, frac,
                used_lbl, free_lbl, reset_lbl_full, f_tiny)

        # ── Prominent reset overlay when high usage (fixes hard-to-read ETA)
        # When quota is 90%+ used OR walled, overlay a bright centred label
        # "resets in 2h34m @ 9:20pm" directly on the bar so it can't be missed.
        if frac >= 0.90 or verdict in ("HOT", "WALL"):
            overlay_lbl = f"◉ resets in {eta} @ {reset_lbl}"
            ov_bb = d.textbbox((0, 0), overlay_lbl, font=f_tiny)
            ov_w = ov_bb[2] - ov_bb[0]
            ov_x = (L + R - ov_w) // 2
            # Bright white text with subtle shadow for legibility over red/warn bar
            d.text((ov_x + 1, xp_bar_y + 2), overlay_lbl, font=f_tiny, fill=(0, 0, 0))
            d.text((ov_x, xp_bar_y + 1), overlay_lbl, font=f_tiny, fill=(255, 240, 100))

        # ── Gemini quota bar (compact, above Claude bar) ───────────────────
        gem_used = quota.get("gemini_requests_today", 0) or 0
        gem_cap  = quota.get("gemini_rpd_cap", 1000) or 1000
        gem_reset = quota.get("gemini_reset_local", "?")
        gem_rpm  = quota.get("gemini_rpm_current", 0) or 0
        gem_rpm_cap = quota.get("gemini_rpm_cap", 60) or 60
        gem_frac = min(1.0, gem_used / max(1, gem_cap))
        # Color: green under 70%, warn 70-90%, crit above
        if gem_frac < 0.7:   gem_color = (60, 200, 120)   # green
        elif gem_frac < 0.9: gem_color = (220, 160, 60)   # warn
        else:                gem_color = (220, 80, 80)    # crit
        # Label row (above bar): "GEMINI 3/1000 (0.3%) · rpm 2/60 · ↺ 12:00am"
        gem_lbl_left = f"GEMINI {gem_used}/{gem_cap}"
        gem_lbl_right = f"rpm {gem_rpm}/{gem_rpm_cap}  ↺ {gem_reset}"
        d.text((L, gem_label_y), gem_lbl_left, font=f_tiny, fill=gem_color)
        gr_bb = d.textbbox((0, 0), gem_lbl_right, font=f_tiny)
        d.text((R - (gr_bb[2] - gr_bb[0]), gem_label_y), gem_lbl_right, font=f_tiny, fill=DIM)
        # Compact 5px bar (matches XP bar visual style, one-segment)
        gem_bar_w = R - L
        gem_used_w = max(0, min(gem_bar_w, int(gem_bar_w * gem_frac)))
        # Background (unused capacity)
        d.rectangle([L, gem_bar_y, R, gem_bar_y + 5], fill=(30, 40, 55))
        # Used portion in tier color
        if gem_used_w > 0:
            d.rectangle([L, gem_bar_y, L + gem_used_w, gem_bar_y + 5], fill=gem_color)
        # Frame
        d.rectangle([L, gem_bar_y, R, gem_bar_y + 5], outline=(60, 70, 85))

        # ── TURN as 3D drop-shadow below the XP bar ────────────────────────
        # Renders BELOW the XP bar, offset +2px right & +9px down, 4px tall.
        # Visually reads as the shadow being cast by the XP bar; the used
        # portion is the asymmetry color, the rest is a darker void.
        if turns_cap and turns_cap > 0:
            sh_x0 = L + 2
            sh_x1 = R + 2 if R + 2 <= W - 6 else R   # avoid running off canvas
            sh_y0 = xp_bar_y + 9
            sh_y1 = sh_y0 + 4
            sh_w  = sh_x1 - sh_x0
            t_used_w = int(sh_w * min(1.0, turns_pct))
            # Dim void background
            d.rectangle([sh_x0, sh_y0, sh_x1, sh_y1], fill=(22, 26, 34))
            # Used portion in asymmetry color
            if t_used_w > 0:
                d.rectangle([sh_x0, sh_y0, sh_x0 + t_used_w, sh_y1], fill=t_color)
            # Thin top edge highlight to sell the 3D fold
            d.line([(sh_x0, sh_y0), (sh_x1, sh_y0)], fill=(50, 56, 70), width=1)

    try:
        stamp = now.strftime("%-I:%M:%S%p").lower()
    except ValueError:
        stamp = now.strftime("%I:%M:%S%p").lstrip("0").lower()
    d.text((L, B - 8), stamp, font=f_tiny, fill=DIM)
    hb = ACCENT if tick % 2 == 0 else OK
    # Heartbeat fully inside the inset
    d.ellipse([(R - 10, B - 10), (R - 2, B - 2)], fill=hb)

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90, optimize=True)
    return buf.getvalue()
