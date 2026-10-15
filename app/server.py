"""RAYCUE dashboard HTTP server + render loop.

Routes:
  GET /                 → latest dashboard HTML (repositioned layout)
  GET /dashboard.jpg    → latest 480x320 JPEG (in-memory)
  GET /state.json       → current collected state for debugging
  GET /healthz          → 200 if at least one render has succeeded
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from collect import collect_state
from render import render_to_bytes
from k8s_alerts_enricher import K8sAlertsEnricher, K8sPodEndpoints

PORT = int(os.environ.get("HTTP_PORT", "8088"))
INTERVAL = int(os.environ.get("RENDER_INTERVAL", "10"))
RAYCUE_PATH = os.environ.get("RAYCUE_PATH")  # Optional: write to /Volumes/RAYCUE/Photos on Mac or host mount

_LOCK = threading.Lock()
_LATEST_IMG: bytes = b""
_LATEST_STATE: dict = {}
_TICK = 0
_RENDER_OK = False
_LAST_RENDER_TS = 0.0
_LAST_ERROR = ""


def _renderer_loop() -> None:
    global _LATEST_IMG, _LATEST_STATE, _TICK, _RENDER_OK, _LAST_RENDER_TS, _LAST_ERROR
    toggle = 0  # alternate between v1 and v2
    while True:
        start = time.monotonic()
        try:
            state = collect_state()
            img = render_to_bytes(state, tick=_TICK)
            with _LOCK:
                _LATEST_IMG = img
                _LATEST_STATE = state
                _RENDER_OK = True
                _LAST_RENDER_TS = time.time()
                _LAST_ERROR = ""

            # Write to RayCUE mount if available, alternating v1/v2
            if RAYCUE_PATH:
                outfile = pathlib.Path(RAYCUE_PATH) / ("dashboard_v1.jpg" if toggle == 0 else "dashboard_v2.jpg")
                try:
                    outfile.parent.mkdir(parents=True, exist_ok=True)
                    outfile.write_bytes(img)
                    toggle = 1 - toggle
                except Exception as e:
                    print(f"[raycue] write failed: {e}", flush=True, file=sys.stderr)

            elapsed_render = time.monotonic() - start
            print(f"[render] tick={_TICK} bytes={len(img)} containers={len(state.get('containers', []))} "
                  f"dt={elapsed_render:.2f}s", flush=True)
        except Exception as exc:
            tb = traceback.format_exc()
            with _LOCK:
                _LAST_ERROR = f"{exc}: {tb}"
            print(f"[render] error: {exc}\n{tb}", flush=True, file=sys.stderr)
        _TICK = (_TICK + 1) % 1_000_000
        elapsed = time.monotonic() - start
        time.sleep(max(0.0, INTERVAL - elapsed))


class Handler(BaseHTTPRequestHandler):
    def _get_dashboard_html(self) -> str:
        """Return the repositioned dashboard HTML layout."""
        return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>CephaloBot Dashboard — Repositioned Layout</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: linear-gradient(135deg, #0f0f23 0%, #1a1a3e 100%);
            color: #e0e0e0;
            padding: 20px;
            min-height: 100vh;
        }
        .dashboard {
            max-width: 1600px;
            margin: 0 auto;
            display: grid;
            grid-template-columns: repeat(12, 1fr);
            gap: 16px;
            grid-auto-rows: 200px;
        }
        .widget {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 12px;
            padding: 16px;
            backdrop-filter: blur(10px);
            display: flex;
            flex-direction: column;
            justify-content: space-between;
            position: relative;
            overflow: hidden;
        }
        .widget::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 2px;
            background: linear-gradient(90deg, transparent, rgba(100, 200, 255, 0.3), transparent);
        }
        .widget-title {
            font-size: 13px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 1px;
            color: #888;
            margin-bottom: 12px;
        }
        .widget-content {
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 14px;
        }
        .widget-tesseract-tiers { grid-column: 1 / 3; grid-row: 1 / 3; background: linear-gradient(135deg, rgba(50, 80, 200, 0.1), rgba(100, 150, 255, 0.05)); border: 1px solid rgba(100, 150, 255, 0.2); }
        .widget-memory { grid-column: 1 / 3; grid-row: 3 / 5; background: linear-gradient(135deg, rgba(150, 100, 200, 0.1), rgba(200, 150, 255, 0.05)); border: 1px solid rgba(200, 150, 255, 0.2); }
        .widget-circle { grid-column: 10 / 13; grid-row: 1 / 5; background: radial-gradient(circle at 30% 30%, rgba(255, 150, 100, 0.15), rgba(100, 50, 150, 0.1)); border: 2px solid rgba(255, 150, 100, 0.3); }
        .widget-gpu-cpu { grid-column: 3 / 6; grid-row: 1 / 4; background: linear-gradient(135deg, rgba(100, 200, 100, 0.1), rgba(150, 255, 150, 0.05)); border: 1px solid rgba(100, 200, 100, 0.2); }
        .widget-raycue { grid-column: 6 / 9; grid-row: 1 / 3; background: linear-gradient(135deg, rgba(200, 100, 100, 0.1), rgba(255, 150, 150, 0.05)); border: 1px solid rgba(200, 100, 100, 0.2); }
        .widget-health { grid-column: 6 / 9; grid-row: 3 / 5; background: linear-gradient(135deg, rgba(100, 200, 200, 0.1), rgba(150, 255, 255, 0.05)); border: 1px solid rgba(100, 200, 200, 0.2); }
        .widget-services { grid-column: 9 / 12; grid-row: 5 / 7; background: linear-gradient(135deg, rgba(200, 200, 100, 0.1), rgba(255, 255, 150, 0.05)); border: 1px solid rgba(200, 200, 100, 0.2); }
        .widget-docker { grid-column: 1 / 3; grid-row: 5 / 7; background: linear-gradient(135deg, rgba(255, 140, 30, 0.15), rgba(255, 180, 60, 0.05)); border: 1px solid rgba(255, 140, 30, 0.3); }
        .widget-systemd { grid-column: 3 / 6; grid-row: 5 / 7; background: linear-gradient(135deg, rgba(168, 85, 247, 0.15), rgba(200, 130, 255, 0.05)); border: 1px solid rgba(168, 85, 247, 0.3); }
        .widget-alerts { grid-column: 3 / 9; grid-row: 4 / 6; background: linear-gradient(135deg, rgba(255, 100, 100, 0.1), rgba(255, 150, 150, 0.05)); border: 1px solid rgba(255, 100, 100, 0.3); }
        .alerts-container { width: 100%; max-height: 100%; overflow-y: auto; font-size: 11px; }
        .alert-item { background: rgba(0, 0, 0, 0.3); border-left: 3px solid #f87171; padding: 8px 12px; margin-bottom: 6px; border-radius: 4px; cursor: pointer; user-select: none; }
        .alert-item.critical { border-left-color: #dc2626; background: rgba(220, 38, 38, 0.1); }
        .alert-item.warning { border-left-color: #f59e0b; background: rgba(245, 158, 11, 0.1); }
        .alert-item.info { border-left-color: #3b82f6; background: rgba(59, 130, 246, 0.1); }
        .alert-summary { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
        .alert-badge { display: inline-block; padding: 2px 6px; border-radius: 3px; font-weight: 600; font-size: 10px; }
        .alert-badge.critical { background: rgba(220, 38, 38, 0.5); color: #fca5a5; }
        .alert-badge.warning { background: rgba(245, 158, 11, 0.5); color: #fcd34d; }
        .alert-details { display: none; background: rgba(0, 0, 0, 0.5); padding: 8px; border-radius: 3px; margin-top: 4px; font-size: 10px; }
        .alert-item.expanded .alert-details { display: block; }
        .remediation-step { margin: 4px 0; padding: 4px; background: rgba(255, 255, 255, 0.05); border-left: 2px solid #64c8ff; padding-left: 6px; }
        .endpoint-link { color: #64c8ff; text-decoration: none; }
        .endpoint-link:hover { text-decoration: underline; }
        .status-item { display: flex; align-items: center; justify-content: space-between; padding: 8px 0; font-size: 12px; border-bottom: 1px solid rgba(255, 255, 255, 0.05); }
        .status-item:last-child { border-bottom: none; }
        .status-indicator { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 8px; }
        .status-indicator.healthy { background: #4ade80; box-shadow: 0 0 8px rgba(74, 222, 128, 0.5); }
        .status-indicator.warning { background: #fbbf24; box-shadow: 0 0 8px rgba(251, 191, 36, 0.5); }
        .status-indicator.offline { background: #ef4444; box-shadow: 0 0 8px rgba(239, 68, 68, 0.5); }
        .chart-placeholder { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; background: rgba(255, 255, 255, 0.02); border-radius: 8px; color: #666; font-size: 12px; }
        .tier-list { width: 100%; display: flex; flex-direction: column; gap: 8px; }
        .tier-item { background: rgba(255, 255, 255, 0.05); border-left: 3px solid #64c8ff; padding: 8px 12px; border-radius: 4px; font-size: 12px; }
        .tier-item.t1 { border-left-color: #22c55e; }
        .tier-item.t2 { border-left-color: #3b82f6; }
        .tier-item.t3 { border-left-color: #8b5cf6; }
        .tier-item.t4 { border-left-color: #ec4899; }
        .tier-item.t5 { border-left-color: #f59e0b; }
        .header { grid-column: 1 / 13; grid-row: 0; padding: 20px; background: rgba(0, 0, 0, 0.3); border-radius: 12px; margin-bottom: 10px; }
        .header h1 { font-size: 28px; font-weight: 700; margin-bottom: 4px; }
        .header p { font-size: 13px; color: #888; }
        .refresh-control { position: fixed; bottom: 20px; right: 20px; background: rgba(0, 0, 0, 0.5); border: 1px solid rgba(100, 200, 255, 0.2); padding: 12px 16px; border-radius: 8px; z-index: 1000; }
        .refresh-control label { color: #888; font-size: 11px; margin-right: 8px; }
        .refresh-control select { background: rgba(255, 255, 255, 0.1); color: #64c8ff; border: 1px solid #64c8ff; padding: 4px 8px; border-radius: 4px; font-size: 11px; cursor: pointer; }
        .refresh-control input { margin-left: 8px; cursor: pointer; }
        .dropdown-marker { display: inline-block; margin-left: 4px; color: #64c8ff; cursor: pointer; font-weight: bold; user-select: none; }
        .expandable-content { display: none; margin-top: 8px; padding-top: 8px; border-top: 1px solid rgba(255, 255, 255, 0.1); }
        .expandable-content.expanded { display: block; }
        .endpoint-list { font-size: 10px; line-height: 1.8; }
        .endpoint-link { color: #64c8ff; text-decoration: none; margin: 0 4px; }
        .endpoint-link:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="header">
        <h1>🧠 CephaloBot Infrastructure Dashboard</h1>
        <p>Real-time cluster health, model routing, and hardware status</p>
    </div>
    <div class="dashboard">
        <div class="widget widget-tesseract-tiers">
            <div class="widget-title">⚡ Tesseract Router Tiers</div>
            <div class="widget-content">
                <div class="tier-list">
                    <div class="tier-item t1">T1: Local GPU (llama-2-70b, phi-4) ✅</div>
                    <div class="tier-item t2">T2: MCP Cache ✅</div>
                    <div class="tier-item t3">T3: Cloud (Claude Sonnet) ✅</div>
                    <div class="tier-item t4">T4: Qwen (RTX 6900 XT) ⚠️ Offline</div>
                    <div class="tier-item t5">T5: Queue Drain ✅</div>
                </div>
            </div>
        </div>
        <div class="widget widget-memory">
            <div class="widget-title">💾 System Memory</div>
            <div class="widget-content">
                <div class="chart-placeholder">Doughnut: 32GB / 64GB (50%)</div>
            </div>
        </div>
        <div class="widget widget-gpu-cpu">
            <div class="widget-title">🎮 GPU / CPU Status</div>
            <div class="widget-content">
                <div style="width: 100%; font-size: 12px; line-height: 1.8;">
                    <div class="status-item"><span><span class="status-indicator healthy"></span>RTX A2000</span><span>8GB ✅</span></div>
                    <div class="status-item"><span><span class="status-indicator offline"></span>RTX 6900 XT</span><span>16GB 🔌</span></div>
                    <div class="status-item"><span><span class="status-indicator healthy"></span>CPU Fallback</span><span>24 cores ✅</span></div>
                    <div class="status-item"><span><span class="status-indicator healthy"></span>ollama-a2000</span><span>running</span></div>
                    <div class="status-item"><span><span class="status-indicator offline"></span>ollama-6900xt</span><span>offline</span></div>
                </div>
            </div>
        </div>
        <div class="widget widget-raycue">
            <div class="widget-title">🎯 Raycue Cluster Visualization</div>
            <div class="widget-content">
                <div class="chart-placeholder">3D Cluster Topology + Service Graph</div>
            </div>
        </div>
        <div class="widget widget-health">
            <div class="widget-title">❤️ Pod Health Summary</div>
            <div class="widget-content">
                <div style="width: 100%; font-size: 12px; line-height: 1.8;">
                    <div class="status-item"><span><span class="status-indicator healthy"></span>cephalobot (28)</span><span>running</span></div>
                    <div class="status-item"><span><span class="status-indicator healthy"></span>kraken (3)</span><span>running</span></div>
                    <div class="status-item"><span><span class="status-indicator warning"></span>teach-worker</span><span>262 restarts</span></div>
                </div>
            </div>
        </div>
        <div class="widget widget-circle">
            <div class="widget-title">🎲 Tesseract Arbiter</div>
            <div class="widget-content">
                <svg viewBox="0 0 200 200" style="width: 140px; height: 140px;">
                    <circle cx="100" cy="100" r="95" fill="none" stroke="rgba(255, 150, 100, 0.3)" stroke-width="2"/>
                    <circle cx="100" cy="100" r="70" fill="none" stroke="rgba(100, 200, 255, 0.3)" stroke-width="2"/>
                    <circle cx="100" cy="100" r="45" fill="rgba(255, 150, 100, 0.1)" stroke="rgba(255, 150, 100, 0.5)" stroke-width="2"/>
                    <circle cx="100" cy="100" r="6" fill="#ff9664"/>
                    <line x1="100" y1="100" x2="100" y2="30" stroke="rgba(100, 200, 255, 0.5)" stroke-width="1.5" stroke-dasharray="3,3"/>
                    <line x1="100" y1="100" x2="170" y2="70" stroke="rgba(100, 200, 255, 0.5)" stroke-width="1.5" stroke-dasharray="3,3"/>
                    <line x1="100" y1="100" x2="170" y2="130" stroke="rgba(100, 200, 255, 0.5)" stroke-width="1.5" stroke-dasharray="3,3"/>
                    <line x1="100" y1="100" x2="100" y2="170" stroke="rgba(100, 200, 255, 0.5)" stroke-width="1.5" stroke-dasharray="3,3"/>
                </svg>
                <div style="font-size: 11px; margin-top: 8px; color: #aaa;">Race winner selection<br/>via amplitude weighting</div>
            </div>
        </div>
        <div class="widget widget-services">
            <div class="widget-title">🚀 Key Services</div>
            <div class="widget-content">
                <div style="width: 100%; font-size: 11px; line-height: 1.9;">
                    <div class="status-item"><span><span class="status-indicator healthy"></span>tesseract-router</span></div>
                    <div class="status-item"><span><span class="status-indicator healthy"></span>redis</span></div>
                    <div class="status-item"><span><span class="status-indicator healthy"></span>loki/promtail</span></div>
                    <div class="status-item"><span><span class="status-indicator healthy"></span>mcp-controller</span></div>
                </div>
            </div>
        </div>
        <div class="widget widget-docker">
            <div class="widget-title">🐳 Docker Containers</div>
            <div class="widget-content">
                <div style="width: 100%; text-align: center;">
                    <div style="font-size: 28px; font-weight: 700; color: #ff8c1e; margin-bottom: 8px;" id="docker-count">0</div>
                    <div style="font-size: 11px; color: #888;">Non-K8s containers</div>
                </div>
            </div>
        </div>
        <div class="widget widget-systemd">
            <div class="widget-title">⚙️ Systemd Services</div>
            <div class="widget-content">
                <div style="width: 100%; font-size: 11px; line-height: 1.8;" id="systemd-services">
                    <div style="color: #888; text-align: center; padding: 20px;">Loading...</div>
                </div>
            </div>
        </div>
        <div class="widget widget-alerts">
            <div class="widget-title">⚠️ K8s Alerts & Pod Endpoints</div>
            <div class="widget-content">
                <div class="alerts-container" id="alerts-container">
                    <div style="color: #666; text-align: center; padding: 20px;">Loading alerts...</div>
                </div>
            </div>
        </div>
    </div>

    <div class="refresh-control">
        <label>Refresh:</label>
        <select id="refresh-interval" onchange="setRefreshInterval(this.value)">
            <option value="off">Off</option>
            <option value="5000" selected>5s</option>
            <option value="10000">10s</option>
            <option value="15000">15s</option>
        </select>
    </div>

    <script>
        let refreshInterval = 5000;
        let refreshTimer = null;

        function setRefreshInterval(value) {
            if (refreshTimer) clearInterval(refreshTimer);
            if (value === 'off') {
                refreshInterval = null;
            } else {
                refreshInterval = parseInt(value);
                fetchAlerts();
                refreshTimer = setInterval(fetchAlerts, refreshInterval);
            }
        }

        async function fetchAlerts() {
            try {
                const response = await fetch('/alerts.json');
                const data = await response.json();
                renderAlerts(data);
            } catch (e) {
                console.error('Alert fetch failed:', e);
                document.getElementById('alerts-container').innerHTML =
                    '<div style="color: #f87171; font-size: 11px;">Error loading alerts</div>';
            }
        }

        function renderAlerts(data) {
            const container = document.getElementById('alerts-container');
            let html = '<div class="alert-summary">';

            // Summary badges
            html += '<div style="display: flex; gap: 8px;">';
            if (data.summary.critical > 0) {
                html += `<span class="alert-badge critical">🔴 Critical: ${data.summary.critical}</span>`;
            }
            if (data.summary.warning > 0) {
                html += `<span class="alert-badge warning">⚠️ Warning: ${data.summary.warning}</span>`;
            }
            html += `<span class="alert-badge" style="background: rgba(59, 130, 246, 0.5); color: #93c5fd;">ℹ️ Total: ${data.summary.total_alerts}</span>`;
            html += '</div></div>';

            // Alerts list with dropdown markers
            if (data.alerts && data.alerts.length > 0) {
                data.alerts.forEach((alert, idx) => {
                    const urgency = alert.remediation?.urgency || 'info';
                    const expandId = `alert-${idx}`;
                    html += `
                        <div class="alert-item ${urgency}">
                            <div style="display: flex; justify-content: space-between; align-items: center; cursor: pointer;" onclick="document.getElementById('${expandId}').classList.toggle('expanded')">
                                <span><strong>[${alert.reason}]</strong> ${alert.pod} <span class="dropdown-marker">></span></span>
                                <span style="color: #888; font-size: 10px;">${alert.namespace}</span>
                            </div>
                            <div class="alert-details expandable-content" id="${expandId}">
                                <div style="margin-bottom: 6px; color: #ccc; font-size: 10px;">${alert.message}</div>
                                <div style="color: #64c8ff; font-weight: 600; margin-top: 6px; margin-bottom: 4px; font-size: 10px;">Fix Steps:</div>
                    `;

                    if (alert.remediation?.steps) {
                        alert.remediation.steps.forEach(step => {
                            html += `<div class="remediation-step">→ ${step}</div>`;
                        });
                    }

                    html += '</div></div>';
                });
            } else {
                html += '<div style="color: #4ade80; font-size: 11px; text-align: center; padding: 20px;">✅ No active alerts</div>';
            }

            // Pod endpoints section with dropdowns
            if (data.pod_endpoints && data.pod_endpoints.length > 0) {
                html += '<div style="margin-top: 12px; border-top: 1px solid rgba(255, 255, 255, 0.1); padding-top: 8px;">';
                html += '<div style="color: #64c8ff; font-weight: 600; margin-bottom: 6px; font-size: 10px;">📍 Pod Endpoints</div>';
                data.pod_endpoints.forEach((ep, idx) => {
                    const endpointId = `endpoint-${idx}`;
                    html += `
                        <div style="margin-bottom: 6px; font-size: 10px; cursor: pointer;" onclick="document.getElementById('${endpointId}').classList.toggle('expanded')">
                            <strong>${ep.service}</strong> <span class="dropdown-marker">></span>
                            <div class="endpoint-list expandable-content" id="${endpointId}">
                                <div>🌐 LAN: <a class="endpoint-link" href="http://10.0.9.99:${ep.port}" target="_blank">http://10.0.9.99:${ep.port}</a></div>
                                <div>🔗 Tailscale: <a class="endpoint-link" href="http://100.71.53.83:${ep.port}" target="_blank">http://100.71.53.83:${ep.port}</a></div>
                            </div>
                        </div>
                    `;
                });
                html += '</div>';
            }

            container.innerHTML = html;
        }

        async function fetchDashboardState() {
            try {
                const response = await fetch('/state.json');
                const state = await response.json();

                // Update Docker count
                const dockerCount = state.docker_count || 0;
                document.getElementById('docker-count').textContent = dockerCount;

                // Update Systemd services
                const systemdServices = state.systemd_services || [];
                let systemdHtml = '';
                if (systemdServices.length === 0) {
                    systemdHtml = '<div style="color: #4ade80; text-align: center; padding: 20px; font-size: 11px;">✅ No in-house services</div>';
                } else {
                    systemdServices.forEach(svc => {
                        const status = svc.status === 'running' || svc.status === 'active' ? 'healthy' : 'offline';
                        systemdHtml += `<div class="status-item"><span><span class="status-indicator ${status}"></span>${svc.name}</span><span style="font-size: 10px; color: #888;">${svc.status}</span></div>`;
                    });
                }
                document.getElementById('systemd-services').innerHTML = systemdHtml;
            } catch (e) {
                console.error('State fetch failed:', e);
            }
        }

        // Initialize refresh
        fetchAlerts();
        fetchDashboardState();
        refreshTimer = setInterval(() => {
            fetchAlerts();
            fetchDashboardState();
        }, refreshInterval);

        // Cleanup on page unload
        window.addEventListener('beforeunload', () => {
            if (refreshTimer) clearInterval(refreshTimer);
        });
    </script>
</body>
</html>"""

    def _respond(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/dashboard.jpg":
            with _LOCK:
                img = _LATEST_IMG
                ok = _RENDER_OK
            if not ok or not img:
                self._respond(503, b"no render yet\n", "text/plain")
                return
            self._respond(200, img, "image/jpeg")
        elif path == "/healthz":
            with _LOCK:
                ok = _RENDER_OK
                age = time.time() - _LAST_RENDER_TS if _LAST_RENDER_TS else None
            if ok and age is not None and age < INTERVAL * 3:
                self._respond(200, b"ok\n", "text/plain")
            else:
                msg = f"unhealthy (render_ok={ok}, age={age})\n".encode()
                self._respond(503, msg, "text/plain")
        elif path == "/state.json":
            with _LOCK:
                body = json.dumps(_LATEST_STATE, indent=2, default=str).encode()
            self._respond(200, body, "application/json")
        elif path == "/alerts.json":
            # K8s alerts with verbose remediation + pod endpoints
            try:
                enricher = K8sAlertsEnricher()
                endpoints = K8sPodEndpoints()

                # Collect alerts from all key namespaces
                all_alerts = []
                all_endpoints = []
                for ns in ["default", "cephalobot", "vault", "tesseract", "dns", "prometheus"]:
                    alerts = enricher.get_pod_events(ns)
                    all_alerts.extend(alerts)
                    urls = endpoints.get_pod_urls(ns)
                    all_endpoints.extend(urls)

                # Build response
                response = {
                    "timestamp": time.time(),
                    "alerts": all_alerts,
                    "pod_endpoints": all_endpoints,
                    "summary": {
                        "total_alerts": len(all_alerts),
                        "critical": len([a for a in all_alerts if a.get("remediation", {}).get("urgency") == "critical"]),
                        "warning": len([a for a in all_alerts if a.get("remediation", {}).get("urgency") == "warning"]),
                    }
                }
                body = json.dumps(response, indent=2, default=str).encode()
                self._respond(200, body, "application/json")
            except Exception as e:
                error_body = json.dumps({"error": str(e)}).encode()
                self._respond(500, error_body, "application/json")
        elif path == "/":
            # Serve the new repositioned dashboard HTML
            html = self._get_dashboard_html()
            self._respond(200, html.encode(), "text/html")
        else:
            self._respond(404, b"not found\n", "text/plain")

    def log_message(self, *args, **kwargs):  # silence default access log
        return


def main() -> None:
    t = threading.Thread(target=_renderer_loop, daemon=True, name="renderer")
    t.start()
    print(f"[server] starting on :{PORT}, render every {INTERVAL}s", flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
