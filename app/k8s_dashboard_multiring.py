#!/usr/bin/env python3
"""Enhanced K8s dashboard with multi-ring resource donuts"""
from flask import Flask, render_template_string, jsonify
from flask_cors import CORS
import json
import requests
import subprocess
from datetime import datetime
from collections import deque
from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
import sys

print("STARTING K8S DASHBOARD V15", file=sys.stderr, flush=True)

app = Flask(__name__)
CORS(app)
metrics_history = deque(maxlen=60)

try:
    config.load_incluster_config()
    v1 = client.CoreV1Api()
    apps_v1 = client.AppsV1Api()
    print("Kubernetes config loaded successfully", file=sys.stderr, flush=True)
except Exception as e:
    print(f"Warning: Could not load K8s config: {e}", file=sys.stderr, flush=True)
    v1 = None
    apps_v1 = None

def parse_resource(value):
    """Parse K8s resource string to numeric value"""
    if not value:
        return 0
    value = str(value).strip()
    if value.endswith('m'):
        return int(value[:-1])
    elif value.endswith('Ki'):
        return int(value[:-2]) * 1024
    elif value.endswith('Mi'):
        return int(value[:-2]) * 1024 * 1024
    elif value.endswith('Gi'):
        return int(value[:-2]) * 1024 * 1024 * 1024
    else:
        return int(float(value)) if value else 0

def get_ollama_pods():
    """Get ollama pods with GPU and LLM info"""
    if not v1:
        return []
    try:
        pods = v1.list_namespaced_pod('ollama')
        ollama_pods = []

        for pod in pods.items:
            status = pod.status.phase
            if status != 'Running':
                continue

            pod_name = pod.metadata.name
            pod_info = {
                'name': pod_name,
                'namespace': pod.metadata.namespace,
                'status': status,
                'gpu': 'none',
                'llms': [],
                'llm_count': '0/0'
            }

            # Try to extract GPU info from environment variables
            for container in pod.spec.containers:
                if container.env:
                    env_vars = {e.name: e.value for e in container.env if e.value}
                    if 'CUDA_VISIBLE_DEVICES' in env_vars:
                        pod_info['gpu'] = f"CUDA GPU {env_vars['CUDA_VISIBLE_DEVICES']}"
                        break
                    elif 'NVIDIA_VISIBLE_DEVICES' in env_vars:
                        pod_info['gpu'] = f"NVIDIA GPU {env_vars['NVIDIA_VISIBLE_DEVICES']}"
                        break

            # If no GPU found in env vars, try to infer from pod name
            if pod_info['gpu'] == 'none':
                if 'a2000' in pod_name:
                    pod_info['gpu'] = "NVIDIA A2000"
                elif '5060ti' in pod_name:
                    pod_info['gpu'] = "NVIDIA RTX 5060 Ti"
                elif '6900xt' in pod_name:
                    pod_info['gpu'] = "AMD RX 6900 XT"
                elif 'gpu0' in pod_name:
                    pod_info['gpu'] = "GPU 0"
                elif 'gpu1' in pod_name:
                    pod_info['gpu'] = "GPU 1"
                elif 'gpu2' in pod_name:
                    pod_info['gpu'] = "GPU 2"
                elif 'cpu' in pod_name:
                    pod_info['gpu'] = "CPU"

            ollama_pods.append(pod_info)
        return ollama_pods
    except Exception as e:
        print(f"Error in get_ollama_pods: {e}")
        import traceback
        traceback.print_exc()
        return []

def get_docker_containers():
    """Get non-K8s Docker containers"""
    try:
        result = subprocess.run(['docker', 'ps', '--format', '{{json .}}'],
                              capture_output=True, text=True, timeout=5)
        containers = []
        if result.returncode == 0:
            for line in result.stdout.strip().split('\n'):
                if line:
                    data = json.loads(line)
                    # Skip k8s containers
                    if 'k8s_' not in data.get('Names', ''):
                        containers.append({
                            'name': data.get('Names', 'unknown'),
                            'image': data.get('Image', 'unknown'),
                            'status': data.get('Status', 'unknown'),
                            'state': 'Running' if 'Up' in data.get('Status', '') else 'Stopped'
                        })
        return containers
    except Exception as e:
        print(f"Docker error: {e}", file=sys.stderr)
        return []

def get_systemd_services():
    """Get custom systemd services (openclaw, cephalobot, kiwix, etc)"""
    try:
        result = subprocess.run(['systemctl', 'list-units', '--type=service', '--all', '--no-pager', '--output=json'],
                              capture_output=True, text=True, timeout=5)
        services = []
        if result.returncode == 0:
            data = json.loads(result.stdout)
            custom_prefixes = ['openclaw-', 'cephalobot-', 'kiwix', 'vaultwarden', 'kraken', 'tesseract', 'grinder']
            for svc in data:
                name = svc.get('unit', '')
                if name.endswith('.service'):
                    service_name = name[:-8]  # Remove .service
                    if any(service_name.startswith(prefix) for prefix in custom_prefixes):
                        services.append({
                            'name': service_name,
                            'state': svc.get('active', 'unknown'),
                            'sub_state': svc.get('sub', 'unknown')
                        })
        return services
    except Exception as e:
        print(f"Systemd error: {e}", file=sys.stderr)
        return []

def get_exposed_services():
    """Get K8s services with external exposure (NodePort, LoadBalancer, Ingress)"""
    if not v1:
        return []
    try:
        services = []
        svc_list = v1.list_service_for_all_namespaces()
        for svc in svc_list.items:
            namespace = svc.metadata.namespace
            name = svc.metadata.name
            svc_type = svc.spec.type
            ports = svc.spec.ports or []

            # Get NodePort and LoadBalancer services
            if svc_type == 'NodePort':
                for port in ports:
                    services.append({
                        'name': f'{namespace}/{name}',
                        'type': 'NodePort',
                        'protocol': port.protocol or 'TCP',
                        'internal_port': port.target_port,
                        'external_port': port.port,
                        'node_port': port.node_port
                    })
            elif svc_type == 'LoadBalancer':
                for port in ports:
                    services.append({
                        'name': f'{namespace}/{name}',
                        'type': 'LoadBalancer',
                        'protocol': port.protocol or 'TCP',
                        'internal_port': port.target_port,
                        'external_port': port.port,
                        'node_port': None
                    })
        return services
    except Exception as e:
        print(f"Error in get_exposed_services: {e}", file=sys.stderr)
        return []

def get_cluster_metrics():
    """Get detailed resource metrics"""
    if not v1:
        return {'error': 'K8s client not available', 'timestamp': datetime.now().isoformat()}
    try:
        nodes = v1.list_node()

        cpu_allocatable = memory_allocatable = 0
        for node in nodes.items:
            alloc = node.status.allocatable or {}
            cpu_allocatable += parse_resource(alloc.get('cpu', '0'))
            memory_allocatable += parse_resource(alloc.get('memory', '0'))

        pods = v1.list_pod_for_all_namespaces()

        cpu_requests = memory_requests = cpu_limits = memory_limits = 0
        pod_count = 0

        for pod in pods.items:
            pod_count += 1
            for container in pod.spec.containers:
                resources = container.resources or {}
                requests = resources.requests or {}
                limits = resources.limits or {}

                cpu_requests += parse_resource(requests.get('cpu', '0'))
                memory_requests += parse_resource(requests.get('memory', '0'))
                cpu_limits += parse_resource(limits.get('cpu', '0'))
                memory_limits += parse_resource(limits.get('memory', '0'))

        cpu_used = memory_used = 0
        pod_capacity = 110

        return {
            'cpu': {
                'usage': cpu_used,
                'requests': cpu_requests,
                'limits': cpu_limits,
                'allocatable': cpu_allocatable,
                'usage_pct': round(cpu_used / cpu_allocatable * 100, 1) if cpu_allocatable > 0 else 0,
                'requests_pct': round(cpu_requests / cpu_allocatable * 100, 1) if cpu_allocatable > 0 else 0,
                'limits_pct': round(cpu_limits / cpu_allocatable * 100, 1) if cpu_allocatable > 0 else 0,
                'warning': cpu_limits > cpu_allocatable
            },
            'memory': {
                'usage': memory_used,
                'requests': memory_requests,
                'limits': memory_limits,
                'allocatable': memory_allocatable,
                'usage_pct': round(memory_used / memory_allocatable * 100, 1) if memory_allocatable > 0 else 0,
                'requests_pct': round(memory_requests / memory_allocatable * 100, 1) if memory_allocatable > 0 else 0,
                'limits_pct': round(memory_limits / memory_allocatable * 100, 1) if memory_allocatable > 0 else 0,
                'warning': memory_limits > memory_allocatable,
                'usage_gb': round(memory_used / (1024**3), 1),
                'requests_gb': round(memory_requests / (1024**3), 1),
                'limits_gb': round(memory_limits / (1024**3), 1),
                'allocatable_gb': round(memory_allocatable / (1024**3), 1)
            },
            'pods': {
                'usage': pod_count,
                'allocatable': pod_capacity,
                'usage_pct': round(pod_count / pod_capacity * 100, 1)
            },
            'timestamp': datetime.now().isoformat()
        }
    except Exception as e:
        return {'error': str(e), 'timestamp': datetime.now().isoformat()}

def get_k8s_alerts():
    """Get K8s events and warnings"""
    if not v1:
        return []
    try:
        events = v1.list_event_for_all_namespaces()

        alerts = []
        for event in events.items:
            if event.type in ['Warning', 'Error']:
                alerts.append({
                    'type': event.type,
                    'reason': event.reason,
                    'message': event.message,
                    'object': event.involved_object.name if event.involved_object else 'unknown',
                    'count': event.count or 1,
                    'timestamp': event.metadata.creation_timestamp.isoformat() if event.metadata.creation_timestamp else ''
                })
        return sorted(alerts, key=lambda x: x['timestamp'], reverse=True)[:10]
    except Exception as e:
        print(f"Error in get_k8s_alerts: {e}")
        import traceback
        traceback.print_exc()
        return []

def get_pods_by_namespace():
    """Get pods by namespace"""
    with open('/tmp/debug.log', 'a') as f:
        f.write('get_pods_by_namespace called\n')
    if not v1:
        with open('/tmp/debug.log', 'a') as f:
            f.write('v1 is None\n')
        return {}
    try:
        with open('/tmp/debug.log', 'a') as f:
            f.write('calling list_pod_for_all_namespaces\n')
        pods = v1.list_pod_for_all_namespaces()
        with open('/tmp/debug.log', 'a') as f:
            f.write(f'got {len(pods.items)} pods\n')
        namespaces = {}

        for pod in pods.items:
            ns = pod.metadata.namespace
            if ns not in namespaces:
                namespaces[ns] = []

            status = pod.status.phase
            container_statuses = pod.status.container_statuses or []
            restarts = sum(c.restart_count or 0 for c in container_statuses)
            ready_count = sum(1 for c in container_statuses if c.ready)
            total_containers = len(container_statuses)

            namespaces[ns].append({
                'name': pod.metadata.name,
                'status': status,
                'restarts': restarts,
                'ready': f"{ready_count}/{total_containers}"
            })
        return namespaces
    except Exception as e:
        print(f"Error in get_pods_by_namespace: {e}")
        import traceback
        traceback.print_exc()
        return {}

@app.route('/')
def dashboard():
    return render_template_string('''<!DOCTYPE html>
<html>
<head>
    <title>K8s Dashboard - NUCUBT01</title>
    <script src="http://cdnjs.cloudflare.com/ajax/libs/Chart.js/3.9.1/chart.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0f0f0f; color: #e0e0e0; }
        header { background: #1a1a1a; padding: 20px; border-bottom: 2px solid #333; }
        header h1 { font-size: 24px; margin-bottom: 5px; }
        .container { max-width: 1600px; margin: 20px auto; padding: 0 20px; }
        .alerts-section { background: #1a1a1a; border-left: 4px solid #dc3545; padding: 15px; margin-bottom: 20px; border-radius: 4px; }
        .alerts-title { font-weight: 600; color: #dc3545; margin-bottom: 10px; font-size: 14px; }
        .alert-item { background: #2a2a2a; padding: 10px; margin-bottom: 8px; border-radius: 3px; font-size: 11px; border-left: 2px solid #dc3545; }
        .metrics-row { display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; margin-bottom: 20px; }
        .metric-card { background: #1a1a1a; padding: 20px; border-radius: 6px; border: 1px solid #333; text-align: center; }
        .metric-title { font-size: 12px; color: #888; margin-bottom: 10px; text-transform: uppercase; font-weight: 600; }
        .metric-donut { position: relative; width: 140px; height: 140px; margin: 0 auto 15px; }
        .metric-center { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); text-align: center; font-size: 11px; }
        .metric-legend { font-size: 10px; text-align: left; margin-top: 10px; }
        .legend-item { display: flex; align-items: center; margin-bottom: 4px; }
        .legend-color { width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; }
        .warning { color: #ffc107; font-size: 9px; margin-top: 8px; }
        .trends-section { background: #1a1a1a; padding: 20px; border-radius: 6px; border: 1px solid #333; margin-bottom: 20px; height: 400px; display: flex; flex-direction: column; }
        .chart-container { position: relative; flex: 1; min-height: 0; }
        .namespace { margin-bottom: 30px; }
        .namespace-title { background: #1a1a1a; padding: 12px 15px; border-left: 4px solid #007bff; margin-bottom: 10px; font-weight: 600; font-size: 13px; border-radius: 4px; }
        .pods-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px; }
        .pod-card { background: #1a1a1a; border: 1px solid #333; border-radius: 4px; padding: 12px; font-size: 11px; }
        .pod-name { font-weight: 600; margin-bottom: 6px; word-break: break-word; }
        .pod-status { display: inline-block; padding: 3px 6px; border-radius: 2px; font-size: 10px; }
        .pod-meta { margin-top: 8px; padding-top: 8px; border-top: 1px solid #333; font-size: 10px; }
        .status-Running { background: #28a745; color: white; }
        .status-Pending { background: #ffc107; color: #000; }
        .status-Failed { background: #dc3545; color: white; }
        .refresh-time { text-align: right; color: #666; font-size: 10px; margin-top: 20px; }
        .endpoints { text-align: right; margin-top: 20px; }
        .endpoints a { color: #2196f3; text-decoration: none; margin-left: 15px; font-size: 11px; }
        .endpoints a:hover { text-decoration: underline; }
        .alert-item { cursor: pointer; }
        .alert-item:hover { background: #3a3a3a !important; }
        .modal { display: none; position: fixed; z-index: 1000; left: 0; top: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.5); }
        .modal-content { background: #1a1a1a; margin: 10% auto; padding: 20px; border: 1px solid #333; border-radius: 6px; width: 600px; max-height: 80vh; overflow-y: auto; }
        .modal-close { color: #888; float: right; font-size: 24px; font-weight: bold; cursor: pointer; }
        .modal-close:hover { color: #ccc; }
        .modal-title { font-size: 16px; font-weight: 600; color: #fff; margin-bottom: 15px; }
        .modal-detail { margin-bottom: 10px; font-size: 11px; color: #ddd; }
        .modal-detail-key { color: #888; font-weight: 600; }
        .ollama-section { background: #1a1a1a; padding: 15px; border-left: 4px solid #ff9800; margin-bottom: 20px; border-radius: 4px; }
        .ollama-title { font-size: 14px; font-weight: 600; color: #ff9800; margin-bottom: 15px; }
        .ollama-pod { background: #252525; padding: 12px; margin-bottom: 10px; border-radius: 4px; border: 1px solid #333; }
        .docker-section { background: #1a1a1a; padding: 15px; border-left: 4px solid #0db7ed; margin-bottom: 20px; border-radius: 4px; }
        .docker-title { font-size: 14px; font-weight: 600; color: #0db7ed; margin-bottom: 15px; }
        .docker-container { background: #252525; padding: 12px; margin-bottom: 8px; border-radius: 4px; border: 1px solid #333; }
        .systemd-section { background: #1a1a1a; padding: 15px; border-left: 4px solid #8b5cf6; margin-bottom: 20px; border-radius: 4px; }
        .systemd-title { font-size: 14px; font-weight: 600; color: #8b5cf6; margin-bottom: 15px; }
        .systemd-service { background: #252525; padding: 12px; margin-bottom: 8px; border-radius: 4px; border: 1px solid #333; }
        .service-state-active { color: #28a745; font-weight: 600; }
        .service-state-inactive { color: #dc3545; font-weight: 600; }
    </style>
</head>
<body>
    <header>
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <div>
                <h1>⚙️ Kubernetes Dashboard <span style='font-size: 14px; color: #888; font-weight: 400; margin-left: 10px;'>v15.2</span></h1>
                <p>Real-time cluster overview - NUCUBT01 K8s</p>
            </div>
            <button id="pauseBtn" style="padding: 10px 20px; font-size: 14px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer;">⏸ Pause</button>
        </div>
    </header>
    <div class="container">
        <div id="alerts-container"></div>
        <div id="alertModal" class="modal">
            <div class="modal-content">
                <span class="modal-close" onclick="document.getElementById('alertModal').style.display='none'">&times;</span>
                <div class="modal-title" id="modalTitle"></div>
                <div id="modalDetails"></div>
            </div>
        </div>
        <div class="metrics-row">
            <div class="metric-card">
                <div class="metric-title">CPU</div>
                <div class="metric-donut"><canvas id="cpuChart" width="140" height="140"></canvas></div>
                <div class="metric-legend" id="cpu-legend"></div>
            </div>
            <div class="metric-card">
                <div class="metric-title">Memory</div>
                <div class="metric-donut"><canvas id="memoryChart" width="140" height="140"></canvas></div>
                <div class="metric-legend" id="memory-legend"></div>
            </div>
            <div class="metric-card">
                <div class="metric-title">Pods</div>
                <div class="metric-donut"><canvas id="podsChart" width="140" height="140"></canvas></div>
                <div class="metric-legend" id="pods-legend"></div>
            </div>
        </div>
        <div class="trends-section">
            <div style="font-size: 14px; font-weight: 600; margin-bottom: 15px;">📊 Trends (5 min)</div>
            <div class="chart-container"><canvas id="trendsChart" width="800" height="200"></canvas></div>
        </div>
        <div id="dashboard"></div>
        <div id="ollama-container"></div>
        <div id="docker-container"></div>
        <div id="systemd-container"></div>
        <div id="ports-container"></div>
        <div class="refresh-time">Updated: <span id="refresh-time">--:--:--</span></div>
        <div class="endpoints"><a href="/api/pods" target="_blank">pods</a><a href="/api/metrics" target="_blank">metrics</a><a href="/api/alerts" target="_blank">alerts</a><a href="/api/ollama" target="_blank">ollama</a><a href="/api/docker" target="_blank">docker</a><a href="/api/systemd" target="_blank">systemd</a></div>
    </div>
    <script>
        const charts = {};

        function createMultiRingChart(id, rings) {
            const canvas = document.getElementById(id);
            if (!canvas) return null;
            const ctx = canvas.getContext('2d');
            if (!ctx) return null;

            try {
                const chart = new Chart(ctx, {
                    type: 'doughnut',
                    data: {
                        datasets: rings.map(r => ({
                            data: [r.value, 100 - r.value],
                            backgroundColor: [r.color, '#444'],
                            borderColor: '#0f0f0f',
                            borderWidth: 0
                        }))
                    },
                    options: {
                        responsive: false,
                        maintainAspectRatio: false,
                        rotation: 0,
                        circumference: 360,
                        cutout: '60%',
                        plugins: {
                            legend: { display: false },
                            tooltip: { enabled: false }
                        }
                    }
                });
                return chart;
            } catch(e) {
                console.error('Chart error:', e);
                return null;
            }
        }

        function createTrendsChart() {
            const canvas = document.getElementById('trendsChart');
            if (!canvas) return null;
            const ctx = canvas.getContext('2d');
            if (!ctx) return null;

            try {
                return new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: [],
                        datasets: [
                            { label: 'CPU %', data: [], borderColor: '#ff9800', borderWidth: 2, tension: 0.3, fill: false },
                            { label: 'Memory %', data: [], borderColor: '#2196f3', borderWidth: 2, tension: 0.3, fill: false },
                            { label: 'Pods', data: [], borderColor: '#4caf50', borderWidth: 2, tension: 0.3, fill: false }
                        ]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        scales: {
                            y: { beginAtZero: true, max: 100, grid: { color: '#222' }, ticks: { color: '#888' } },
                            x: { grid: { color: '#222' }, ticks: { color: '#888', maxRotation: 45, minRotation: 0 } }
                        },
                        plugins: {
                            legend: { display: true, labels: { color: '#888', font: { size: 12 }, padding: 15 } }
                        }
                    }
                });
            } catch(e) {
                console.error('Trends chart error:', e);
                return null;
            }
        }

        function renderMetrics(data) {
            if (data.error) return;

            charts.cpu = createMultiRingChart('cpuChart', [
                { value: Math.min(data.cpu.usage_pct, 100), color: '#e91e63' },
                { value: Math.min(data.cpu.requests_pct, 100), color: '#4caf50' },
                { value: Math.min(data.cpu.limits_pct, 100), color: '#2196f3' }
            ]);

            document.getElementById('cpu-legend').innerHTML = `
                <div class="legend-item"><span class="legend-color" style="background: #e91e63;"></span>Usage: ${data.cpu.usage}</div>
                <div class="legend-item"><span class="legend-color" style="background: #4caf50;"></span>Requests: ${data.cpu.requests}</div>
                <div class="legend-item"><span class="legend-color" style="background: #2196f3;"></span>Limits: ${data.cpu.limits}</div>
            `;

            charts.memory = createMultiRingChart('memoryChart', [
                { value: Math.min(data.memory.usage_pct, 100), color: '#e91e63' },
                { value: Math.min(data.memory.requests_pct, 100), color: '#4caf50' },
                { value: Math.min(data.memory.limits_pct, 100), color: '#2196f3' }
            ]);

            document.getElementById('memory-legend').innerHTML = `
                <div class="legend-item"><span class="legend-color" style="background: #e91e63;"></span>Usage: ${data.memory.usage_gb}GB</div>
                <div class="legend-item"><span class="legend-color" style="background: #4caf50;"></span>Requests: ${data.memory.requests_gb}GB</div>
                <div class="legend-item"><span class="legend-color" style="background: #2196f3;"></span>Limits: ${data.memory.limits_gb}GB</div>
            `;

            charts.pods = createMultiRingChart('podsChart', [
                { value: data.pods.usage_pct, color: '#4caf50' }
            ]);

            document.getElementById('pods-legend').innerHTML = `
                <div class="legend-item"><span class="legend-color" style="background: #4caf50;"></span>Usage: ${data.pods.usage} / ${data.pods.allocatable}</div>
            `;

            if (charts.trends.data.labels.length < 12) {
                charts.trends.data.labels.push(new Date(data.timestamp).toLocaleTimeString());
                charts.trends.data.datasets[0].data.push(data.cpu.usage_pct);
                charts.trends.data.datasets[1].data.push(data.memory.usage_pct);
                charts.trends.data.datasets[2].data.push(data.pods.usage);
            } else {
                charts.trends.data.labels.shift();
                charts.trends.data.datasets.forEach(ds => ds.data.shift());
                charts.trends.data.labels.push(new Date(data.timestamp).toLocaleTimeString());
                charts.trends.data.datasets[0].data.push(data.cpu.usage_pct);
                charts.trends.data.datasets[1].data.push(data.memory.usage_pct);
                charts.trends.data.datasets[2].data.push(data.pods.usage);
            }
            charts.trends.update();
        }

        function refresh() {
            Promise.all([
                fetch('/api/metrics').then(r=>r.json()).catch(e => ({error: e.message})),
                fetch('/api/alerts').then(r=>r.json()).catch(e => []),
                fetch('/api/pods').then(r=>r.json()).catch(e => ({})),
                fetch('/api/ollama').then(r=>r.json()).catch(e => []),
                fetch('/api/docker').then(r=>r.json()).catch(e => []),
                fetch('/api/systemd').then(r=>r.json()).catch(e => []),
                fetch('/api/exposed-services').then(r=>r.json()).catch(e => [])
            ])
            .then(([m, a, p, ollama, docker, systemd, ports]) => {
                if (m && !m.error) renderMetrics(m);
                let alerts = (a && a.length > 0) ? '<div class="alerts-section"><div class="alerts-title">⚠️ Warnings: ' + a.length + '</div>' : '';
                if (a && a.forEach) a.forEach((al, idx) => {
                    alerts += '<div class="alert-item" onclick="showAlert(' + idx + ')"><span class="alert-type">' + al.type + '</span> ' + al.reason + '</div>';
                });
                if (a && a.length > 0) alerts += '</div>';
                document.getElementById('alerts-container').innerHTML = alerts;
                window.alertsData = a;

                let pods = '';
                if (p) {
                    Object.keys(p).sort().forEach(ns => {
                        if (ns !== 'error' && p[ns]) {
                            pods += '<div class="namespace"><div class="namespace-title">' + ns + ' (' + p[ns].length + ' pods)</div><div class="pods-grid">';
                            p[ns].forEach(pod => {
                                let podHtml = '<div class="pod-card"><div class="pod-name">' + pod.name + '</div><div class="status-' + pod.status + ' pod-status">' + pod.status + '</div><div style="margin-top: 6px; color: #888; font-size: 10px;">Ready: ' + pod.ready + ' | Restarts: ' + pod.restarts + '</div></div>';
                                pods += podHtml;
                            });
                            pods += '</div></div>';
                        }
                    });
                }
                document.getElementById('dashboard').innerHTML = pods;

                let ollamaHtml = '<div class="ollama-section"><div class="ollama-title">🦙 Ollama Pods (' + (ollama ? ollama.length : 0) + ')</div>';
                if (ollama && ollama.length > 0) {
                    ollama.forEach((pod, idx) => {
                        const dropdownId = 'models-' + idx;
                        const modelCount = pod.model_count ? pod.model_count : 0;
                        let modelsHtml = '';
                        if (pod.models && pod.models.length > 0) {
                            pod.models.forEach(model => {
                                modelsHtml += '<div style="color: #4caf50; padding: 4px 0;">• ' + model + '</div>';
                            });
                        } else {
                            modelsHtml = '<div style="color: #666;">No models loaded</div>';
                        }
                        ollamaHtml += '<div class="ollama-pod"><div style="display: flex; justify-content: space-between; align-items: center;"><div><div style="font-weight: 600;">' + pod.name + '</div><div style="color: #888; font-size: 10px;">GPU: ' + pod.gpu + ' | Port: ' + pod.port + '</div></div><button onclick="document.getElementById(\'' + dropdownId + '\').style.display = document.getElementById(\'' + dropdownId + '\').style.display === \'none\' ? \'block\' : \'none\'" style="padding: 4px 8px; background: #333; color: #fff; border: 1px solid #555; border-radius: 3px; cursor: pointer; font-size: 11px;">Models (' + modelCount + ') ▼</button></div><div id="' + dropdownId + '" style="display: none; margin-top: 10px; padding-top: 10px; border-top: 1px solid #333; font-size: 10px; max-height: 200px; overflow-y: auto;">' + modelsHtml + '</div></div>';
                    });
                } else {
                    ollamaHtml += '<div style="color: #888; font-size: 12px; padding: 8px;">No Ollama pods running</div>';
                }
                ollamaHtml += '</div>';
                document.getElementById('ollama-container').innerHTML = ollamaHtml;

                let dockerHtml = '';
                if (docker && docker.length > 0) {
                    dockerHtml = '<div class="docker-section"><div class="docker-title">🐳 Docker Containers (' + docker.length + ')</div>';
                    docker.forEach(c => {
                        dockerHtml += '<div class="docker-container"><div style="font-weight: 600;">' + c.name + '</div><div style="color: #888; font-size: 10px;">Image: ' + c.image + '</div><div style="color: ' + (c.state === 'Running' ? '#28a745' : '#dc3545') + '; font-size: 10px;">' + c.state + ' - ' + c.status + '</div></div>';
                    });
                    dockerHtml += '</div>';
                }
                document.getElementById('docker-container').innerHTML = dockerHtml;

                let systemdHtml = '';
                if (systemd && systemd.length > 0) {
                    systemdHtml = '<div class="systemd-section"><div class="systemd-title">⚙️ Systemd Services (' + systemd.length + ')</div>';
                    systemd.forEach(s => {
                        let stateClass = s.state === 'active' ? 'service-state-active' : 'service-state-inactive';
                        systemdHtml += '<div class="systemd-service"><div style="font-weight: 600;">' + s.name + '</div><div class="' + stateClass + '">' + s.state + ' (' + s.sub_state + ')</div></div>';
                    });
                    systemdHtml += '</div>';
                }
                document.getElementById('systemd-container').innerHTML = systemdHtml;

                let portsHtml = '<div style="background: #1a1a1a; padding: 15px; border-left: 4px solid #2196f3; margin-bottom: 20px; border-radius: 4px;"><div style="font-size: 14px; font-weight: 600; color: #2196f3; margin-bottom: 15px;">🌐 Exposed Services (' + (ports ? ports.length : 0) + ')</div>';
                if (ports && ports.length > 0) {
                    ports.forEach(p => {
                        const externalUrl = 'http://10.0.9.99:' + p.node_port;
                        portsHtml += '<div style="background: #252525; padding: 12px; margin-bottom: 8px; border-radius: 4px; border: 1px solid #333;"><div style="font-weight: 600;">' + p.name + '</div><div style="font-size: 11px; color: #2196f3;"><a href="' + externalUrl + '" target="_blank" style="color: #2196f3; text-decoration: none;">' + externalUrl + '</a></div><div style="font-size: 11px; color: #888;">Internal: ' + p.internal_port + '</div></div>';
                    });
                } else {
                    portsHtml += '<div style="color: #888; font-size: 12px; padding: 8px;">No exposed services</div>';
                }
                portsHtml += '</div>';
                document.getElementById('ports-container').innerHTML = portsHtml;

                document.getElementById('refresh-time').textContent = new Date().toLocaleTimeString();
            })
            .catch(err => console.error('Refresh error:', err));
        }

        function showAlert(idx) {
            if (!window.alertsData || !window.alertsData[idx]) return;
            const al = window.alertsData[idx];
            document.getElementById('modalTitle').textContent = al.type + ': ' + al.reason;
            let details = '';
            details += '<div class="modal-detail"><span class="modal-detail-key">Object:</span> ' + al.object + '</div>';
            details += '<div class="modal-detail"><span class="modal-detail-key">Count:</span> ' + al.count + '</div>';
            details += '<div class="modal-detail"><span class="modal-detail-key">Timestamp:</span> ' + al.timestamp + '</div>';
            details += '<div class="modal-detail"><span class="modal-detail-key">Message:</span> ' + al.message + '</div>';
            document.getElementById('modalDetails').innerHTML = details;
            document.getElementById('alertModal').style.display = 'block';
        }

        charts.trends = createTrendsChart();

        let isPaused = false;
        let refreshInterval;

        function startRefresh() {
            refresh();
            refreshInterval = setInterval(refresh, 5000);
        }

        function pauseRefresh() {
            isPaused = true;
            clearInterval(refreshInterval);
            document.getElementById('pauseBtn').textContent = '▶ Resume';
            document.getElementById('pauseBtn').style.background = '#6c757d';
        }

        function resumeRefresh() {
            isPaused = false;
            startRefresh();
            document.getElementById('pauseBtn').textContent = '⏸ Pause';
            document.getElementById('pauseBtn').style.background = '#007bff';
        }

        document.getElementById('pauseBtn').addEventListener('click', () => {
            if (isPaused) {
                resumeRefresh();
            } else {
                pauseRefresh();
            }
        });

        window.onclick = function(e) {
            const modal = document.getElementById('alertModal');
            if (e.target === modal) modal.style.display = 'none';
        }

        startRefresh();
    </script>
</body>
</html>''')

@app.route('/api/metrics')
def api_metrics():
    return jsonify(get_cluster_metrics())

@app.route('/api/alerts')
def api_alerts():
    return jsonify(get_k8s_alerts())

@app.route('/api/pods')
def api_pods():
    with open('/tmp/debug.log', 'a') as f:
        f.write('/api/pods route called\n')
        f.flush()
    result = get_pods_by_namespace()
    with open('/tmp/debug.log', 'a') as f:
        f.write(f'result: {result}\n')
        f.flush()
    return jsonify(result)

@app.route('/api/ollama')
def api_ollama():
    return jsonify(get_ollama_pods())

@app.route('/api/docker')
def api_docker():
    return jsonify(get_docker_containers())

@app.route('/api/systemd')
def api_systemd():
    return jsonify(get_systemd_services())

@app.route('/api/exposed-services')
def api_exposed_services():
    return jsonify(get_exposed_services())

@app.route("/health")
def health():
    return "ok", 200

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=False)
