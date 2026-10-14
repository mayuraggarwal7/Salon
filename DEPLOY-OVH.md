# Deploying on an OVH VPS

Runs the whole salon app on one server with Docker Compose:

| Service | What it does |
| --- | --- |
| `web` | The Django app (gunicorn). Runs migrations and creates the manager login on start |
| `db` | Postgres 16, data in the `pgdata` volume |
| `caddy` | HTTPS on ports 80/443 with a free Let's Encrypt certificate, renewed automatically |
| `reminders` | Sends WhatsApp appointment reminders every 15 minutes |
| `backup` | Nightly database dump to `/opt/salon/backups`, keeps 14 days |

Uploaded invoice scans live in the `media` volume, so they survive restarts and redeploys.

## What you need

- An OVH VPS with Ubuntu 22.04/24.04 or Debian 12. The smallest VPS plan (2 GB RAM) is enough.
- A domain or subdomain, e.g. `book.akssalon.in`, with a DNS **A record** pointing at the VPS IPv4 address.
  HTTPS will not work until this resolves.
- SSH access to the VPS (OVH emails the login when the VPS is created).

## First deploy

```bash
ssh ubuntu@<vps-ip>
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/mayuraggarwal7/salon.git /opt/salon   # or use the curl one-liner in deploy/ovh-setup.sh
sudo /opt/salon/deploy/ovh-setup.sh book.akssalon.in
```

The script installs Docker, opens ports 22/80/443 in the firewall, creates `/opt/salon/.env` with generated
passwords, and starts everything. Then:

1. Open `https://book.akssalon.in` and log in as `admin`. The password is `DJANGO_ADMIN_PASSWORD` in
   `/opt/salon/.env` (`sudo grep ADMIN /opt/salon/.env`).
2. Edit `/opt/salon/.env` for the salon name, address and WhatsApp keys, then apply with
   `cd /opt/salon && sudo docker compose up -d`.
3. In Meta's WhatsApp settings, set the webhook URL to `https://book.akssalon.in/whatsapp/webhook/` and the verify
   token to `WHATSAPP_VERIFY_TOKEN` from `.env`.

## Updating to a new version

```bash
cd /opt/salon && sudo git pull && sudo docker compose up -d --build
```

## Day to day

| Task | Command (in `/opt/salon`) |
| --- | --- |
| Status | `sudo docker compose ps` |
| App logs | `sudo docker compose logs -f web` |
| Restart | `sudo docker compose restart web` |
| Manual backup | `sudo docker compose exec backup sh -c 'pg_dump -h db -U salon -d salon -Fc -f /backups/manual.dump'` |
| Restore a backup | `sudo docker compose exec -T db pg_restore -U salon -d salon --clean < backups/salon-YYYY-MM-DD.dump` |
| Django shell | `sudo docker compose exec web python manage.py shell` |

Backups stay on the same server. For safety, copy `/opt/salon/backups` somewhere else now and then, or turn on OVH's
automated VPS backup option.
