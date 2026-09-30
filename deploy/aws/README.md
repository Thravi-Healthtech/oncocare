# Deploying OncoCare to AWS (onco.thravi.in)

One command, run from the repo root on a machine where the AWS CLI v2 is signed in:

```bash
./deploy/aws/deploy.sh
```

First run creates everything, later runs redeploy the latest **committed** code (`git archive HEAD`).

What it sets up in `ap-south-1` (Mumbai):

| Piece | Detail |
|---|---|
| EC2 | `oncocare`, Ubuntu 24.04 ARM, `t4g.micro` (override with `INSTANCE_TYPE=`), 20 GB encrypted gp3 kept if the instance is terminated |
| Network | Security group `oncocare-web`: ports 80/443 only, **no SSH**. Elastic IP tagged `oncocare` |
| DNS | Route 53 `A onco.thravi.in → Elastic IP` in the `thravi.in` zone |
| HTTPS | Caddy reverse proxy, automatic Let's Encrypt certificate, HSTS + basic security headers |
| App | systemd `oncocare.service` → `python3 -m backend.server --seed` on 127.0.0.1:4173, sandboxed; data in `/var/lib/oncocare` (SQLite DB, uploads, env) |
| Secrets | SSM Parameter Store SecureStrings `/oncocare/demo_password` (generated) and `/oncocare/anthropic_api_key` (copied from local `.env` if present) |
| Access | IAM role `oncocare-ec2` (SSM management + read its release bucket and `/oncocare/*` parameters). Shell: `aws ssm start-session --target <instance-id>` |
| Releases | Private S3 bucket `oncocare-deploy-<account>-ap-south-1`; last 5 releases kept on the server |

Public mode (`ONCOCARE_PUBLIC=1`) is set on the server: cookies are `Secure`, the AI-key settings form is locked,
internal error details are hidden, the server refuses to start with the default `1234` password, and a
"demo – synthetic patients only" banner is shown on every page.

Useful commands:

```bash
aws ssm get-parameter --region ap-south-1 --name /oncocare/demo_password --with-decryption --query Parameter.Value --output text
aws ssm put-parameter --region ap-south-1 --name /oncocare/demo_password --type SecureString --overwrite --value 'new-pass'   # then redeploy
```

This is a demo deployment: one shared password, no backups. Before real patient data: OTP sign-in per user,
automated backups (EBS snapshots / DB dumps), audit logging and a proper hosting/compliance review.
