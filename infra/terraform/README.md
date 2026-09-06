# Deploying

## Two-phase apply is required — this is not optional

The `kubernetes`, `helm`, and `kubectl` providers are configured against `module.eks`'s outputs (cluster endpoint, CA cert, auth token). On a brand-new account, those don't exist yet, so a single `terraform apply` from a blank slate fails with either:
- `kubernetes_manifest`: `cannot create REST client: no client config`
- `kubectl` provider: `invalid configuration: no configuration has been provided`

This is a well-known, well-documented limitation of bootstrapping an EKS cluster and installing Kubernetes-level resources in the same Terraform run — not a bug in this config, and not something worth engineering around with a more complex multi-state-file setup for an exercise this scope. The standard fix is a two-phase apply:

```bash
# Phase 1: stand up networking + the EKS cluster only, so the provider
# configs above have real values to resolve against.
terraform apply -target=module.networking -target=module.eks

# Phase 2: everything else (storage, ECR, IRSA, messaging, observability,
# and all the Kubernetes-level installs) — provider configs now resolve
# against the real cluster from phase 1.
terraform apply
```

This was caught by actually running `terraform plan` against the real sandbox AWS account (not just `terraform validate`, which only checks HCL syntax against provider schemas — it can't catch this class of issue since it doesn't need live provider configuration to succeed).

## First-time setup

```bash
# 1. Create the state bucket (one-time, before `terraform init` can use it as a backend).
aws s3 mb s3://<your-state-bucket-name> --region ap-south-1

# 2. Init with that bucket as the backend.
terraform init -backend-config="bucket=<your-state-bucket-name>" \
                -backend-config="key=churn-fde/terraform.tfstate" \
                -backend-config="region=ap-south-1"

# 3. Two-phase apply, as above.
terraform apply -target=module.networking -target=module.eks
terraform apply

# 4. Point kubectl at the new cluster (also printed as a Terraform output).
aws eks update-kubeconfig --name churn-fde-sandbox --region ap-south-1
```

## Re-applying to the official Localytics account

Per docs/architecture/00-overview.md: this same config re-applies unchanged to the official account once that AWS invite arrives. Concretely:
- Fresh `terraform init` with a **new** state bucket/key in that account — no state continuity assumed between the sandbox and official accounts (they're different, unfamiliar environments; see the staff-engineer execution plan's sequencing risks).
- Re-run the same two-phase apply sequence above.
- Check quotas/SCPs before the full apply — Karpenter needs broad EC2 instance-type permissions and the official account may restrict these differently than the personal sandbox. Test with `-target=module.eks` first (phase 1) before committing to the full apply.

## Cost control between sessions

`terraform destroy` between test sessions rather than leaving the cluster running continuously — see docs/architecture/01-data-platform.md's cost-control notes. The NAT gateway, EKS control plane, and any Karpenter-provisioned EC2 nodes all bill continuously while they exist.
