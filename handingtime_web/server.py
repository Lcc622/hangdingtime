from __future__ import annotations

import csv
import importlib.util
import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
JOBS_DIR = DATA_DIR / "jobs"
SCRIPT_DIR = ROOT / "EPUS_2ht"
SCRIPT_PATH = SCRIPT_DIR / "update_handing_time(2).py"

DEFAULT_LOGIN_USER = os.environ.get("ECCANG_USER", "CNSZ401")
DEFAULT_BATCH_SIZE = 500
DEFAULT_QUERY_CHUNK_SIZE = 150
DEFAULT_SAVE_CHUNK_SIZE = 150
WEB_TOKEN = os.environ.get("HT_WEB_TOKEN", "")

ACCOUNT_PRESETS = {
    "EPUS": "AmazonEPUS",
    "DAMAUS": "Amazon_PZnew_US_US",
}

JOBS_DIR.mkdir(parents=True, exist_ok=True)


def load_handingtime_module() -> Any:
    sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location("handingtime_update", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load script: {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HT = load_handingtime_module()


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def split_skus(raw: str) -> list[str]:
    values: list[str] = []
    for item in re.split(r"[\s,，;；]+", raw):
        item = item.strip()
        if item:
            values.append(item)
    deduped: list[str] = []
    seen: set[str] = set()
    for sku in values:
        if sku not in seen:
            seen.add(sku)
            deduped.append(sku)
    return deduped


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


@dataclass
class Job:
    id: str
    account: str
    handing_time: str
    skus_total: int
    dry_run: bool = False
    batch_size: int = DEFAULT_BATCH_SIZE
    status: str = "queued"
    created_at: str = field(default_factory=now_text)
    started_at: str | None = None
    finished_at: str | None = None
    processed: int = 0
    found: int = 0
    saved_ok: int = 0
    save_failed: int = 0
    not_found: int = 0
    current_offset: int = 0
    error: str = ""
    log_path: str = ""
    result_path: str = ""
    not_found_path: str = ""
    failed_path: str = ""


jobs: dict[str, Job] = {}
job_logs: dict[str, queue.Queue[str]] = {}
jobs_lock = threading.Lock()


def append_log(job: Job, message: str) -> None:
    line = f"[{now_text()}] {message}"
    q = job_logs.get(job.id)
    if q:
        q.put(line)
    if job.log_path:
        with open(job.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def find_listings_for_group(session: Any, skus: list[str], account: str, page_size: int) -> list[dict[str, Any]]:
    try:
        return HT.find_listings(session, skus, account, page_size)
    except RuntimeError as exc:
        text = str(exc)
        if "No Data" in text or "'total': '0'" in text or '"total": "0"' in text:
            return []
        raise


def run_job(job_id: str, skus: list[str], login_user: str, login_pass: str) -> None:
    job = jobs[job_id]
    job_dir = JOBS_DIR / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    job.log_path = str(job_dir / "run.log")
    job.result_path = str(job_dir / "results.csv")
    job.not_found_path = str(job_dir / "not_found.csv")
    job.failed_path = str(job_dir / "failed.csv")

    result_rows: list[dict[str, Any]] = []
    missing_rows: list[dict[str, Any]] = []
    failed_rows: list[dict[str, Any]] = []

    os.environ["ECCANG_USER"] = login_user
    os.environ["ECCANG_PASS"] = login_pass

    with jobs_lock:
        job.status = "running"
        job.started_at = now_text()
    append_log(job, f"Started job. account={job.account}, handing_time={job.handing_time}, skus={len(skus)}, dry_run={job.dry_run}")

    try:
        session = HT.make_session(None, timeout=90, retries=3)
        groups = HT.chunks(skus, job.batch_size)
        for batch_index, group in enumerate(groups, start=1):
            offset = (batch_index - 1) * job.batch_size
            with jobs_lock:
                job.current_offset = offset
            append_log(job, f"Query batch {batch_index}/{len(groups)} offset={offset}, size={len(group)}")

            rows: list[dict[str, Any]] = []
            for query_group in HT.chunks(group, DEFAULT_QUERY_CHUNK_SIZE):
                rows.extend(find_listings_for_group(session, query_group, job.account, 200))

            seen_keys: set[tuple[str, str, str, str]] = set()
            deduped_rows: list[dict[str, Any]] = []
            for row in rows:
                key = HT.listing_key(row, job.account)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                deduped_rows.append(row)

            found_skus = {HT.seller_sku_of(row) for row in deduped_rows}
            missing = [sku for sku in group if sku not in found_skus]
            for sku in missing:
                missing_rows.append({"seller_sku": sku, "reason": "not_found"})

            with jobs_lock:
                job.processed += len(group)
                job.found += len(deduped_rows)
                job.not_found += len(missing)

            append_log(job, f"Batch {batch_index}: found={len(deduped_rows)}, not_found={len(missing)}")

            if not deduped_rows:
                continue

            if job.dry_run:
                for row in deduped_rows:
                    result_rows.append(
                        {
                            "seller_sku": HT.seller_sku_of(row),
                            "listing_id": row.get("listing_id", ""),
                            "id": row.get("id", ""),
                            "old_handing_time": HT.supply_set(row).get("handing_time", ""),
                            "new_handing_time": job.handing_time,
                            "status": "dry_run",
                            "message": "Not submitted",
                        }
                    )
                continue

            for save_group in HT.chunks(deduped_rows, DEFAULT_SAVE_CHUNK_SIZE):
                payload = HT.build_update_payload(save_group, job.handing_time, None, None, None)
                result = HT.post_json(session, HT.SAVE_PATH, payload)
                ok = HT.is_success_result(result)
                message = json.dumps(result, ensure_ascii=False)
                if ok:
                    with jobs_lock:
                        job.saved_ok += len(save_group)
                    append_log(job, f"Saved {len(save_group)} listings: {message}")
                    for row in save_group:
                        result_rows.append(
                            {
                                "seller_sku": HT.seller_sku_of(row),
                                "listing_id": row.get("listing_id", ""),
                                "id": row.get("id", ""),
                                "old_handing_time": HT.supply_set(row).get("handing_time", ""),
                                "new_handing_time": job.handing_time,
                                "status": "success",
                                "message": "Success",
                            }
                        )
                else:
                    with jobs_lock:
                        job.save_failed += len(save_group)
                    append_log(job, f"Save failed for {len(save_group)} listings: {message}")
                    for row in save_group:
                        failed_item = {
                            "seller_sku": HT.seller_sku_of(row),
                            "listing_id": row.get("listing_id", ""),
                            "id": row.get("id", ""),
                            "old_handing_time": HT.supply_set(row).get("handing_time", ""),
                            "new_handing_time": job.handing_time,
                            "status": "failed",
                            "message": message,
                        }
                        failed_rows.append(failed_item)
                        result_rows.append(failed_item)

        write_csv(Path(job.result_path), result_rows, ["seller_sku", "listing_id", "id", "old_handing_time", "new_handing_time", "status", "message"])
        write_csv(Path(job.not_found_path), missing_rows, ["seller_sku", "reason"])
        write_csv(Path(job.failed_path), failed_rows, ["seller_sku", "listing_id", "id", "old_handing_time", "new_handing_time", "status", "message"])
        with jobs_lock:
            job.status = "completed" if job.save_failed == 0 else "completed_with_failures"
            job.finished_at = now_text()
        append_log(job, f"Finished. success={job.saved_ok}, failed={job.save_failed}, not_found={job.not_found}")
    except Exception as exc:
        with jobs_lock:
            job.status = "failed"
            job.error = str(exc)
            job.finished_at = now_text()
        append_log(job, "Fatal error: " + str(exc))
        append_log(job, traceback.format_exc())
        if result_rows:
            write_csv(Path(job.result_path), result_rows, ["seller_sku", "listing_id", "id", "old_handing_time", "new_handing_time", "status", "message"])
        if missing_rows:
            write_csv(Path(job.not_found_path), missing_rows, ["seller_sku", "reason"])
        if failed_rows:
            write_csv(Path(job.failed_path), failed_rows, ["seller_sku", "listing_id", "id", "old_handing_time", "new_handing_time", "status", "message"])


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(APP_DIR / "static"), **kwargs)

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw else {}

    def authorized(self) -> bool:
        if not WEB_TOKEN:
            return True
        return self.headers.get("X-HT-Token", "") == WEB_TOKEN

    def require_auth(self) -> bool:
        if self.authorized():
            return True
        self.send_json({"error": "unauthorized"}, 401)
        return False

    def do_POST(self) -> None:
        if self.path != "/api/jobs":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self.require_auth():
            return

        try:
            payload = self.read_json()
            account = str(payload.get("account", "")).strip()
            handing_time = str(payload.get("handingTime", "")).strip()
            sku_text = str(payload.get("skuText", ""))
            login_user = str(payload.get("loginUser", DEFAULT_LOGIN_USER)).strip() or DEFAULT_LOGIN_USER
            login_pass = str(payload.get("loginPass", os.environ.get("ECCANG_PASS", "")))
            dry_run = bool(payload.get("dryRun", False))
            batch_size = int(payload.get("batchSize", DEFAULT_BATCH_SIZE))

            if not account:
                self.send_json({"error": "account is required"}, 400)
                return
            if not handing_time.isdigit() or int(handing_time) <= 0:
                self.send_json({"error": "handingTime must be a positive integer"}, 400)
                return
            if batch_size <= 0 or batch_size > 1000:
                self.send_json({"error": "batchSize must be between 1 and 1000"}, 400)
                return
            skus = split_skus(sku_text)
            if not skus:
                self.send_json({"error": "No SKU provided"}, 400)
                return

            job_id = uuid.uuid4().hex[:12]
            job = Job(
                id=job_id,
                account=account,
                handing_time=handing_time,
                skus_total=len(skus),
                dry_run=dry_run,
                batch_size=batch_size,
            )
            with jobs_lock:
                jobs[job_id] = job
                job_logs[job_id] = queue.Queue()

            thread = threading.Thread(target=run_job, args=(job_id, skus, login_user, login_pass), daemon=True)
            thread.start()
            self.send_json({"job": asdict(job), "presets": ACCOUNT_PRESETS})
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/api/config":
            self.send_json({"accounts": ACCOUNT_PRESETS, "defaultLoginUser": DEFAULT_LOGIN_USER, "tokenRequired": bool(WEB_TOKEN)})
            return
        if path == "/api/jobs":
            if not self.require_auth():
                return
            with jobs_lock:
                items = [asdict(job) for job in jobs.values()]
            self.send_json({"jobs": items})
            return
        if path.startswith("/api/jobs/"):
            if not self.require_auth():
                return
            parts = path.strip("/").split("/")
            job_id = parts[2] if len(parts) >= 3 else ""
            with jobs_lock:
                job = jobs.get(job_id)
            if not job:
                self.send_json({"error": "job not found"}, 404)
                return
            if len(parts) == 3:
                self.send_json({"job": asdict(job)})
                return
            if len(parts) == 4 and parts[3] == "logs":
                limit = int(parse_qs(parsed.query).get("limit", ["500"])[0])
                log_path = Path(job.log_path) if job.log_path else None
                lines: list[str] = []
                if log_path and log_path.exists():
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
                self.send_json({"lines": lines})
                return
            if len(parts) == 5 and parts[3] == "download":
                name = parts[4]
                path_map = {
                    "log": job.log_path,
                    "results": job.result_path,
                    "not_found": job.not_found_path,
                    "failed": job.failed_path,
                }
                file_path = Path(path_map.get(name, ""))
                if not file_path.exists():
                    self.send_json({"error": "file not available"}, 404)
                    return
                content = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8" if name == "log" else "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{file_path.name}"')
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
        return super().do_GET()


def main() -> None:
    host = os.environ.get("HT_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("HT_WEB_PORT", "8765"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Handingtime console running at http://{host}:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
