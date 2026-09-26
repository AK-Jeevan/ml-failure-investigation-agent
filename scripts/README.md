# Deployment

Three ways to run this agent, from quickest to most production-shaped.

## 1. Local Docker Compose (`compose.yaml`)

```bash
docker compose up -d --build      # simulator + api + dashboard, one image, three roles
docker compose ps                 # wait for all three to report (healthy)
docker compose down               # stop (the investigation-data volume keeps the database)
```

Requires an `.env` next to `compose.yaml` (copy `.env.example` values into `.env`). Compose fails
fast without `API_ACCESS_TOKEN` and `GRADIO_AUTH_PASSWORD`.

- Dashboard: <http://127.0.0.1:7860> — login `GRADIO_AUTH_USERNAME` / `GRADIO_AUTH_PASSWORD`
- API docs: <http://127.0.0.1:8000/docs> — send `API_ACCESS_TOKEN` as a bearer token
- Both ports are published on `127.0.0.1` only. `GET /` answers `200` (Gradio login shell),
  `GET /config` and `/metrics` answer `401` without credentials.

## 2. Single EC2 instance (`scripts/deploy-ec2.ps1`)

A convenience path for a private, always-on demo link. It is **not** the architecture the
evaluation and the AWS workflow describe (see section 3), and it terminates TLS nowhere, so
treat it as a demo box.

```powershell
# from the repository root, on a Windows client with ssh/scp/tar on PATH
./scripts/deploy-ec2.ps1 -Instance <public-ip-or-dns> -KeyPath 'C:\keys\investigator.pem'
./scripts/deploy-ec2.ps1 -Instance <public-ip-or-dns> -KeyPath 'C:\keys\investigator.pem' -PublicDashboard
```

What it does, in order:

1. bundles the working tree with the same exclusions `.dockerignore` applies to the image
   context (no `Agent2`/`.venv`, no local SQLite database, no caches);
2. copies the bundle and `scripts/provision-ec2.sh` to the instance over `scp`;
3. unpacks into `~/<RemoteDirectory>` (default `~/mlops-investigation-agent`) and strips CRLF from
   `.env` and from `scripts/*.sh`;
4. installs Docker Engine and the compose plugin (skip with `-SkipProvision`);
5. runs `docker compose up -d --build` on the instance;
6. waits for `http://127.0.0.1:8000/health` and `http://127.0.0.1:7860/` to answer before
   declaring success (timeout via `-HealthyTimeoutSeconds`), then prints the status codes,
   the running commit and the dashboard file hash so the deployed revision is identifiable.

Reaching the dashboard afterwards, without exposing anything publicly:

```powershell
ssh -i <key.pem> -N -L 17860:127.0.0.1:7860 ubuntu@<instance>
# open http://127.0.0.1:17860  (pick a free local port; 7860 may already be taken locally)
```

With `-PublicDashboard` the dashboard also binds `0.0.0.0:7860`, so one security group rule makes
it reachable at `http://<instance-public-ip>:7860`:

```bash
aws ec2 authorize-security-group-ingress --group-id <sg-id> \
  --protocol tcp --port 7860 --cidr <your-public-ip>/32
```

Keep that CIDR scoped to your own address, leave port 8000 closed (the API stays on loopback inside
the instance), and note that instance public IPs change on stop/start unless an Elastic IP is
attached. The dashboard keeps basic authentication enabled whenever it binds beyond localhost.

`scripts/provision-ec2.sh` is idempotent and can also be used on its own:

```bash
ssh -i <key.pem> ubuntu@<instance> 'bash -s' < scripts/provision-ec2.sh
```

## 3. ECS Fargate behind an HTTPS load balancer (the designed path)

`infra/aws/stack.yaml` plus `.github/workflows/deploy-aws.yml` deploy one Fargate task
(simulator + api + dashboard) behind an HTTPS ALB, with secrets injected from Secrets Manager, an
immutable ECR repository and an encrypted EFS volume for investigation data. The workflow runs
manually and authenticates through GitHub OIDC. Prerequisites:

- a VPC with two public subnets in different Availability Zones,
- an ACM certificate in the ALB's region for the dashboard hostname and a DNS record pointing at
  the load balancer,
- three Secrets Manager secrets holding plain strings: API bearer token, dashboard password,
  NVIDIA API key,
- an IAM OIDC provider for `token.actions.githubusercontent.com` and a deploy role trusted for
  `repo:<owner>/<repo>:environment:production`,
- repository variables: `AWS_REGION`, `AWS_STACK_NAME`, `AWS_DEPLOY_ROLE_ARN`, `AWS_VPC_ID`,
  `AWS_PUBLIC_SUBNET_IDS`, `AWS_DASHBOARD_CERTIFICATE_ARN`, `AWS_DASHBOARD_DOMAIN`,
  `AWS_API_TOKEN_SECRET_ARN`, `AWS_DASHBOARD_PASSWORD_SECRET_ARN`, `AWS_NVIDIA_API_KEY_SECRET_ARN`
  and optionally `AWS_DASHBOARD_USERNAME`.

The first run creates the stack with `DesiredCount=0`, pushes the image, then redeploys with
`DesiredCount=1` and smoke-tests the dashboard. Point the hostname at the load balancer and re-run
if the DNS record did not exist yet.

## Operational notes

- Never commit `.env`; it is gitignored. Rotate the NVIDIA key if it has ever been shared, and
  update `.env` (locally and on the instance) afterwards.
- The stack runs the LLM reasoning provider when `NVIDIA_API_KEY` is set and falls back to the
  deterministic planner plus report when it is empty, so the agent still serves investigations.
- `mlops-investigate --evaluate` must pass (38 checks) before deploying; CI runs it on Python
  3.11, 3.12 and 3.13 for every push and pull request.
