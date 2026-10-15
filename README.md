# K8S Kubernetes Dashboard

Real-time Kubernetes cluster monitoring dashboard with multi-GPU metrics, pod status, and K8s event tracking.

## Features

- **Cluster Metrics:** CPU, Memory, Pod capacity with multi-ring donut charts
- **Pod Monitoring:** Real-time pod status across all namespaces
- **K8s Events:** Alert stream with enrichment and remediation hints
- **Ollama Integration:** GPU-aware model loading status
- **REST API:** `/api/metrics`, `/api/alerts`, `/api/pods`, `/api/ollama`

## Deployment

### Prerequisites
- Kubernetes cluster (v1.24+)
- containerd runtime with image access

### Build
```bash
cd app
podman build -t raycue-dashboard:latest .
```

### Deploy to K8s
```bash
kubectl apply -f k8s/deployment.yaml
```

### Access
- NodePort: http://localhost:30089
- Service: `kubectl port-forward -n dashboard svc/raycue-dashboard 8088:8088`

## Development

### Directory Structure
```
app/              - Dashboard application (Python Flask)
k8s/              - Kubernetes manifests
systemd/          - Legacy systemd service version (deprecated)
```

### Local Testing
```bash
cd app
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 server.py
# Visit http://localhost:8088
```

## Architecture

- **server.py:** Flask HTTP server, renders HTML template
- **render.py:** Multi-ring donut chart rendering, metric aggregation
- **collect.py:** K8s API polling, pod/node/event collection
- **k8s_alerts_enricher.py:** K8s event enrichment with remediation hints

## Monitoring

Health endpoint: `GET /healthz` (returns 200 if K8s API responsive)

## Troubleshooting

- **Pod not starting:** Check image availability in containerd
- **No metrics:** Verify ServiceAccount has cluster-reader permissions
- **Port conflict:** Edit NodePort in `k8s/deployment.yaml`
