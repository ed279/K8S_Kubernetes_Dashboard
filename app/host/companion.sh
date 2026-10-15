#!/usr/bin/env bash
# RAYCUE host companion: write systemctl + per-GPU state to /dev/shm/cephalobot/host_state.json
# Multi-GPU aware (2026-07-06): enumerates NVIDIA (nvidia-smi), AMD (sysfs), Intel (best-effort).
set -uo pipefail

OUT="/dev/shm/cephalobot/host_state.json"
TMP="${OUT}.tmp"

mkdir -p "$(dirname "$OUT")"

SERVICES=(
  openclaw-watchdog
  openclaw-kraken
  openclaw-claude-queue-drain
  openclaw-cloudflared
  openclaw-kiwix
  openclaw-clawedgig-bot
  openclaw-teach-worker
)

svc_json=""
for u in "${SERVICES[@]}"; do
  state=$(systemctl is-active "$u" 2>/dev/null || true)
  [[ -z "$state" ]] && state="unknown"
  name="${u#openclaw-}"
  if [[ -n "$svc_json" ]]; then svc_json+=","; fi
  svc_json+="{\"name\":\"$name\",\"state\":\"$state\"}"
done

# ── Per-GPU enumeration ─────────────────────────────────────────────────────
# Emits gpus[] with {vendor, name, util_pct, mem_used_mb, mem_total_mb, source}.
# Vendor short codes: N=nvidia, A=amd, I=intel.
gpus_json=""

_add_gpu() {
  local vendor="$1" name="$2" util="$3" mused="$4" mtotal="$5" source="$6"
  # Guard numeric fields
  [[ "$util" =~ ^[0-9]+$ ]] || util=0
  [[ "$mused" =~ ^[0-9]+$ ]] || mused=0
  [[ "$mtotal" =~ ^[0-9]+$ ]] || mtotal=0
  # Escape name quotes minimally
  name="${name//\"/}"
  if [[ -n "$gpus_json" ]]; then gpus_json+=","; fi
  gpus_json+="{\"vendor\":\"$vendor\",\"name\":\"$name\",\"util_pct\":$util,\"mem_used_mb\":$mused,\"mem_total_mb\":$mtotal,\"source\":\"$source\"}"
}

# NVIDIA — nvidia-smi (all GPUs, one row each)
if command -v nvidia-smi >/dev/null 2>&1; then
  while IFS=',' read -r name util mused mtotal; do
    name="${name## }"; util="${util## }"; mused="${mused## }"; mtotal="${mtotal## }"
    [[ -z "$name" ]] && continue
    _add_gpu "N" "$name" "$util" "$mused" "$mtotal" "nvidia-smi"
  done < <(nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null)
fi

# AMD — sysfs (Radeon/AMDGPU) via /sys/class/drm/card*/device/{vendor,gpu_busy_percent,mem_info_vram_used,mem_info_vram_total}
for dev in /sys/class/drm/card*/device; do
  [[ -e "$dev/vendor" ]] || continue
  vendor_id=$(cat "$dev/vendor" 2>/dev/null)
  if [[ "$vendor_id" == "0x1002" ]]; then
    util=$(cat "$dev/gpu_busy_percent" 2>/dev/null || echo 0)
    mused_bytes=$(cat "$dev/mem_info_vram_used" 2>/dev/null || echo 0)
    mtotal_bytes=$(cat "$dev/mem_info_vram_total" 2>/dev/null || echo 0)
    mused=$(( mused_bytes / 1048576 ))
    mtotal=$(( mtotal_bytes / 1048576 ))
    # Try to get a readable name from lspci
    pci_addr=$(basename $(readlink -f "$dev") 2>/dev/null)
    name=$(lspci -s "$pci_addr" 2>/dev/null | sed 's/.*: //; s/ (rev.*//')
    [[ -z "$name" ]] && name="AMD GPU"
    _add_gpu "A" "$name" "$util" "$mused" "$mtotal" "sysfs"
  fi
done

# Intel — best-effort via /sys/class/drm/card*/engine/*/busy (kernel ≥ 5.7)
# Falls back to reporting 0 util if intel-gpu-tools not installed.
for dev in /sys/class/drm/card*/device; do
  [[ -e "$dev/vendor" ]] || continue
  vendor_id=$(cat "$dev/vendor" 2>/dev/null)
  if [[ "$vendor_id" == "0x8086" ]]; then
    # Intel iGPU utilization is not exposed as a single busy_percent.
    # Best-effort: sum engine busy counters, sample twice, compute delta.
    # Skip if the sysfs path doesn't exist — just report 0 with source=unavailable.
    parent_card=$(dirname "$dev")
    if ls "$parent_card"/engine/*/busy >/dev/null 2>&1; then
      # Sample delta over 200ms
      total1=0; for f in "$parent_card"/engine/*/busy; do total1=$(( total1 + $(cat "$f" 2>/dev/null || echo 0) )); done
      sleep 0.2
      total2=0; for f in "$parent_card"/engine/*/busy; do total2=$(( total2 + $(cat "$f" 2>/dev/null || echo 0) )); done
      # engine busy is nanoseconds cumulative; 200ms window = 200_000_000 ns per engine
      # engines_count * 200M = max
      n_engines=$(ls "$parent_card"/engine/ 2>/dev/null | wc -l)
      max_ns=$(( n_engines * 200000000 ))
      delta=$(( total2 - total1 ))
      if (( max_ns > 0 )); then
        util=$(( delta * 100 / max_ns ))
        (( util > 100 )) && util=100
      else
        util=0
      fi
      source="sysfs-engine"
    else
      util=0
      source="unavailable"
    fi
    pci_addr=$(basename $(readlink -f "$dev") 2>/dev/null)
    name=$(lspci -s "$pci_addr" 2>/dev/null | sed 's/.*: //; s/ (rev.*//')
    [[ -z "$name" ]] && name="Intel iGPU"
    _add_gpu "I" "$name" "$util" 0 0 "$source"
  fi
done

# Aggregate legacy fields for backward compat with existing render logic.
# Use MAX util across all GPUs (highlights the busiest), and SUM mem.
agg_util=0; agg_mused=0; agg_mtotal=0
if [[ -n "$gpus_json" ]]; then
  # Cheap parse: pull util_pct numbers out of the JSON snippets we built.
  for u in $(echo "$gpus_json" | grep -oE '"util_pct":[0-9]+' | cut -d: -f2); do
    (( u > agg_util )) && agg_util=$u
  done
  for m in $(echo "$gpus_json" | grep -oE '"mem_used_mb":[0-9]+' | cut -d: -f2); do
    agg_mused=$(( agg_mused + m ))
  done
  for m in $(echo "$gpus_json" | grep -oE '"mem_total_mb":[0-9]+' | cut -d: -f2); do
    agg_mtotal=$(( agg_mtotal + m ))
  done
fi

ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
cat > "$TMP" <<EOF
{
  "written_at": "$ts",
  "services": [$svc_json],
  "gpus": [$gpus_json],
  "gpu_util_pct": $agg_util,
  "gpu_mem_used_mb": $agg_mused,
  "gpu_mem_total_mb": $agg_mtotal
}
EOF
mv -f "$TMP" "$OUT"
chmod 0644 "$OUT" 2>/dev/null || true
