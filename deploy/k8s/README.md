# slack-log — Kubernetes deployment

FastAPI web service with a **built-in refresh scheduler**. The container image
is published on Docker Hub (`xieyanbo/slack-log`); these manifests target the
authors' EKS cluster (ALB ingress, authentik SSO) but adapt to any cluster.

The data refresh (slackdump archive → split → attach → index) runs *inside*
the server process — a background scheduler on a configurable interval, plus
an on-demand `POST /sync` API. There is no separate refresh pod, so the sync
mutex is a simple in-process lock (see `slack_log/sync.py`).

## Components

| File | What |
|---|---|
| `00-namespace-pvc.yaml` | `slack-log` namespace + 3Gi RWO data volume (search.db + data/) |
| `configmap.yaml` | non-secret config (paths, include filter, OIDC discovery URL, sync interval) |
| `deployment.yaml` | web server Deployment + ClusterIP Service |
| `ingress.yaml` | ALB ingress, `slack-log.example.com`, wildcard cert, health `/healthz` |

Secret `slack-log-secrets` is **not** in this repo (this is a public repo) —
create it directly with `kubectl create secret`, see below.

## Apply order

```sh
kubectl apply -f deploy/k8s/00-namespace-pvc.yaml
kubectl apply -f deploy/k8s/configmap.yaml

# Secret — values NOT committed. OIDC_* from the authentik slack-log provider;
# SLACK_XOXC/XOXD from ~/.config/slack-log/.env; SESSION_SECRET_KEY random;
# SLACK_LOG_SYNC_TOKEN a random bearer token for the POST /sync API.
kubectl create secret generic slack-log-secrets -n slack-log \
  --from-literal=OIDC_CLIENT_ID='<authentik client id>' \
  --from-literal=OIDC_CLIENT_SECRET='<authentik client secret>' \
  --from-literal=SESSION_SECRET_KEY="$(openssl rand -hex 32)" \
  --from-literal=SLACK_LOG_SYNC_TOKEN="$(openssl rand -hex 32)" \
  --from-literal=SLACK_XOXC='<xoxc-...>' \
  --from-literal=SLACK_XOXD='<xoxd-...>'

kubectl apply -f deploy/k8s/deployment.yaml   # Deployment + Service
kubectl apply -f deploy/k8s/ingress.yaml
```

## Bootstrap the data

The PVC starts empty. `/healthz` answers 200 right away, but the content
pages need data. Either wait for the scheduler's first tick, or run one
refresh by hand inside the running pod:

```sh
kubectl -n slack-log exec deploy/slack-log -- sh scripts/refresh.sh
```

The first refresh takes a few minutes (full archive + attachment download).

## Verify

```sh
kubectl get pods -n slack-log
curl -sI https://slack-log.example.com/healthz      # 200, no auth
open https://slack-log.example.com/                 # → authentik login → app
```
