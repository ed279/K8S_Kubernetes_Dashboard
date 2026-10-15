"""Gather live state for the RAYCUE dashboard from inside the container.

Container can access:
  - /var/log/openclaw/ai_router.jsonl (ro bind-mount)        → tier states + events
  - /var/run/docker.sock (ro bind-mount)                     → container stats/ps
  - localhost ollama on 11434/11435/11436 (--network=host)   → loaded models
  - /host_root/proc/uptime, /proc/meminfo, /host_root/       → system metrics
  - /dev/shm/cephalobot/host_state.json (bind-mount)           → systemd + GPU (host companion)
  - /dev/shm/cephalobot/service_health.json (bind-mount)       → SHP tier colors (semantic green)
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import socket
import subprocess
import urllib.request
from concurrent.futures import ThreadPoolExecutor

AI_ROUTER_LOG = os.environ.get("AI_ROUTER_LOG", "/var/log/openclaw/ai_router.jsonl")
HOST_STATE = os.environ.get("HOST_STATE", "/dev/shm/cephalobot/host_state.json")
SHP_STATE = os.environ.get("SHP_STATE", "/dev/shm/cephalobot/service_health.json")
BACKLOG_RING = os.environ.get("BACKLOG_RING", "/dev/shm/cephalobot/backlog_history.json")
HOST_ROOT = os.environ.get("HOST_ROOT", "/host")  # bind-mount host / for df
OLLAMA_PORTS = [p.strip() for p in os.environ.get("OLLAMA_PORTS", "11434,11435,11436,11437").split(",") if p.strip()]
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")


def _docker_get(path: str, timeout: float = 3.0) -> list | dict | None:
    """Query Docker engine over the unix socket."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(DOCKER_SOCK)
        req = f"GET {path} HTTP/1.0\r\nHost: localhost\r\n\r\n".encode()
        s.sendall(req)
        chunks = []
        while True:
            buf = s.recv(65536)
            if not buf:
                break
            chunks.append(buf)
    except Exception:
        return None
    finally:
        s.close()
    raw = b"".join(chunks)
    # split headers/body
    header_end = raw.find(b"\r\n\r\n")
    if header_end < 0:
        return None
    body = raw[header_end + 4:]
    try:
        return json.loads(body)
    except Exception:
        return None


K8S_MIGRATED_DOCKER = {
    "openclaw-cloudflared", "openclaw-kiwix", "openclaw-queue-drain",
    "openclaw-mcp-controller", "openclaw-mcp-builder", "openclaw-mcp-registry",
    "openclaw-router-health-watcher", "openclaw-router-mode-server",
    "openclaw-state-freshness-watchdog", "openclaw-teach-worker",
    "openclaw-tesseract-bridge", "openclaw-timeout-watcher",
    "openclaw-watchdog", "openclaw-dashboard-sidecar", "openclaw-ramdisk",
    "openclaw-service-control-server", "openclaw-loki", "openclaw-promtail",
    "openclaw-loki-cors", "openclaw-redis",
}


def _list_containers() -> list:
    """Fetch all containers (running + stopped) with stats from Docker engine."""
    raw_list = _docker_get("/containers/json?all=1") or []
    # Build name → {state, status} map
    summary = []
    for entry in raw_list:
        names = entry.get("Names") or []
        name = (names[0] if names else "").lstrip("/")
        if not name or name.startswith("clawgig_job_"):
            continue
        if name in K8S_MIGRATED_DOCKER:
            continue
        # Ephemeral MCP tool containers cycle Running→Exited every smoke run by
        # design. Detect by explicit label OR image path fallback (older MCP
        # tool images built before the label was added — e.g. gracious_bohr
        # running harbor.local/openclaw/mcp-celsius-to-fahrenheit).
        # NOTE: exclude the long-running MCP service containers by name —
        # controller/builder/registry are infrastructure, not ephemeral tools.
        labels = entry.get("Labels") or {}
        image_name = entry.get("Image", "") or ""
        MCP_SERVICE_NAMES = {"openclaw-mcp-controller", "openclaw-mcp-builder",
                             "openclaw-mcp-registry"}
        is_mcp_tool = (
            labels.get("openclaw.mcp.kind") == "tool"
            or (
                "openclaw/mcp-" in image_name
                and name not in MCP_SERVICE_NAMES
                # Also skip if image tag matches a known MCP service pattern
                and not any(svc in image_name for svc in
                            ("mcp-controller", "mcp-builder", "mcp-registry"))
            )
        )
        state_str = entry.get("State", "")
        status_str = entry.get("Status", "")
        running = state_str == "running"
        # Running MCP tools = live workload, stay hidden (they cycle rapidly and
        # would flood the container list). Exited MCP tools = "expected idle" —
        # keep them visible but tagged so the dashboard renders IDLE (hollow
        # gray) instead of red DOWN.
        if is_mcp_tool and running:
            continue
        healthy = "(healthy)" in status_str
        summary.append({
            "name": name,
            "id": entry.get("Id"),
            "running": running,
            "healthy": healthy,
            "expected_idle": is_mcp_tool and not running,
        })

    # Stats: per-container CPU% + mem usage. Fan out concurrently — Docker engine handles
    # parallel /stats?stream=0 calls fine, and our previous serial loop ran ~22s for 12 containers.
    def _one(c: dict) -> dict:
        cpu_pct = 0.0
        mem_mb = 0.0
        mem_pct = 0.0
        if c["running"]:
            st = _docker_get(f"/containers/{c['id']}/stats?stream=0", timeout=4.0)
            if isinstance(st, dict):
                try:
                    cpu_delta = st["cpu_stats"]["cpu_usage"]["total_usage"] - st["precpu_stats"]["cpu_usage"]["total_usage"]
                    sys_delta = st["cpu_stats"]["system_cpu_usage"] - st["precpu_stats"]["system_cpu_usage"]
                    online = st["cpu_stats"].get("online_cpus") or len(st["cpu_stats"]["cpu_usage"].get("percpu_usage", []) or [1])
                    if sys_delta > 0 and cpu_delta > 0:
                        cpu_pct = (cpu_delta / sys_delta) * online * 100.0
                except (KeyError, TypeError, ZeroDivisionError):
                    pass
                try:
                    mem_used = st["memory_stats"]["usage"]
                    mem_cache = st["memory_stats"].get("stats", {}).get("cache", 0)
                    mem_used -= mem_cache
                    mem_mb = mem_used / (1024 * 1024)
                    mem_limit = st["memory_stats"]["limit"]
                    mem_pct = (mem_used / mem_limit) * 100.0 if mem_limit else 0.0
                except (KeyError, TypeError, ZeroDivisionError):
                    pass
        return {
            "name": c["name"],
            "status": "up" if c["running"] else "down",
            "healthy": c["healthy"],
            "cpu_pct": round(cpu_pct, 1),
            "mem_mb": round(mem_mb, 0),
            "mem_pct": round(mem_pct, 2),
            "expected_idle": c.get("expected_idle", False),
        }

    workers = max(2, min(16, len(summary)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dstats") as ex:
        out = list(ex.map(_one, summary))
    out.sort(key=lambda c: (c["status"] == "up", -c["mem_mb"]))
    return out


def _ai_router_state() -> tuple[dict, dict, int, list]:
    """Tail ai_router.jsonl, derive tier circuit states + last event + recent route trace."""
    tier_state: dict[str, str] = {}
    last_event = None
    events_recent = 0
    recent_routes: list = []  # most recent first
    ROUTE_EVENTS = {"tier_call", "inference", "complete", "smoke_test"}
    try:
        with open(AI_ROUTER_LOG, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 256 * 1024)  # last 256KB
            f.seek(size - chunk)
            data = f.read().decode("utf-8", errors="ignore")
    except Exception:
        return tier_state, {}, 0, recent_routes

    lines = data.splitlines()
    if lines and not data.endswith("\n"):
        lines = lines[1:]  # drop possibly-truncated first line
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        events_recent += 1
        tier = ev.get("tier")
        ev_kind = ev.get("event")
        if ev_kind == "circuit_change" and tier:
            new_state = (ev.get("extra") or {}).get("new_state")
            if new_state:
                tier_state[tier] = new_state
        # Treat successful smoke_test as authoritative "closed" signal —
        # CB resets aren't always logged as circuit_change events, so a stale
        # historical "open" state will persist forever otherwise.
        # A failed smoke_test does NOT override; let the CB module decide via
        # its own circuit_change emission to avoid flapping the dashboard on
        # transient probe failures.
        if ev_kind == "smoke_test" and tier and bool(ev.get("success", False)):
            tier_state[tier] = "closed"
        if ev_kind in ROUTE_EVENTS and tier:
            ts = ev.get("ts", "")
            recent_routes.append({
                "ts": ts[11:19] if len(ts) >= 19 else ts,
                "tier": tier,
                "model": ev.get("model") or "—",
                "event": ev_kind,
                "success": bool(ev.get("success", True)),
                "latency_ms": ev.get("latency_ms"),
            })
        last_event = ev
    # keep last 8 routes (chronological → reversed for "most recent first" display)
    recent_routes = recent_routes[-8:]
    le = {}
    if last_event:
        ts = last_event.get("ts", "")
        le = {
            "ts": ts[11:19] if len(ts) >= 19 else ts,
            "event": last_event.get("event", "?"),
            "tier": last_event.get("tier") or "-",
            "model": last_event.get("model") or "-",
        }
    return tier_state, le, events_recent, recent_routes


def _ollama_state() -> dict:
    """Per-port ollama loaded-models list."""
    out: dict[str, dict] = {}
    for port in OLLAMA_PORTS:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/ps")
            with urllib.request.urlopen(req, timeout=1.5) as r:
                body = json.loads(r.read().decode("utf-8"))
                models = [m.get("name", "?") for m in body.get("models", [])]
                out[port] = {"loaded": models}
        except Exception:
            out[port] = {"loaded": None}  # DOWN signal
    return out


def _system_metrics() -> dict:
    """Pull host load, mem, disk from /proc + host bind-mount."""
    metrics = {"load": [0.0, 0.0, 0.0], "uptime": "", "mem_used_mb": 0, "mem_total_mb": 0,
               "disk_used_pct": 0, "disk_used": "", "disk_total": ""}
    # /proc is host's since we run with --pid=host or container's procfs reflects host.
    # In Docker on Linux, /proc shows container view by default. Use /host_root/proc if mounted.
    proc_root = "/host_root/proc" if os.path.isdir("/host_root/proc") else "/proc"
    try:
        with open(f"{proc_root}/loadavg") as f:
            parts = f.read().strip().split()
            metrics["load"] = [float(parts[0]), float(parts[1]), float(parts[2])]
    except Exception:
        pass
    try:
        with open(f"{proc_root}/uptime") as f:
            secs = float(f.read().split()[0])
            mins = int(secs // 60)
            if mins < 60:
                metrics["uptime"] = f"{mins} min"
            elif mins < 60 * 24:
                metrics["uptime"] = f"{mins // 60}h {mins % 60}m"
            else:
                d = mins // (60 * 24)
                h = (mins % (60 * 24)) // 60
                metrics["uptime"] = f"{d}d {h}h"
    except Exception:
        pass
    try:
        with open(f"{proc_root}/meminfo") as f:
            mem = {}
            for line in f:
                k, _, v = line.partition(":")
                mem[k.strip()] = v.strip()
            total_kb = int(mem["MemTotal"].split()[0])
            avail_kb = int(mem.get("MemAvailable", mem["MemFree"]).split()[0])
            metrics["mem_total_mb"] = total_kb // 1024
            metrics["mem_used_mb"] = (total_kb - avail_kb) // 1024
    except Exception:
        pass
    # Disk: use df on host bind-mount
    target = HOST_ROOT if os.path.isdir(HOST_ROOT) else "/"
    try:
        r = subprocess.run(["df", "-BM", "--output=size,used,pcent", target],
                           capture_output=True, text=True, timeout=2.0)
        lines = r.stdout.strip().splitlines()
        if len(lines) >= 2:
            cells = lines[1].split()
            if len(cells) >= 3:
                size_mb = int(cells[0].rstrip("M"))
                used_mb = int(cells[1].rstrip("M"))
                metrics["disk_total"] = f"{size_mb // 1024}G"
                metrics["disk_used"] = f"{used_mb // 1024}G"
                metrics["disk_used_pct"] = int(cells[2].rstrip("%"))
    except Exception:
        pass
    return metrics


def _host_companion_state() -> dict:
    """Read systemctl + nvidia-smi state written by host companion."""
    try:
        with open(HOST_STATE) as f:
            return json.load(f)
    except Exception:
        return {}


# Full GPU/model architecture from ai_router.py (authoritative as of 2026-07-06).
# GPU_MODEL_MAP is enriched with live ollama status in collect_state().
# Phantom rows (unplugged GPUs with stale ports) are filtered out below.
GPU_MODEL_MAP = [
    {"gpu": "A2000",  "port": "11434", "primary": "llama3.1:8b",    "deep": "mistral:7b"},
    {"gpu": "6900XT", "port": "11435", "primary": "phi4",            "deep": None},
    {"gpu": "CPU",    "port": "11436", "primary": "deepseek-r1:1.5b","deep": None},
    {"gpu": "5060Ti", "port": "11437", "primary": "qwen2.5:14b",     "deep": None},
]

CLOUD_TIERS = [
    {"label": "T4-Poll", "provider": "Pollinations", "models": ["gpt-4o-mini", "qwen-coder"]},
    {"label": "T5-Cld",  "provider": "Claude",       "models": ["opus-4.8", "sonnet-4.6",  "haiku-4.5", "fable-5*"]},
]

# Static tier → preferred model mapping (used by render tier ladder).
# T3 default reflects the Anthropic fallback chain; render.py overrides it
# dynamically when recent_routes shows which model was actually invoked.
# T4 is now Gemini (2026-07-06) — Anthropic's cross-provider fallback per
# the new openclaw.json fallback chain.
# T3 updated 2026-07-06 per FABLE-TIER: Haiku default for cheap shapes,
# Fable* suffix = failover-only (rare, task-shape gated hail-mary).
TIER_MODELS = {
    "t1": "llama/phi4/deepseek",
    "t2": "mcp-cache",
    "t3": "haiku/sonnet/opus/fable*",
    "t4": "gemini-3-flash",
    "t5": "queue",
}

TESSERACT_EVENTS = os.environ.get("TESSERACT_EVENTS", "/dev/shm/cephalobot/tesseract_events.jsonl")


def _tesseract_state() -> dict:
    """Parse recent Tesseract events for dynamic source weights + last winner."""
    try:
        with open(TESSERACT_EVENTS, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 64 * 1024)
            f.seek(size - chunk)
            data = f.read().decode("utf-8", errors="ignore")
    except Exception:
        return {}
    wins: dict[str, int] = {}
    total_races = 0
    last_winner = None
    last_latency = None
    source_scores: dict[str, list[float]] = {}
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        kind = ev.get("kind")
        if kind == "race_winner_collapsed":
            p = ev.get("payload", {})
            w = p.get("winner", "")
            if w:
                wins[w] = wins.get(w, 0) + 1
                total_races += 1
                last_winner = w
                last_latency = p.get("latency_ms")
        elif kind == "sources_ranked":
            p = ev.get("payload", {})
            for entry in p.get("top_k", []):
                src = entry.get("source_id", "")
                score = entry.get("score", 0)
                if src:
                    source_scores.setdefault(src, []).append(score)
    if total_races:
        weights = {src: round(cnt / total_races * 100) for src, cnt in wins.items()}
        return {
            "weights": weights,
            "total_races": total_races,
            "last_winner": last_winner,
            "last_latency_ms": last_latency,
        }
    if source_scores:
        avg_scores = {src: sum(scores) / len(scores) for src, scores in source_scores.items()}
        total = sum(avg_scores.values()) or 1
        weights = {src: round(sc / total * 100) for src, sc in avg_scores.items()}
        top_source = max(avg_scores, key=avg_scores.get)
        return {
            "weights": weights,
            "total_races": len(next(iter(source_scores.values()))),
            "last_winner": top_source,
            "last_latency_ms": None,
            "mode": "ranked",
        }
    return {}


def _quota_state() -> dict:
    """Read claude_quota.json from ramdisk (pushed from Mac by fetch_loop).

    Returns data with quota_stale=True when file is >5 min old or missing,
    so render.py can show a STALE overlay instead of silently hiding the bar.
    """
    path = os.environ.get("CLAUDE_QUOTA_PATH", "/dev/shm/cephalobot/claude_quota.json")
    STALE_SECS = 300
    try:
        st = os.stat(path)
        age = _dt.datetime.now().timestamp() - st.st_mtime
        with open(path) as f:
            data = json.load(f)
        if age > STALE_SECS:
            data["quota_stale"] = True
            data["quota_age_s"] = int(age)
        return data
    except Exception:
        # File missing — return a sentinel so the bar shows STALE, not nothing
        return {"quota_stale": True, "quota_missing": True}


def collect_state() -> dict:
    tier_state, last_event, events_recent, recent_routes = _ai_router_state()
    containers = _list_containers()
    metrics = _system_metrics()
    host = _host_companion_state()
    quota = _quota_state()

    ollama_raw = _ollama_state()
    tesseract = _tesseract_state()

    # Enrich GPU_MODEL_MAP with live ollama status.
    # Skip GPU entries whose ollama daemon is down (alive=False), so the dashboard
    # doesn't show phantom rows for unplugged hardware with stale port configs.
    gpu_model_map = []
    for g in GPU_MODEL_MAP:
        port = g["port"]
        ol = ollama_raw.get(port, {})
        loaded = ol.get("loaded")  # None = daemon down, [] = up but no model warm
        alive = loaded is not None
        # Skip this GPU if its ollama endpoint is not responding
        if not alive:
            continue
        gpu_model_map.append({
            **g,
            "alive": alive,
            "loaded": loaded or [],
        })

    # Load backlog history snapshot (burn-down sparkline + chip)
    backlog = {}
    try:
        with open(BACKLOG_RING) as f:
            bl_raw = json.load(f)
        cur = bl_raw.get("current") or {}
        hist = bl_raw.get("history") or []
        # Down-sample history to one point per calendar day for the sparkline
        daily = {}
        for e in hist:
            day = (e.get("ts") or "")[:10]
            if day:
                daily[day] = e.get("pending", 0)  # last reading of day wins
        # Latest 8 days, oldest → newest
        spark = [v for _, v in sorted(daily.items())[-8:]]
        backlog = {
            "pending": cur.get("pending", 0),
            "completed": cur.get("completed", 0),
            "in_progress": cur.get("in_progress", 0),
            "tags": cur.get("tags", {}),
            "spark_pending": spark,  # list of last-N pending counts for sparkline
        }
    except Exception:
        pass

    # Load SHP (Service Health Protocol) state for semantic tier colors
    shp_state = {}
    k8s_pods = []
    try:
        with open(SHP_STATE) as f:
            shp_raw = json.load(f)
            shp_state = {
                "summary": shp_raw.get("summary", {}),
                "services": {
                    svc: {"color": data.get("color", "gray"),
                          "probe_ok": data.get("probe_result", False)}
                    for svc, data in shp_raw.get("services", {}).items()
                },
            }
            for svc, data in shp_raw.get("services", {}).items():
                if data.get("kind") == "k8s":
                    k8s_pods.append({
                        "name": svc,
                        "color": data.get("color", "gray"),
                        "probe_ok": data.get("probe_result", False),
                    })
            k8s_pods.sort(key=lambda p: (p["color"] != "green", p["name"]))
    except Exception:
        pass

    # Load K8s pod stats from ramdisk (written by service_health.py or watchdog)
    K8S_POD_STATS = os.environ.get("K8S_POD_STATS",
                                    SHP_STATE.replace("service_health.json", "k8s_pod_stats.json"))
    try:
        with open(K8S_POD_STATS) as f:
            k8s_raw = json.load(f)
        for p in k8s_raw:
            containers.append({
                "name": p.get("name", "?"),
                "status": "up" if p.get("status") == "up" else "down",
                "cpu_pct": round(p.get("cpu_pct", 0), 1),
                "mem_mb": round(p.get("mem_mb", 0), 0),
                "mem_pct": round(p.get("mem_pct", 0), 2),
                "healthy": p.get("status") == "up",
                "expected_idle": False,
                "k8s": True,
            })
    except Exception:
        pass

    # Dynamic tier model labels — override cloud tier defaults with the last
    # model actually invoked for that tier. Local tiers (t1/t2) stay static
    # (hardware-bound). Walking recent_routes newest-first; first hit per tier
    # wins so the label reflects the most recent Anthropic model used.
    dyn_tier_models = dict(TIER_MODELS)
    seen: set = set()
    for r in reversed(recent_routes):
        t = (r.get("tier") or "").lower()
        m = r.get("model")
        if t in ("t3", "t4", "t5") and m and m not in ("—", "-", "?", "") and t not in seen:
            # Trim provider prefix + normalize hyphens for the 15-char slot
            label = m.replace("claude-", "").replace("gemini-", "gem-")
            dyn_tier_models[t] = label
            seen.add(t)

    # Count Docker containers (non-K8s-migrated)
    docker_count = len([c for c in containers if not c.get("k8s", False)])

    # Filter in-house systemd services (exclude ubuntu/linux standard services)
    in_house_services = [
        s for s in host.get("services", [])
        if s.get("name", "").startswith("openclaw-") or s.get("name", "").startswith("cephalobot-")
    ]
    systemd_count = len(in_house_services)

    state = {
        "collected_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "ssh_ok": True,
        "tiers": tier_state,
        "tier_models": dyn_tier_models,
        "gpu_model_map": gpu_model_map,
        "cloud_tiers": CLOUD_TIERS,
        "last_event": last_event,
        "recent_routes": recent_routes,
        "containers": containers,
        "k8s_pods": k8s_pods,
        "services": host.get("services", []),
        "docker_count": docker_count,
        "systemd_services": in_house_services,
        "systemd_count": systemd_count,
        "shp": shp_state,  # Service Health Protocol state (semantic tier colors)
        "backlog": backlog,  # burn-down: current counts + spark_pending (last 8 daily readings)
        "load": metrics["load"],
        "uptime": metrics["uptime"],
        "mem_used_mb": metrics["mem_used_mb"],
        "mem_total_mb": metrics["mem_total_mb"],
        "disk_used_pct": metrics["disk_used_pct"],
        "disk_used": metrics["disk_used"],
        "disk_total": metrics["disk_total"],
        "gpu_util_pct": host.get("gpu_util_pct", 0),
        "gpu_mem_used_mb": host.get("gpu_mem_used_mb", 0),
        "gpu_mem_total_mb": host.get("gpu_mem_total_mb", 0),
        "gpus": host.get("gpus", []),  # per-GPU array {vendor, name, util_pct, mem_*, source}
        "events_recent": events_recent,
        "ollama": ollama_raw,
        "tesseract": tesseract,
        "quota": quota,
    }
    return state
