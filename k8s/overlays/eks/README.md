# EKS overlay — ALB via `TargetGroupBinding`

Deploys IcebergTTX on AWS EKS fronted by an Application Load Balancer, using the
[AWS Load Balancer Controller](https://kubernetes-sigs.github.io/aws-load-balancer-controller/).
It is the cloud-agnostic base (`k8s/kustomization.yaml`) plus a single
`TargetGroupBinding` — nothing else changes, and the CRD dependency is confined
to this directory so the base and `overlays/nginx` stay portable.

```
kubectl apply -k k8s/overlays/eks
```

## When to use this vs. an ALB Ingress

There are two ways to front the app with an ALB. Pick one:

| | `TargetGroupBinding` (this overlay) | ALB `Ingress` |
|---|---|---|
| Who owns the ALB | You (Terraform/CDK/console) | The controller creates + owns it |
| What Kubernetes does | Registers/deregisters pods in your target group | Provisions ALB, listeners, target groups |
| Use when | ALB/DNS/cert are managed by your infra-as-code | You want K8s to manage the whole edge |

To use the **ALB Ingress** path instead: delete `targetgroupbinding.yaml`, drop
it from `kustomization.yaml`, and add an `Ingress` with `ingressClassName: alb`
and `alb.ingress.kubernetes.io/*` annotations (scheme, `target-type: ip`,
`certificate-arn`, `healthcheck-path: /static/css/output.css`,
`load-balancer-attributes: idle_timeout.timeout_seconds=…`). The base needs no
change either way.

## Prerequisites

1. **AWS Load Balancer Controller** installed in the cluster — it owns the
   `elbv2.k8s.aws` CRDs. Without it, `kubectl apply -k` fails with
   `no matches for kind "TargetGroupBinding"`.
2. **An ALB with an HTTPS listener** (ACM certificate). Terminate TLS at the ALB
   and forward HTTP to caddy. **Do not serve plaintext HTTP** — the app sets
   `Secure` cookies and the auth flow breaks over HTTP.
3. **A target group of type `ip`** whose health check path is one caddy serves,
   e.g. `/static/css/output.css` (matches caddy's readiness probe). The ALB
   default `/` will flap.
4. Fill in the placeholders in `targetgroupbinding.yaml`
   (`REPLACE_WITH_TARGET_GROUP_ARN`, `REPLACE_WITH_ALB_SECURITY_GROUP_ID`) and
   the usual secrets in `k8s/base/secrets.yaml` / config in `k8s/base/configmap.yaml`.

## Gotchas specific to this app

- **`targetType: ip`, not `instance`.** `ip` mode lets the ALB hit pod IPs
  directly via the VPC CNI, so the base's ClusterIP caddy Service works as-is.
  `instance` mode would require switching that Service to `type: NodePort`.
- **WebSockets.** The app holds live facilitator/participant sockets. The ALB
  default idle timeout is 60s and will drop idle connections — raise
  `idle_timeout.timeout_seconds` on the load balancer.
- **Single replica.** Per the deployment docs the app runs `replicas: 1` with
  `strategy: Recreate` (in-process timers, rate-limit counters, WS fan-out, and
  config caches are not shared). The ALB in front does not change that — do not
  scale caddy/app out to fill the target group; stickiness is moot with one pod.
- **NetworkPolicy.** `k8s/base/networkpolicy.yaml` already allows caddy:8080 from any
  source, so it does not block ALB traffic in `ip` mode. The `networking` block
  in `targetgroupbinding.yaml` is what lets the controller open the ALB security
  group to the pods.
