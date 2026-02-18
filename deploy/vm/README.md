# ICSARA VM Deployment Runbook (Google Cloud)

This runbook deploys ICSARA API on an existing Ubuntu/Debian VM with:
- public API on `http://<VM_PUBLIC_IP>:8080/v1`
- `X-API-Key` auth
- PostgreSQL external
- Redis internal (not exposed)
- auto-start with `systemd`

## 1. Bootstrap VM

Run in VM via SSH:

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates gnupg
```

Install Docker Engine + Compose plugin:

```bash
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
```

Enable Docker log rotation (`/etc/docker/daemon.json`):

```bash
sudo tee /etc/docker/daemon.json >/dev/null <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
JSON
sudo systemctl restart docker
```

Create 2 GB swap (for e2-micro safety):

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 2. Clone and configure app

```bash
sudo mkdir -p /opt/icsara
sudo chown -R "$USER":"$USER" /opt/icsara
cd /opt/icsara
git clone <YOUR_PUBLIC_REPO_URL> app
cd /opt/icsara/app
git checkout main
```

Create production env file:

```bash
cp deploy/vm/.env.prod.example .env
```

Edit `.env` and set real values:
- `API_KEYS`
- `DATABASE_URL`
- keep `REDIS_URL=redis://redis:6379/0`
- keep `DATA_DIR=/data/jobs`
- keep `CELERY_CONCURRENCY=1`
- keep `CORS_ALLOW_ALL=true` (temporary)

Protect secrets:

```bash
chmod 600 /opt/icsara/app/.env
```

## 3. Firewall and ports

Inside VM, check existing ports before deployment:

```bash
sudo ss -tulpn
```

Expected exposure for ICSARA:
- open `8080/tcp` only for API
- do not expose `6379` or `8000`

Create GCP firewall rules (run from machine with `gcloud` configured):

```bash
gcloud compute firewall-rules create allow-icsara-8080 \
  --network=default \
  --direction=INGRESS \
  --priority=1000 \
  --action=ALLOW \
  --rules=tcp:8080 \
  --source-ranges=0.0.0.0/0 \
  --target-tags=<VM_NETWORK_TAG>
```

Keep SSH restricted to your admin IP range.

## 4. First deployment

```bash
cd /opt/icsara/app
chmod +x deploy/vm/deploy.sh
./deploy/vm/deploy.sh
```

What this does:
1. updates `main`
2. starts Redis
3. runs `alembic upgrade head`
4. builds and starts API + worker

## 5. Auto-start with systemd

If your deploy user is not `deploy`, edit `deploy/systemd/icsara-stack.service` first.

```bash
cd /opt/icsara/app
sudo cp deploy/systemd/icsara-stack.service /etc/systemd/system/icsara-stack.service
sudo systemctl daemon-reload
sudo systemctl enable --now icsara-stack
sudo systemctl status icsara-stack --no-pager
```

## 6. Daily cleanup (TTL jobs)

Create a daily cron at 03:00:

```bash
(crontab -l 2>/dev/null; echo "0 3 * * * cd /opt/icsara/app && /usr/bin/docker compose -f docker-compose.prod.yml run --rm worker python scripts/cleanup_expired_jobs.py >> /var/log/icsara-cleanup.log 2>&1") | crontab -
```

## 7. Backups and uptime

Disk snapshots:
- Configure daily Compute Engine disk snapshot schedule in GCP Console.
- Attach schedule to VM disk where `/opt/icsara/app/data` lives.

Uptime check:
- Create HTTP uptime check to `http://<VM_PUBLIC_IP>:8080/v1/health/live`.
- Alert on non-200 status.

## 8. Validation checklist

Health:

```bash
curl -i http://<VM_PUBLIC_IP>:8080/v1/health/live
curl -i http://<VM_PUBLIC_IP>:8080/v1/health/ready
```

Auth:

```bash
curl -i http://<VM_PUBLIC_IP>:8080/v1/jobs/00000000-0000-0000-0000-000000000000
```

Redis not public (run from external host):

```bash
nc -vz <VM_PUBLIC_IP> 6379
```

Must fail/timeout.

## 9. Operations

Deploy update:

```bash
cd /opt/icsara/app
./deploy/vm/deploy.sh
```

Logs:

```bash
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml logs -f worker
```

## 10. Phase 2 target

When domain is available:
1. add HTTPS (nginx or existing reverse proxy)
2. replace wildcard CORS with explicit frontend origins
3. keep API key rotation policy active
