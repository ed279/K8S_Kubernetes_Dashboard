#!/usr/bin/env python3
"""Enhanced K8s dashboard with multi-ring resource donuts"""
from flask import Flask, render_template_string, jsonify
from flask_cors import CORS
import subprocess
import json
import requests
from datetime import datetime
from collections import deque

app = Flask(__name__)
CORS(app)
metrics_history = deque(maxlen=60)

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
    try:
        result = subprocess.run(['kubectl', 'get', 'pods', '-n', 'ollama', '-o', 'json'],
                              capture_output=True, text=True, timeout=10)
        pods_data = json.loads(result.stdout) if result.stdout else {'items': []}

        ollama_pods = []
        for pod in pods_data.get('items', []):
            status = pod['status']['phase']
            # Only include Running pods
            if status != 'Running':
                continue

            pod_name = pod['metadata']['name']

            # Get model count from ollama pod
            model_count = '0/0'
            try:
                result = subprocess.run(['kubectl', 'exec', '-n', 'ollama', pod_name, '--', 'ollama', 'list'],
                                      capture_output=True, text=True, timeout=5)
                if result.returncode == 0:
                    lines = result.stdout.strip().split('\n')[1:]  # Skip header
                    count = len([l for l in lines if l.strip()])
                    model_count = f'{count}/{count}'
            except:
                pass

            pod_info = {
                'name': pod_name,
                'namespace': pod['metadata']['namespace'],
                'status': status,
                'gpu': 'none',
                'llms': [],
                'llm_count': model_count
            }

            # Try to extract GPU info from environment variables
            for container in pod['spec'].get('containers', []):
                env_vars = {e['name']: e.get('value', '') for e in container.get('env', [])}
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
        return []

def get_cluster_metrics():
    """Get detailed resource metrics"""
    try:
        # Get node allocatable
        result = subprocess.run(['kubectl', 'get', 'nodes', '-o', 'json'],
                              capture_output=True, text=True, timeout=10)
        nodes_data = json.loads(result.stdout) if result.stdout else {'items': []}

        cpu_allocatable = memory_allocatable = 0
        for node in nodes_data.get('items', []):
            alloc = node['status'].get('allocatable', {})
            cpu_allocatable += parse_resource(alloc.get('cpu', '0'))
            memory_allocatable += parse_resource(alloc.get('memory', '0'))

        # Get usage from metrics
        result = subprocess.run(['kubectl', 'top', 'nodes', '-o', 'json'],
                              capture_output=True, text=True, timeout=10)
        nodes_metrics = json.loads(result.stdout) if result.stdout else {'items': []}

        cpu_used = memory_used = 0
        for node in nodes_metrics.get('items', []):
            usage = node.get('usage', {})
            cpu_used += parse_resource(usage.get('cpu', '0m'))
            memory_used += parse_resource(usage.get('memory', '0Mi'))

        # Get pod requests and limits
        result = subprocess.run(['kubectl', 'get', 'pods', '-A', '-o', 'json'],
                              capture_output=True, text=True, timeout=10)
        pods_data = json.loads(result.stdout) if result.stdout else {'items': []}

        cpu_requests = memory_requests = cpu_limits = memory_limits = 0
        pod_count = 0

        for pod in pods_data.get('items', []):
            pod_count += 1
            for container in pod['spec'].get('containers', []):
                resources = container.get('resources', {})
                requests = resources.get('requests', {})
                limits = resources.get('limits', {})

                cpu_requests += parse_resource(requests.get('cpu', '0'))
                memory_requests += parse_resource(requests.get('memory', '0'))
                cpu_limits += parse_resource(limits.get('cpu', '0'))
                memory_limits += parse_resource(limits.get('memory', '0'))

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
    try:
        result = subprocess.run(['kubectl', 'get', 'events', '-A', '--sort-by=.metadata.creationTimestamp', '-o', 'json'],
                              capture_output=True, text=True, timeout=10)
        data = json.loads(result.stdout) if result.stdout else {'items': []}

        alerts = []
        for event in data.get('items', [])[-30:]:
            if event.get('type') in ['Warning', 'Error']:
                alerts.append({
                    'type': event.get('type'),
                    'reason': event.get('reason'),
                    'message': event.get('message'),
                    'object': event.get('involvedObject', {}).get('name'),
                    'count': event.get('count', 1),
                    'timestamp': event.get('metadata', {}).get('creationTimestamp')
                })
        return sorted(alerts, key=lambda x: x['timestamp'], reverse=True)[:10]
    except:
        return []

def get_pods_by_namespace():
    """Get pods by namespace"""
    try:
        result = subprocess.run(['kubectl', 'get', 'pods', '-A', '-o', 'json'],
                              capture_output=True, text=True, timeout=10)
        data = json.loads(result.stdout)
        namespaces = {}

        for pod in data.get('items', []):
            ns = pod['metadata']['namespace']
            if ns not in namespaces:
                namespaces[ns] = []

            status = pod['status']['phase']
            restarts = sum(c.get('restartCount', 0) for c in pod['status'].get('containerStatuses', []))

            namespaces[ns].append({
                'name': pod['metadata']['name'],
                'status': status,
                'restarts': restarts,
                'ready': f"{sum(1 for c in pod['status'].get('containerStatuses', []) if c.get('ready', False))}/{len(pod['status'].get('containerStatuses', []))}"
            })
        return namespaces
    except:
        return {}

@app.route('/')
def dashboard():
    return render_template_string('''<!DOCTYPE html>
<html>
<head>
    <title>K8s Dashboard - NUCUBT01</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/3.9.1/chart.min.js"></script>
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
        .trends-section { background: #1a1a1a; padding: 20px; border-radius: 6px; border: 1px solid #333; margin-bottom: 20px; }
        .chart-container { position: relative; height: 200px; }
        .namespace { margin-bottom: 30px; }
        .namespace-title { background: #1a1a1a; padding: 12px 15px; border-left: 4px solid #007bff; margin-bottom: 10px; font-weight: 600; font-size: 13px; border-radius: 4px; }
        .pods-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px; }
        .pod-card { background: #1a1a1a; border: 1px solid #333; border-radius: 4px; padding: 12px; font-size: 11px; }
        .pod-name { font-weight: 600; margin-bottom: 6px; word-break: break-word; }
        .pod-status { display: inline-block; padding: 3px 6px; border-radius: 2px; font-size: 10px; }
        .pod-meta { margin-top: 8px; padding-top: 8px; border-top: 1px solid #333; font-size: 10px; }
        .pod-gpu { color: #ff9800; margin-bottom: 4px; }
        .pod-llm-header { cursor: pointer; user-select: none; color: #aaa; margin-bottom: 4px; display: flex; align-items: center; }
        .pod-llm-toggle { display: inline-block; width: 12px; margin-right: 6px; transition: transform 0.2s; }
        .pod-llm-toggle.open { transform: rotate(90deg); }
        .pod-llm-list { display: none; margin-top: 6px; padding-left: 16px; max-height: 150px; overflow-y: auto; }
        .pod-llm-list.open { display: block; }
        .pod-llm-item { color: #ccc; padding: 2px 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .status-Running { background: #28a745; color: white; }
        .status-Pending { background: #ffc107; color: #000; }
        .status-Failed { background: #dc3545; color: white; }
        .refresh-time { text-align: right; color: #666; font-size: 10px; margin-top: 20px; }
        .ollama-section { background: #1a1a1a; padding: 15px; border-left: 4px solid #ff9800; margin-bottom: 20px; border-radius: 4px; }
        .ollama-title { font-size: 14px; font-weight: 600; color: #ff9800; margin-bottom: 15px; }
        .ollama-pod { background: #252525; padding: 12px; margin-bottom: 10px; border-radius: 4px; border: 1px solid #333; }
        .ollama-pod-header { display: flex; align-items: center; cursor: pointer; user-select: none; }
        .ollama-expand { width: 20px; height: 20px; display: flex; align-items: center; justify-content: center; font-size: 14px; color: #888; font-weight: 600; margin-right: 10px; transition: transform 0.2s; }
        .ollama-expand.open { transform: rotate(90deg); }
        .ollama-name { flex: 1; font-weight: 600; font-size: 12px; }
        .ollama-gpu { display: inline-block; padding: 2px 6px; background: #444; border-radius: 2px; font-size: 10px; color: #aaa; margin: 0 6px; }
        .ollama-status { display: inline-block; padding: 2px 6px; border-radius: 2px; font-size: 10px; background: #28a745; color: white; }
        .ollama-content { display: none; padding: 10px 0 0 20px; margin-top: 10px; border-top: 1px solid #333; }
        .ollama-content.open { display: block; }
        .ollama-llm { font-size: 11px; color: #ccc; padding: 4px 0; }
        .ollama-counter { font-size: 10px; color: #888; margin-top: 6px; }
    </style>
</head>
<body>
    <header>
        <h1>⚙️ Kubernetes Dashboard</h1>
        <p>Real-time cluster overview - NUCUBT01 K8s</p>
    </header>
    <div class="container">
        <div id="alerts-container"></div>
        <div class="metrics-row">
            <div class="metric-card">
                <div class="metric-title">CPU</div>
                <div class="metric-donut"><canvas id="cpuChart"></canvas></div>
                <div class="metric-legend" id="cpu-legend"></div>
            </div>
            <div class="metric-card">
                <div class="metric-title">Memory</div>
                <div class="metric-donut"><canvas id="memoryChart"></canvas></div>
                <div class="metric-legend" id="memory-legend"></div>
            </div>
            <div class="metric-card">
                <div class="metric-title">Pods</div>
                <div class="metric-donut"><canvas id="podsChart"></canvas></div>
                <div class="metric-legend" id="pods-legend"></div>
            </div>
        </div>
        <div class="trends-section">
            <div style="font-size: 14px; font-weight: 600; margin-bottom: 15px;">📊 Trends (5 min)</div>
            <div class="chart-container"><canvas id="trendsChart"></canvas></div>
        </div>
        <div id="dashboard"></div>
        <div class="refresh-time">Updated: <span id="refresh-time">--:--:--</span></div>
    </div>
    <script>
        const charts = {};

        function createMultiRingChart(id, rings) {
            const ctx = document.getElementById(id).getContext('2d');
            return new Chart(ctx, {
                type: 'doughnut',
                data: {
                    datasets: rings.map(r => ({
                        data: [r.value, 100 - r.value],
                        backgroundColor: [r.color, '#333'],
                        borderColor: '#1a1a1a',
                        borderWidth: 2,
                        borderRadius: 0
                    }))
                },
                options: {
                    responsive: true,
                    circumference: 360,
                    rotation: 0,
                    plugins: { legend: { display: false }, tooltip: { enabled: false } },
                    cutout: ['75%', '60%', '45%']
                }
            });
        }

        function createTrendsChart() {
            return new Chart(document.getElementById('trendsChart').getContext('2d'), {
                type: 'line',
                data: { labels: [], datasets: [
                    { label: 'CPU %', data: [], borderColor: '#ff9800', borderWidth: 2, tension: 0.1 },
                    { label: 'Memory %', data: [], borderColor: '#2196f3', borderWidth: 2, tension: 0.1 },
                    { label: 'Pods', data: [], borderColor: '#4caf50', borderWidth: 2, tension: 0.1, yAxisID: 'y1' }
                ]},
                options: {
                    responsive: true, maintainAspectRatio: false,
                    scales: {
                        y: { beginAtZero: true, max: 100, grid: { color: '#333' } },
                        y1: { type: 'linear', position: 'right', max: 120, grid: { drawOnChartArea: false } },
                        x: { grid: { color: '#333' } }
                    }
                }
            });
        }

        function renderMetrics(data) {
            if (data.error) return;

            // CPU rings
            charts.cpu = createMultiRingChart('cpuChart', [
                { value: Math.min(data.cpu.usage_pct, 100), color: '#e91e63' },
                { value: Math.min(data.cpu.requests_pct, 100), color: '#4caf50' },
                { value: Math.min(data.cpu.limits_pct, 100), color: '#2196f3' }
            ]);

            document.getElementById('cpu-legend').innerHTML = `
                <div class="legend-item"><span class="legend-color" style="background: #e91e63;"></span>Usage: ${data.cpu.usage}</div>
                <div class="legend-item"><span class="legend-color" style="background: #4caf50;"></span>Requests: ${data.cpu.requests}</div>
                <div class="legend-item"><span class="legend-color" style="background: #2196f3;"></span>Limits: ${data.cpu.limits}</div>
                <div class="legend-item"><span style="color: #888;">Allocatable: ${data.cpu.allocatable}</span></div>
                ${data.cpu.warning ? '<div class="warning">⚠️ Limits > Capacity</div>' : ''}
            `;

            // Memory rings
            charts.memory = createMultiRingChart('memoryChart', [
                { value: Math.min(data.memory.usage_pct, 100), color: '#e91e63' },
                { value: Math.min(data.memory.requests_pct, 100), color: '#4caf50' },
                { value: Math.min(data.memory.limits_pct, 100), color: '#2196f3' }
            ]);

            document.getElementById('memory-legend').innerHTML = `
                <div class="legend-item"><span class="legend-color" style="background: #e91e63;"></span>Usage: ${data.memory.usage_gb}GB</div>
                <div class="legend-item"><span class="legend-color" style="background: #4caf50;"></span>Requests: ${data.memory.requests_gb}GB</div>
                <div class="legend-item"><span class="legend-color" style="background: #2196f3;"></span>Limits: ${data.memory.limits_gb}GB</div>
                <div class="legend-item"><span style="color: #888;">Allocatable: ${data.memory.allocatable_gb}GB</span></div>
                ${data.memory.warning ? '<div class="warning">⚠️ Limits > Capacity</div>' : ''}
            `;

            // Pods
            charts.pods = createMultiRingChart('podsChart', [
                { value: data.pods.usage_pct, color: '#4caf50' }
            ]);

            document.getElementById('pods-legend').innerHTML = `
                <div class="legend-item"><span class="legend-color" style="background: #4caf50;"></span>Usage: ${data.pods.usage}</div>
                <div class="legend-item"><span style="color: #888;">Allocatable Capacity: ${data.pods.allocatable}</span></div>
            `;

            // Trends
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

        const ollamaPodData = {};

        function attachLLMHandlers() {
            document.querySelectorAll('.pod-llm-header').forEach(el => {
                el.addEventListener('click', function(e) {
                    e.stopPropagation();
                    const toggle = this.querySelector('.pod-llm-toggle');
                    const list = this.nextElementSibling;
                    toggle.classList.toggle('open');
                    list.classList.toggle('open');
                    if (list.classList.contains('open') && !list.hasAttribute('data-loaded')) {
                        const podName = this.closest('.pod-card').getAttribute('data-pod-name');
                        fetch('/api/ollama/' + podName)
                            .then(r => r.json())
                            .then(data => {
                                let llmHtml = '';
                                if (data.models && data.models.length > 0) {
                                    data.models.forEach(m => { llmHtml += '<div class="pod-llm-item">• ' + m + '</div>'; });
                                } else {
                                    llmHtml += '<div class="pod-llm-item" style="color: #888;">No models loaded</div>';
                                }
                                list.innerHTML = llmHtml;
                                list.setAttribute('data-loaded', 'true');
                            })
                            .catch(err => { list.innerHTML = '<div style="color: #f00;">Error loading</div>'; });
                    }
                });
            });
        }

        function refresh() {
            Promise.all([
                fetch('/api/metrics').then(r=>r.json()).catch(e => ({error: e.message})),
                fetch('/api/alerts').then(r=>r.json()).catch(e => []),
                fetch('/api/pods').then(r=>r.json()).catch(e => ({})),
                fetch('/api/ollama').then(r=>r.json()).catch(e => [])
            ])
            .then(([m, a, p, o]) => {
                if (m && !m.error) renderMetrics(m);
                let alerts = (a && a.length > 0) ? '<div class="alerts-section"><div class="alerts-title">⚠️ Warnings: ' + a.length + '</div>' : '';
                if (a && a.forEach) a.forEach(al => { alerts += '<div class="alert-item"><span class="alert-type">' + al.type + '</span> ' + al.reason + '</div>'; });
                if (a && a.length > 0) alerts += '</div>';
                document.getElementById('alerts-container').innerHTML = alerts;

                // Build ollama pod map for quick lookup
                if (o && o.forEach) o.forEach(op => { ollamaPodData[op.name] = op; });

                let pods = '';
                if (p) {
                    Object.keys(p).sort().forEach(ns => {
                        if (ns !== 'error' && p[ns]) {
                            pods += '<div class="namespace"><div class="namespace-title">' + ns + ' (' + p[ns].length + ' pods)</div><div class="pods-grid">';
                            p[ns].forEach(pod => {
                                const ollama = ollamaPodData[pod.name];
                                const isOllamaPod = (ns === 'ollama') || !!ollama;
                                let podHtml = '<div class="pod-card" data-pod-name="' + pod.name + '"><div class="pod-name">' + pod.name + '</div><div class="status-' + pod.status + ' pod-status">' + pod.status + '</div><div style="margin-top: 6px; color: #888; font-size: 10px;">Ready: ' + pod.ready + ' | Restarts: ' + pod.restarts + '</div>';
                                if (isOllamaPod) {
                                    const gpu = ollama ? ollama.gpu : 'none';
                                    podHtml += '<div class="pod-meta"><div class="pod-gpu">🖥️ ' + gpu + '</div><div class="pod-llm-header"><span class="pod-llm-toggle">&gt;</span>Running LLMs</div><div class="pod-llm-list"></div></div>';
                                }
                                podHtml += '</div>';
                                pods += podHtml;
                            });
                            pods += '</div></div>';
                        }
                    });
                }
                document.getElementById('dashboard').innerHTML = pods;
                attachLLMHandlers();
                document.getElementById('refresh-time').textContent = new Date().toLocaleTimeString();
            })
            .catch(err => console.error('Refresh error:', err));
        }

        charts.trends = createTrendsChart();
        refresh();
        setInterval(refresh, 5000);
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
    return jsonify(get_pods_by_namespace())

@app.route('/api/ollama')
def api_ollama():
    return jsonify(get_ollama_pods())

@app.route('/api/ollama/<pod_name>')
def api_ollama_models(pod_name):
    try:
        # Query ollama pod directly via kubectl exec
        result = subprocess.run(['kubectl', 'exec', '-n', 'ollama', pod_name, '--', 'ollama', 'list'],
                              capture_output=True, text=True, timeout=5)

        if result.returncode == 0:
            lines = result.stdout.strip().split('\n')[1:]  # Skip header
            models = []
            for line in lines:
                if line.strip():
                    parts = line.split()
                    if parts:
                        models.append(parts[0])
            return jsonify({'models': models})
        else:
            return jsonify({'models': []}), 200
    except Exception as e:
        return jsonify({'models': [], 'error': str(e)}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
