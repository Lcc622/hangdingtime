# Handingtime Web Console Deployment

This app wraps the existing Eccang handing-time script with a local web console.

## Files Required On Server

Upload the whole `E:\hangdingtime` project or at least these folders:

- `EPUS_2ht`
- `EPUS_6ht` is not required on Linux except for the local Windows venv; do not upload `.venv`.
- `handingtime_web`

Recommended server path:

```bash
/opt/handingtime
```

## Install On Tencent Cloud Linux

```bash
sudo mkdir -p /opt/handingtime
sudo chown -R "$USER:$USER" /opt/handingtime

# Upload/copy project files into /opt/handingtime first.
cd /opt/handingtime

python3 -m venv venv
./venv/bin/pip install -U pip
./venv/bin/pip install -r handingtime_web/requirements.txt
./venv/bin/playwright install chromium
```

## Run Manually

```bash
cd /opt/handingtime
export ECCANG_USER='CNSZ401'
export ECCANG_PASS='your-password'
export HT_WEB_TOKEN='choose-a-long-random-token'
export HT_WEB_HOST='0.0.0.0'
export HT_WEB_PORT='8765'
./venv/bin/python -u handingtime_web/server.py
```

Open:

```text
http://SERVER_IP:8765
```

If using Tencent Cloud security groups, allow inbound TCP `8765`, or use Nginx reverse proxy on port `80/443`.

## Run With systemd

Edit:

```bash
sudo cp handingtime_web/deploy/handingtime-web.service /etc/systemd/system/handingtime-web.service
sudo nano /etc/systemd/system/handingtime-web.service
```

Set these values:

```text
Environment=ECCANG_USER=CNSZ401
Environment=ECCANG_PASS=your-password
Environment=HT_WEB_TOKEN=choose-a-long-random-token
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now handingtime-web
sudo systemctl status handingtime-web
```

Logs:

```bash
journalctl -u handingtime-web -f
```

## Nginx Reverse Proxy

Copy and edit:

```bash
sudo cp handingtime_web/deploy/nginx.conf /etc/nginx/conf.d/handingtime-web.conf
sudo nano /etc/nginx/conf.d/handingtime-web.conf
sudo nginx -t
sudo systemctl reload nginx
```

Keep app bound to `127.0.0.1:8765` when using Nginx.

## Runtime Data

Job logs and CSV results are saved under:

```text
/opt/handingtime/handingtime_web/data/jobs/<job_id>/
```

Each job writes:

- `run.log`
- `results.csv`
- `not_found.csv`
- `failed.csv`

## Account Mapping

- EPUS: `AmazonEPUS`
- DAMAUS: `Amazon_PZnew_US_US`
