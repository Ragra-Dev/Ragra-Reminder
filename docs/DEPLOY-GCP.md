# Deploying Ragra on a free Google Cloud VM

An always-on e2-micro VM runs the app and Caddy (automatic HTTPS) with Docker
Compose. Data lives in a Docker volume on the VM's disk. Run one instance only.
Free-tier terms change; check Google's "Always Free" page before relying on it.

## 1. Create the VM
Compute Engine -> Create instance: machine type `e2-micro`, region
`us-west1`, `us-central1` or `us-east1`, boot disk Debian 12 with a 30 GB
standard persistent disk, and tick **Allow HTTP traffic** and **Allow HTTPS
traffic**. Then VPC network -> IP addresses -> reserve the VM's external
address as **static**.

## 2. Point a hostname at it
Create a free name at duckdns.org (for example `ragra-reminder`) and set its
IP to the VM's static address. Your hostname is `ragra-reminder.duckdns.org`.

## 3. Prepare the server (SSH button in the console)
```
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER && newgrp docker
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
sudo apt-get install -y git
git clone https://github.com/Ragra-Dev/Ragra-Reminder.git && cd Ragra-Reminder
```
The swap file matters: the VM has 1 GB of memory and the image build needs more.

## 4. Configure
```
cp deploy.env.example .env
docker compose run --rm --no-deps --entrypoint "" app python -m ragra.cli generate-credential-key
nano .env
```
Fill in every value (the generated key goes in `RAGRA_CREDENTIAL_KEY`, and
replace each `<RAGRA_DOMAIN>` with your hostname). Keep a private copy of the key.

## 5. Start
```
docker compose up -d --build
docker compose logs -f app
```

## 6. Google Cloud Console
Credentials -> your OAuth client -> Authorized redirect URIs: add
`https://<hostname>/auth/callback` and `https://<hostname>/account/google/callback`.

## Operating it
- Update: `git pull && docker compose up -d --build`
- Logs: `docker compose logs --tail 100 app`
- Back up the database:
  `docker compose cp app:/data/ragra.db ./ragra-backup.db`
