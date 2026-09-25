# Roostoo Trading Bot — AWS Production Deployment Guide

This guide describes how to deploy the autonomous Roostoo Quantitative Trading Bot to an AWS EC2 instance (or Lightsail) for 24/7 continuous operation during the competition.

---

## Architecture Overview

```
                      +---------------------------------------+
                      |         AWS EC2 (t3.micro/small)      |
                      |                                       |
+------------------+  |  +---------------------------------+  |
| Roostoo Exchange |<--->|       roostoo-bot container     |  |
| Mock API         |  |  |  - Python 3.11 Runtime          |  |
+------------------+  |  |  - Trading Engine Loop          |  |
                      |  |  - Risk Manager (Circuit Breaker)|  |
                      |  |  - Local Ledger & Reconcile     |  |
                      |  |  - SHA-256 Audit Logger         |  |
                      |  |  - FastAPI & WebSocket Server   |  |
                      |  +---------------------------------+  |
                      |                  |                    |
                      |         [Port 8080 Telemetry]         |
                      |                  v                    |
                      |  +---------------------------------+  |
+------------------+  |  |       Nginx Reverse Proxy       |  |
| Competition Lead |<--->|    (TLS/HTTPS + HTTP Basic Auth)|  |
| / Operator Web   |  |  +---------------------------------+  |
+------------------+  +---------------------------------------+
```

---

## 1. AWS EC2 Instance Provisioning

1. **Instance Type**: `t3.small` (or `t3.micro` free tier, 1-2 vCPU, 1-2 GB RAM).
2. **AMI**: Ubuntu Server 24.04 LTS (x86_64).
3. **Storage**: 20 GB gp3 SSD.
4. **Security Group Inbound Rules**:
   - `SSH (22)`: Your personal IP only (`x.x.x.x/32`).
   - `HTTPS (443)`: Allowed for authorized operators (or specific competition monitoring IPs).
   - `HTTP (80)`: For Let's Encrypt SSL certificate issuance.
   - `Custom TCP (8080)`: Internal only (do not expose directly to 0.0.0.0/0 without reverse proxy auth).

---

## 2. Server Environment Setup

SSH into your instance:
```bash
ssh -i your-key.pem ubuntu@<your-ec2-ip>
```

Update system and install Docker & Docker Compose:
```bash
sudo apt-get update && sudo apt-get upgrade -y
sudo apt-get install -y curl git ufw fail2ban certbot python3-certbot-nginx

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker ubuntu

# Configure Amazon Time Sync Service (Chrony) for sub-millisecond clock sync
sudo apt-get install -y chrony
echo "server 169.254.169.123 prefer iburst minpoll 4 maxpoll 4" | sudo tee -a /etc/chrony/chrony.conf
sudo systemctl restart chrony
chronyc tracking

# Log out and log back in for docker group to take effect
exit
```

---

## 3. Clone Repository and Configure Secrets

```bash
git clone <your-repo-url> /home/ubuntu/roostoo-bot
cd /home/ubuntu/roostoo-bot

# Create production environment file
cp .env.example .env
nano .env
```

Ensure `.env` contains:
```ini
ROOSTOO_API_KEY=your_actual_competition_api_key
ROOSTOO_SECRET_KEY=your_actual_competition_secret_key
ROOSTOO_BASE_URL=https://mock-api.roostoo.com
DASHBOARD_AUTH_TOKEN=your_strong_secret_token_here
```

Validate permissions:
```bash
chmod 600 .env
```

---

## 4. Run Pre-Flight Verification

Before launching the container, run the automated verification script:
```bash
python3 -m pip install -r requirements.txt
python3 scripts/preflight_check.py
```
Ensure all critical checks return `PASS`.

---

## 5. Launch Container via Docker Compose

```bash
# Build and start container in detached daemon mode
docker compose up --build -d

# Verify container is running and healthy
docker compose ps
docker compose logs -f
```

The container includes built-in healthchecks:
```bash
docker inspect --format='{{json .State.Health.Status}}' roostoo-bot
# Returns: "healthy"
```

---

## 6. Systemd Auto-Restart Configuration

To ensure the trading bot survives server reboots, create a systemd service:

```bash
sudo nano /etc/systemd/system/roostoo-bot.service
```

Paste:
```ini
[Unit]
Description=Roostoo Autonomous Quantitative Trading Bot
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/ubuntu/roostoo-bot
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable roostoo-bot.service
```

---

## 7. Audit Log Backup & Retention

The bot writes an immutable SHA-256 hash-chained audit log to `/app/logs/audit_*.jsonl`.
Set up a daily cron job to sync audit logs and portfolio states to AWS S3:

```bash
crontab -e
```

Add:
```cron
# Backup logs and state to S3 every 6 hours
0 */6 * * * aws s3 sync /home/ubuntu/roostoo-bot/logs s3://your-audit-bucket/logs/
0 */6 * * * aws s3 sync /home/ubuntu/roostoo-bot/state s3://your-audit-bucket/state/
```

---

## 8. Emergency Operator Playbook

### View Live Trading Logs
```bash
docker compose logs -f --tail=100
```

### Check Audit Chain Integrity
```bash
docker compose exec trading-bot python -c "from logs.audit_logger import AuditLogger; print('Chain OK:', AuditLogger().verify_chain())"
```

### Emergency Stop (Operator Kill Switch)
Option A — Via Dashboard:
Click the red **EMERGENCY KILL SWITCH** button in the dashboard operator panel.

Option B — Via Terminal:
```bash
docker compose stop
```
