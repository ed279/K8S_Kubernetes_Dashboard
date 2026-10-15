"""K8s Events Enricher — adds verbose details + remediation to alert display.

Parses K8s events to extract:
1. Actual error message (vs generic "Failed"/"BackOff")
2. Affected resource (pod/node/pvc)
3. Remediation steps
4. Urgency (critical/warning/info)
"""
import os
import subprocess
import json
from datetime import datetime
from typing import Optional
import re

def run_kubectl(args: list) -> Optional[dict]:
    """Run kubectl command and parse JSON output."""
    try:
        # Try standard kubectl first, then fall back to k3s
        cmd = ["kubectl"] + args + ["-o", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return json.loads(result.stdout)
        return None
    except Exception:
        try:
            # Fallback to k3s kubectl on host
            cmd = ["/usr/local/bin/k3s", "kubectl"] + args + ["-o", "json"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                return json.loads(result.stdout)
        except Exception:
            pass
        return None

# Remediation mapping: error pattern → fix steps
REMEDIATION_MAP = {
    r"ImagePullBackOff|Failed to pull image": {
        "urgency": "critical",
        "steps": [
            "Check image URL is correct and accessible",
            "Verify image registry credentials (imagePullSecrets)",
            "Check network connectivity to image registry",
            "Try: kubectl describe pod <pod> for registry URL",
            "Retry with: kubectl rollout restart deployment/<name>"
        ]
    },
    r"CrashLoopBackOff": {
        "urgency": "critical",
        "steps": [
            "Check container logs: kubectl logs <pod>",
            "Look for startup errors (missing env vars, config files)",
            "Verify all required volumes are mounted",
            "Check resource limits vs actual usage",
            "Restart pod: kubectl delete pod <pod>"
        ]
    },
    r"Pending|Unschedulable": {
        "urgency": "warning",
        "steps": [
            "Check node capacity: kubectl top nodes",
            "Look for resource requests exceeding available",
            "Check node selectors match available nodes",
            "Drain problematic node: kubectl drain <node>",
            "Check for taints preventing scheduling"
        ]
    },
    r"OOMKilled|Out of memory": {
        "urgency": "critical",
        "steps": [
            "Increase memory limit in deployment",
            "Check actual usage: kubectl top pod",
            "Look for memory leaks in container logs",
            "Reduce container count or data size",
            "Scale pod horizontally instead of vertically"
        ]
    },
    r"Evicted": {
        "urgency": "critical",
        "steps": [
            "Check eviction reason: kubectl describe pod",
            "Common: memory pressure, disk pressure, PID pressure",
            "Clean up: kubectl delete pod <pod> (let controller respawn)",
            "Monitor node health: kubectl get nodes",
            "Check kubelet logs for eviction thresholds"
        ]
    },
    r"ConnectionRefused|Connection reset": {
        "urgency": "warning",
        "steps": [
            "Check if target service is running: kubectl get svc",
            "Verify service endpoints: kubectl get endpoints",
            "Check DNS resolution in cluster",
            "Test connectivity: kubectl run -it --rm debug --image=curlimages/curl -- sh",
            "Review network policies blocking traffic"
        ]
    },
}

class K8sAlertsEnricher:
    """Enriches K8s alerts with detailed error info and fixes."""

    def get_pod_events(self, namespace: str = "default", pod: Optional[str] = None) -> list:
        """Fetch recent pod events from K8s."""
        args = ["get", "events", "-n", namespace, "--sort-by=.metadata.creationTimestamp"]
        result = run_kubectl(args)
        if not result or "items" not in result:
            return []

        events = []
        for event in result["items"]:
            # Filter to warnings/errors only
            event_type = event.get("type", "")
            if event_type not in ("Warning", "Error"):
                continue

            # Filter to pod if specified
            if pod and event.get("involvedObject", {}).get("name") != pod:
                continue

            # Extract key fields
            alert = {
                "id": f"{event['involvedObject']['namespace']}:{event['involvedObject']['name']}:{event['reason']}",
                "type": event_type,
                "reason": event.get("reason", "Unknown"),
                "message": event.get("message", ""),
                "pod": event["involvedObject"]["name"],
                "namespace": event["involvedObject"]["namespace"],
                "timestamp": event.get("firstTimestamp", event.get("metadata", {}).get("creationTimestamp")),
                "count": event.get("count", 1),
                "object_kind": event["involvedObject"]["kind"],
            }

            # Enrich with remediation
            alert["remediation"] = self._get_remediation(alert["message"], alert["reason"])
            events.append(alert)

        return events

    def _get_remediation(self, message: str, reason: str) -> dict:
        """Match error pattern to remediation steps."""
        full_text = f"{reason} {message}".lower()

        for pattern, remedy in REMEDIATION_MAP.items():
            if re.search(pattern, full_text, re.IGNORECASE):
                return remedy

        # Default remediation
        return {
            "urgency": "info",
            "steps": [
                f"Check event details: kubectl describe {reason}",
                "Review container logs for more context",
                "Check resource health: kubectl top nodes/pods"
            ]
        }

    def get_summary(self, namespace: str = "default") -> dict:
        """Get alert summary for dashboard."""
        events = self.get_pod_events(namespace)

        critical = [e for e in events if e["remediation"]["urgency"] == "critical"]
        warnings = [e for e in events if e["remediation"]["urgency"] == "warning"]
        infos = [e for e in events if e["remediation"]["urgency"] == "info"]

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "namespace": namespace,
            "total_alerts": len(events),
            "critical_count": len(critical),
            "warning_count": len(warnings),
            "info_count": len(infos),
            "alerts": events,
            "grouped": {
                "critical": critical,
                "warning": warnings,
                "info": infos
            }
        }

# Pod endpoint discovery for dashboard
class K8sPodEndpoints:
    """Discovers and formats pod URLs for dashboard access."""

    def get_pod_urls(self, namespace: str = "default") -> list:
        """Get all pods with exposed HTTP endpoints."""
        args = ["get", "pods", "-n", namespace]
        result = run_kubectl(args)
        if not result or "items" not in result:
            return []

        urls = []
        for pod in result["items"]:
            name = pod["metadata"]["name"]
            labels = pod["metadata"].get("labels", {})

            # Look for pods with service labels or known exposed ports
            if "app" in labels:
                # Try to find corresponding service
                service_result = run_kubectl(["get", "svc", "-n", namespace, "-l", f"app={labels['app']}"])
                if service_result and "items" in service_result:
                    for svc in service_result["items"]:
                        service_name = svc["metadata"]["name"]
                        for port_spec in svc.get("spec", {}).get("ports", []):
                            port = port_spec.get("port")
                            if port:
                                urls.append({
                                    "pod_name": name,
                                    "service": service_name,
                                    "port": port,
                                    "protocol": "http",
                                    "local_url": f"http://10.0.9.99:{port}",
                                    "tailscale_url": self._get_tailscale_url(service_name, port),
                                    "exposed_ports": port_spec.get("containerPort")
                                })

        return urls

    def _get_tailscale_url(self, service: str, port: int) -> Optional[str]:
        """Build Tailscale URL if available."""
        # This would need to query Tailscale for the pod IP, for now return pattern
        return f"http://{service}.tailscale.local:{port}"

if __name__ == "__main__":
    enricher = K8sAlertsEnricher()

    # Get alerts for all namespaces
    for ns in ["default", "cephalobot", "vault", "tesseract", "dns"]:
        summary = enricher.get_summary(ns)
        print(f"\n{ns}: {summary['total_alerts']} alerts "
              f"({summary['critical_count']} critical, {summary['warning_count']} warning)")

        for alert in summary["alerts"][:3]:  # Show first 3
            print(f"  [{alert['reason']}] {alert['pod']}")
            print(f"    → Fix: {alert['remediation']['steps'][0]}")

    # Get pod endpoints
    endpoints = K8sPodEndpoints()
    urls = endpoints.get_pod_urls("default")
    print(f"\n\nDiscovered {len(urls)} pod endpoints")
    for url in urls[:5]:
        print(f"  {url['service']}: {url['local_url']}")
