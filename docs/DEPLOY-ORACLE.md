# Hosting Ragra for free on Oracle Cloud

Oracle's "Always Free" tier gives a real always-on server at no cost. This guide
puts Ragra on it with Docker Compose and automatic HTTPS (Caddy). It reuses the
same `docker-compose.yml` and `Caddyfile` as `docs/DEPLOY-GCP.md`.

Free-tier terms change. Check Oracle's "Always Free Resources" page before you
rely on any number written here.

## Why Oracle, and how Ragra fits

| What Ragra needs | Why | Oracle Always Free |
|---|---|---|
| Always on | Reminders fire from a background loop every ~2 minutes | A normal VM that never sleeps |
| A real disk | The whole database is one SQLite file | A normal boot disk (about 47 GB) that survives restarts |
| One process only | SQLite is a single file, so two copies would fight over it | A VM runs exactly the one container you start |
| Outbound web access | Google APIs, Brevo email | Allowed on 443 |
| HTTPS with a fixed address | Google sign-in needs a stable redirect address | Free reserved IP plus a free DuckDNS name plus Caddy |
| Small footprint | A few students, light traffic | 1 GB RAM is enough with a 2 GB swap file |

Ragra runs `ragra.cli serve-hosted`: the web app and the reminder loop in one
process (see the `Dockerfile`). Nothing else is needed.

### Advantages over Railway
- No trial that ends: the VM keeps running while the account stays active.
- A normal disk, so no volume plans to pay for.
- No sleeping, unlike Render's free plan.
- You control everything: logs, backups, updates.

### Things to know
- **Card needed to sign up** (a debit or credit card, for identity). Always Free resources are not charged.
- **You run the server**: you apply updates and keep backups. This guide makes both short.
- **Oracle may reclaim idle free VMs.** Ragra is light, so it could look idle. If you see a notice, upgrade the account to Pay As You Go (you still pay nothing while you stay inside Always Free limits). Check Oracle's current policy.
- **Pick the AMD micro VM** (`VM.Standard.E2.1.Micro`). It is the one that is reliably available. The free Arm allowance was reduced in 2026 and is often out of stock.
- **One VM, one app.** Never run two copies against the same database.

## 1. Create the Oracle account
cloud.oracle.com -> Sign up. Pick a home region close to you (it cannot be
changed later) and finish the card verification.

## 2. Create the VM
Menu -> Compute -> Instances -> **Create instance**.
- Name: `ragra`
- Image: **Canonical Ubuntu 22.04**
- Shape: **Change shape** -> *Specialty and previous generation* -> **VM.Standard.E2.1.Micro** (marked Always Free-eligible)
- Networking: keep the defaults, make sure **Assign a public IPv4 address** is on
- SSH keys: **Generate a key pair** and **download the private key**. Keep it safe: it is your only way in.
- Boot volume: leave the default (up to the free allowance)

Click **Create**. When it shows *Running*, note the **Public IP address**.

### Make the IP permanent (free)
Networking -> IP management -> **Reserved public IPs** -> Reserve public IP address
(name `ragra-ip`). Then open the instance -> Attached VNICs -> the VNIC -> IPv4
addresses -> Edit -> *Reserved public IP* -> choose `ragra-ip`.

### Open the web ports
Networking -> Virtual cloud networks -> your VCN -> the public subnet -> its
**Security List** -> **Add ingress rules** (two rules):
- Source CIDR `0.0.0.0/0`, IP protocol TCP, destination port `80`
- Source CIDR `0.0.0.0/0`, IP protocol TCP, destination port `443`

## 3. Point a free name at it
At duckdns.org sign in, create a name such as `ragra-reminder`, and set its IP to
the VM's reserved address. Your address is `ragra-reminder.duckdns.org`.

## 4. Connect to the server (from Windows PowerShell)
```
ssh -i "C:\Users\dell\Downloads\ssh-key-XXXX.key" ubuntu@<PUBLIC_IP>
```
If Windows says the key file is "too open", run once (replace the path):
```
icacls "C:\Users\dell\Downloads\ssh-key-XXXX.key" /inheritance:r /grant:r "$($env:USERNAME):(R)"
```

## 5. Prepare the server
Run these on the server:
```
# let web traffic through the Ubuntu firewall
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save

# Docker, git, and a swap file (the VM has only 1 GB of memory; building needs more)
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
sudo apt-get install -y git
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab

git clone https://github.com/Ragra-Dev/Ragra-Reminder.git && cd Ragra-Reminder
```

## 6. Configure
```
cp deploy.env.example .env
docker compose run --rm --no-deps --entrypoint "" app python -m ragra.cli generate-credential-key
nano .env
```
Fill in `.env` (Ctrl+O, Enter, Ctrl+X to save):
- `RAGRA_DOMAIN` = `ragra-reminder.duckdns.org`
- replace every `<RAGRA_DOMAIN>` with that same name
- `RAGRA_CREDENTIAL_KEY` = the key printed above. **Keep a private copy** (it unlocks stored Google connections).
- `RAGRA_OAUTH_CLIENT_ID` / `RAGRA_OAUTH_CLIENT_SECRET` = the same values you use on Railway today
- `RAGRA_OWNER_EMAIL` = your Google login email
- `RAGRA_BREVO_API_KEY` and `RAGRA_EMAIL_FROM` = the same Brevo values as on Railway

Copy the other variables you have on Railway (Railway -> your service -> Variables)
so nothing is missing.

## 7. Start
```
docker compose up -d --build
docker compose logs -f app
```
The first build takes several minutes on this small machine. Caddy fetches the
HTTPS certificate by itself once the DuckDNS name points at the VM.
Open `https://ragra-reminder.duckdns.org`.

## 8. Google Cloud Console
APIs & Services -> Credentials -> your OAuth client -> **Authorized redirect URIs**, add:
- `https://ragra-reminder.duckdns.org/auth/callback`
- `https://ragra-reminder.duckdns.org/account/google/callback`

## 9. Your data
- **Fresh start (simplest):** sign in, reconnect Google, and Classroom items sync back by themselves. Chat history and old feedback are not carried over.
- **Keep everything:** copy `/data/ragra.db` from the Railway volume to the new server with the same `RAGRA_CREDENTIAL_KEY` as before, then `docker compose cp ragra.db app:/data/ragra.db` and restart. Do this before step 7 if you want it.

## 10. Backups and updates
```
chmod +x scripts/backup-db.sh
crontab -e        # add this line at the bottom:
15 3 * * * /home/ubuntu/Ragra-Reminder/scripts/backup-db.sh >> /home/ubuntu/backup.log 2>&1
```
That keeps 14 daily copies in `backups/`. Download one now and then with `scp`.

Update to the latest code:
```
cd ~/Ragra-Reminder && git pull && docker compose up -d --build
```

Handy commands: `docker compose logs --tail 100 app`, `docker compose ps`,
`docker stats` (memory use), `docker compose restart app`.

## Checks after it is live
1. `https://<name>/` loads and Google sign-in works.
2. Contact us -> send feedback -> both team inboxes receive it, and `/contact/inbox` shows "Sent to 2 of 2".
3. `docker compose logs app` shows a `tick start` / `tick end` line every couple of minutes.
4. Turn the Railway service off only after all three pass.
