# Handingtime Web Console

Web console for batch updating Eccang Amazon listing `handing_time` by shop account, SKU list, and target days.

## Upload Filename Rule

Upload one or more `.csv`/`.txt` SKU files. The web console infers the shop account and target `handing_time` from each filename:

- `epus_instock_ht2.csv` -> `EPUS` -> `AmazonEPUS`, `handing_time=2`
- `damaus_outofstock_ht6.csv` -> `DAMAUS` -> `Amazon_PZnew_US_US`, `handing_time=6`

The first filename segment before `_`, `-`, or a space is the shop prefix. The `ht<number>` segment is the target day count.

## Shop Account Mapping

- EPUS: `AmazonEPUS`
- DAMAUS: `Amazon_PZnew_US_US`

## Local Run

```powershell
$env:ECCANG_USER='CNSZ401'
$env:ECCANG_PASS='your-password'
$env:HT_WEB_TOKEN='choose-a-long-random-token'
.\EPUS_6ht\.venv\Scripts\python.exe -u .\handingtime_web\server.py
```

Open:

```text
http://127.0.0.1:8765/handingtime/
```

## Server Deployment

See [handingtime_web/DEPLOY.md](handingtime_web/DEPLOY.md).

## Runtime Output

Each job writes logs and CSV results under:

```text
handingtime_web/data/jobs/<job_id>/
```
