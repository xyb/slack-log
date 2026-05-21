# slack-log — Kubernetes deployment

FastAPI search service + daily slackdump refresh CronJob. The container image
is published on Docker Hub (`xieyanbo/slack-log`); these manifests target the
authors' EKS cluster (ALB ingress, authentik SSO) but adapt to any cluster.

## Components

| File | What |
|---|---|
| `00-namespace-pvc.yaml` | `slack-log` namespace + 3Gi RWO data volume (search.db + html/ + data/) |
| `configmap.yaml` | non-secret config (paths, include filter, OIDC discovery URL) |
| `deployment.yaml` | web server Deployment + ClusterIP Service |
| `ingress.yaml` | ALB ingress, `slack-log.example.com`, wildcard cert, health `/healthz` |
| `cronjob.yaml` | daily refresh (slackdump → split → attach → render → index) |

Secret `slack-log-secrets` is **not** in this repo (this is a public repo) —
create it directly with `kubectl create secret`, see below.

## Apply order

```sh
kubectl apply -f deploy/k8s/00-namespace-pvc.yaml
kubectl apply -f deploy/k8s/configmap.yaml

# Secret — values NOT committed. OIDC_* from the authentik slack-log provider;
# SLACK_XOXC/XOXD from ~/.config/slack-log/.env; SESSION_SECRET_KEY random.
kubectl create secret generic slack-log-secrets -n slack-log \
  --from-literal=OIDC_CLIENT_ID='<authentik client id>' \
  --from-literal=OIDC_CLIENT_SECRET='<authentik client secret>' \
  --from-literal=SESSION_SECRET_KEY="$(openssl rand -hex 32)" \
  --from-literal=SLACK_XOXC='<xoxc-...>' \
  --from-literal=SLACK_XOXD='<xoxd-...>'

kubectl apply -f deploy/k8s/deployment.yaml   # Deployment + Service
kubectl apply -f deploy/k8s/ingress.yaml
kubectl apply -f deploy/k8s/cronjob.yaml
```

## Bootstrap the data

The PVC starts empty. Trigger the first refresh by hand instead of waiting
for the nightly schedule:

```sh
kubectl create job -n slack-log --from=cronjob/slack-log-refresh slack-log-bootstrap
kubectl logs -n slack-log -f job/slack-log-bootstrap
```

The web pod will return 503 from readiness until `search.db` + `html/` exist;
that is expected before the first refresh completes.

## Verify

```sh
kubectl get pods -n slack-log
curl -sI https://slack-log.example.com/healthz      # 200, no auth
open https://slack-log.example.com/                 # → authentik login → app
```
