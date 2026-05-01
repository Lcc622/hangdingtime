import argparse
import csv
import json
import posixpath
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from typing import Any

import requests
import eccang_auth


BASE_URL = "https://everpretty-eb.eccang.com"
LIST_PATH = "/product/amazon-merchant-list/list/page/{page}/pageSize/{page_size}"
SAVE_PATH = "/product/amazon-merchant-list/save-supply-type"

# 这里默认使用 10.py 抓包里的 SKU。也可以运行时直接传 SKU 覆盖：
# python3 update_handing_time.py EE00466BD06-USA EA02715KG00-USA --handing-time 2
DEFAULT_SELLER_SKUS = [
    "EA02333BD00-USA",
    "EA02333NB00-USA",
    "EA02333OD00-USA",
    "EA02333PK00-USA",
    "EA02715KG00-USA",
    "EA02719BK00-USA",
]

DEFAULT_USER_ACCOUNT = "AmazonEPUS"
DEFAULT_HANDING_TIME = "2"
DEFAULT_SUPPLY_TYPE = "1"
DEFAULT_STATUS = "1"
DEFAULT_TIMEOUT = 90
DEFAULT_RETRIES = 3
DEFAULT_SUPPLY_WAREHOUSES = [
    "CN_WAREHOUSE",
    "US_WAREHOUSE",
    "YKD_US_WAREHOUSE",
    "YKD_USSC_WAREHOUSE",
    "YKD_USCE_WAREHOUSE",
]

EXCEL_HEADER_ALIASES = {
    "sku",
    "msku",
    "sellersku",
    "seller",
    "店铺sku",
    "平台sku",
    "卖家sku",
    "商品sku",
}

XML_NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

COOKIES = {}

HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": BASE_URL,
    "Pragma": "no-cache",
    "Referer": "https://everpretty-eb.eccang.com/product/amazon-merchant-list/list?resource=sso&token=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJjdXJyZW50VGltZU1pbGxpcyI6IjE3Nzc1MTQ3OTU5ODEiLCJleHAiOjE3Nzc1ODY3OTUsImFjY291bnQiOiJldmVycHJldHR5I0lBTVdMWFFafjQzMTAzN35TU09fU1lTX1VTRVIifQ.MbU39GcoI0WtSqA2a8u60yzFs4lsQOZxYEDMNcKuwvY&subjectCodeEncrypt=everpretty",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}


def split_values(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        for item in value.replace(",", " ").split():
            item = item.strip()
            if item:
                result.append(item)
    return result


def normalize_excel_header(value: str) -> str:
    return re.sub(r"[\s_\-:/\\（）()]+", "", value.strip().lower())


def excel_col_to_index(col: str) -> int:
    index = 0
    for char in col.upper():
        if not ("A" <= char <= "Z"):
            raise ValueError(f"Excel 列名不合法：{col}")
        index = index * 26 + ord(char) - ord("A") + 1
    return index - 1


def normalize_xlsx_path(path: str) -> str:
    path = path.lstrip("/")
    if path.startswith("xl/"):
        return path
    return posixpath.normpath(posixpath.join("xl", path))


def read_shared_strings(workbook: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []

    root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
    values = []
    for item in root.findall("main:si", XML_NS):
        texts = [node.text or "" for node in item.findall(".//main:t", XML_NS)]
        values.append("".join(texts))
    return values


def xlsx_sheet_path(workbook: zipfile.ZipFile, sheet: str | None) -> str:
    workbook_root = ET.fromstring(workbook.read("xl/workbook.xml"))
    rels_root = ET.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
    rel_targets = {
        rel.attrib["Id"]: normalize_xlsx_path(rel.attrib["Target"])
        for rel in rels_root.findall("pkgrel:Relationship", XML_NS)
    }

    sheets = []
    for sheet_node in workbook_root.findall("main:sheets/main:sheet", XML_NS):
        rel_id = sheet_node.attrib.get(f"{{{XML_NS['rel']}}}id")
        if rel_id not in rel_targets:
            continue
        sheets.append(
            {
                "name": sheet_node.attrib.get("name", ""),
                "path": rel_targets[rel_id],
            }
        )

    if not sheets:
        raise RuntimeError("Excel 文件里没有找到工作表。")
    if not sheet:
        return sheets[0]["path"]
    if sheet.isdigit():
        sheet_index = int(sheet) - 1
        if 0 <= sheet_index < len(sheets):
            return sheets[sheet_index]["path"]
        raise RuntimeError(f"Excel 第 {sheet} 个工作表不存在。")

    for item in sheets:
        if item["name"] == sheet:
            return item["path"]
    names = ", ".join(item["name"] for item in sheets)
    raise RuntimeError(f"Excel 工作表 {sheet!r} 不存在。可用工作表：{names}")


def cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//main:t", XML_NS)).strip()

    value_node = cell.find("main:v", XML_NS)
    if value_node is None or value_node.text is None:
        return ""
    value = value_node.text.strip()
    if cell_type == "s":
        try:
            return shared_strings[int(value)].strip()
        except (ValueError, IndexError):
            return ""
    return value


def read_xlsx_rows(path: str, sheet: str | None = None) -> list[list[str]]:
    if not path.lower().endswith(".xlsx"):
        raise RuntimeError("当前只支持 .xlsx 文件；如果是 .xls，请先另存为 .xlsx。")

    with zipfile.ZipFile(path) as workbook:
        shared_strings = read_shared_strings(workbook)
        sheet_xml = workbook.read(xlsx_sheet_path(workbook, sheet))
        root = ET.fromstring(sheet_xml)

    rows: list[list[str]] = []
    for row_node in root.findall(".//main:sheetData/main:row", XML_NS):
        row_values: dict[int, str] = {}
        next_col = 0
        for cell in row_node.findall("main:c", XML_NS):
            ref = cell.attrib.get("r", "")
            match = re.match(r"([A-Z]+)", ref)
            col_index = excel_col_to_index(match.group(1)) if match else next_col
            row_values[col_index] = cell_text(cell, shared_strings)
            next_col = col_index + 1

        if not row_values:
            rows.append([])
            continue
        max_col = max(row_values)
        rows.append([row_values.get(i, "") for i in range(max_col + 1)])
    return rows


def find_sku_column(rows: list[list[str]], sku_column: str | None, source_name: str) -> tuple[int, int]:
    if sku_column:
        if re.fullmatch(r"[A-Za-z]+", sku_column.strip()):
            col_index = excel_col_to_index(sku_column.strip())
            for row_index, row in enumerate(rows[:10]):
                if col_index < len(row) and normalize_excel_header(row[col_index]) in EXCEL_HEADER_ALIASES:
                    return col_index, row_index + 1
            return col_index, 0

        wanted = normalize_excel_header(sku_column)
        for row_index, row in enumerate(rows[:10]):
            for col_index, value in enumerate(row):
                if normalize_excel_header(value) == wanted:
                    return col_index, row_index + 1
        raise RuntimeError(f"{source_name} 前 10 行没有找到列名：{sku_column}")

    for row_index, row in enumerate(rows[:10]):
        for col_index, value in enumerate(row):
            if normalize_excel_header(value) in EXCEL_HEADER_ALIASES:
                return col_index, row_index + 1

    for col_index in range(max((len(row) for row in rows), default=0)):
        if any(col_index < len(row) and row[col_index].strip() for row in rows):
            return col_index, 0

    raise RuntimeError(f"{source_name} 里没有找到可读取的 SKU 列。")


def read_excel_skus(path: str, sheet: str | None, sku_column: str | None) -> list[str]:
    rows = read_xlsx_rows(path, sheet)
    col_index, start_row = find_sku_column(rows, sku_column, "Excel")
    return values_from_column(rows, col_index, start_row)


def read_csv_rows(path: str) -> list[list[str]]:
    encodings = ["utf-8-sig", "gb18030"]
    last_error = None
    for encoding in encodings:
        try:
            with open(path, newline="", encoding=encoding) as f:
                return [[cell.strip() for cell in row] for row in csv.reader(f)]
        except UnicodeDecodeError as exc:
            last_error = exc
    raise RuntimeError(f"CSV 编码读取失败：{last_error}")


def read_csv_skus(path: str, sku_column: str | None) -> list[str]:
    rows = read_csv_rows(path)
    col_index, start_row = find_sku_column(rows, sku_column, "CSV")
    return values_from_column(rows, col_index, start_row)


def values_from_column(rows: list[list[str]], col_index: int, start_row: int) -> list[str]:
    skus = []
    for row in rows[start_row:]:
        if col_index >= len(row):
            continue
        value = row[col_index].strip()
        if value:
            skus.extend(split_values([value]))
    return skus


def load_skus(args: argparse.Namespace) -> list[str]:
    skus = split_values(args.skus) if args.skus else []
    if args.sku_file:
        with open(args.sku_file, encoding="utf-8") as f:
            skus.extend(split_values(f.readlines()))
    if args.csv:
        skus.extend(read_csv_skus(args.csv, args.sku_column))
    if args.excel:
        skus.extend(read_excel_skus(args.excel, args.sheet, args.sku_column))
    if not skus:
        skus = list(DEFAULT_SELLER_SKUS)

    seen = set()
    deduped = []
    for sku in skus:
        if sku not in seen:
            seen.add(sku)
            deduped.append(sku)
    if args.offset:
        deduped = deduped[args.offset :]
    if args.limit:
        deduped = deduped[: args.limit]
    return deduped


def parse_cookie_header(raw_cookie: str) -> dict[str, str]:
    cookies = {}
    for part in raw_cookie.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        cookies[key.strip()] = value.strip()
    return cookies


def make_session(
    cookie_header: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
) -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    session.request_timeout = timeout
    session.request_retries = retries
    if cookie_header:
        session.cookies.update(parse_cookie_header(cookie_header))
    else:
        # 没传 --cookie 时走 eccang_auth 自动登录（缓存有效则跳过 Playwright）
        if not eccang_auth.login(session, domain='eb'):
            raise RuntimeError("eccang_auth 登录失败，请检查账号密码或网络")
    return session


def search_payload(skus: list[str], user_account: str) -> dict[str, str]:
    return {
        "item_status": "",
        "fulfillment_type": "",
        "supply_type_input": "",
        "supply_status_input": "",
        "is_b2b": "",
        "is_lt_suggest_price": "",
        "is_lt_listing_price": "",
        "user_account[]": user_account,
        "platform_sku_bind_user": "",
        "type": "seller_sku_arr",
        "code": " ".join(skus),
        "sync_status": "",
        "ps_id": "",
        "sell_qty_from": "",
        "sell_qty_to": "",
        "sold_qty_from": "",
        "sold_qty_to": "",
        "search_time": "add_time",
        "sendDateFrom": "",
        "sendDateEnd": "",
        "sort_time": "open_date",
        "sort_code": "asc",
        "remark": "",
    }


def post_json(session: requests.Session, path: str, data: Any) -> dict[str, Any]:
    timeout = getattr(session, "request_timeout", DEFAULT_TIMEOUT)
    retries = getattr(session, "request_retries", DEFAULT_RETRIES)
    last_error = None
    relogin_attempted = False
    for attempt in range(1, retries + 1):
        try:
            response = session.post(f"{BASE_URL}{path}", data=data, timeout=timeout)
            # 先判定 cookie 是否失效（不要 raise_for_status，因为登录页可能返回 200 HTML）
            if eccang_auth.is_session_expired(response):
                if relogin_attempted:
                    raise RuntimeError(f"接口 {path} 重登后仍返回登录失效响应：{response.text[:500]}")
                print(f"[登录失效] {path} 检测到 cookie 失效，正在重新登录...")
                if not eccang_auth.relogin(session, domain='eb'):
                    raise RuntimeError("自动重登失败")
                relogin_attempted = True
                continue  # 不计入 attempt，直接重试
            response.raise_for_status()
            try:
                return response.json()
            except ValueError as exc:
                raise RuntimeError(f"接口没有返回 JSON：{response.text[:500]}") from exc
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= retries:
                break
            wait_seconds = attempt * 3
            print(f"[重试] {path} 第 {attempt}/{retries} 次请求失败：{exc}，{wait_seconds}s 后重试...")
            time.sleep(wait_seconds)
    raise last_error


def listing_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data", {})
    if isinstance(data, dict):
        rows = []
        for listing_id, row in data.items():
            if isinstance(row, dict):
                row.setdefault("listing_id", listing_id)
                rows.append(row)
        return rows
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def payload_total(payload: dict[str, Any]) -> int:
    try:
        return int(payload.get("total") or 0)
    except (TypeError, ValueError):
        return 0


def find_listings(
    session: requests.Session,
    skus: list[str],
    user_account: str,
    page_size: int,
) -> list[dict[str, Any]]:
    wanted = set(skus)
    results: list[dict[str, Any]] = []
    seen = set()
    page = 1

    while True:
        path = LIST_PATH.format(page=page, page_size=page_size)
        payload = post_json(session, path, search_payload(skus, user_account))
        if payload.get("state") != 1:
            raise RuntimeError(f"查询失败：{payload}")

        rows = listing_rows(payload)
        total = payload_total(payload)
        for row in rows:
            seller_sku = str(row.get("seller_sku") or row.get("seller_sku_org") or row.get("sku") or "")
            listing_id = str(row.get("listing_id") or row.get("platform_sku") or "")
            listing_pk = str(row.get("id") or "")
            account = str(row.get("user_account") or row.get("acc") or user_account)
            if not seller_sku or seller_sku not in wanted:
                continue
            if not listing_id or not listing_pk:
                print(f"[跳过] {seller_sku} 缺少 listing_id 或 id：{row}", file=sys.stderr)
                continue

            key = (listing_id, listing_pk, seller_sku, account)
            if key in seen:
                continue
            seen.add(key)
            results.append(row)

        if not rows or len(rows) < page_size:
            break
        if total and page * page_size >= total:
            break
        page += 1

    return results


def first_text(*values: Any, default: str = "") -> str:
    for value in values:
        if value is None:
            continue
        text = str(value)
        if text and text != "--":
            return text
    return default


def warehouse_values(row: dict[str, Any], override: list[str] | None) -> list[str]:
    if override:
        return override

    raw = row.get("supplySet", {}).get("supply_warehouse") if isinstance(row.get("supplySet"), dict) else None
    if isinstance(raw, list):
        values = [str(item) for item in raw if item]
        return values or list(DEFAULT_SUPPLY_WAREHOUSES)
    if isinstance(raw, str) and raw and raw != "--":
        values = split_values([raw])
        values = [value for value in values if "WAREHOUSE" in value]
        return values or list(DEFAULT_SUPPLY_WAREHOUSES)
    return list(DEFAULT_SUPPLY_WAREHOUSES)


def supply_set(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("supplySet")
    return value if isinstance(value, dict) else {}


def add_supply_fields(
    data: list[tuple[str, str]],
    row: dict[str, Any],
    handing_time: str,
    warehouses: list[str] | None,
    supply_type: str | None,
    status: str | None,
) -> None:
    ss = supply_set(row)
    listing_id = str(row.get("listing_id"))
    seller_sku = str(row.get("seller_sku") or row.get("seller_sku_org") or row.get("sku"))
    listing_pk = str(row.get("id"))
    user_account = str(row.get("user_account") or row.get("acc") or DEFAULT_USER_ACCOUNT)
    prefix = f"pu[{listing_id}]"

    data.extend(
        [
            (f"{prefix}[handing_time]", handing_time),
            (f"{prefix}[supply_type]", first_text(supply_type, ss.get("supply_type"), default=DEFAULT_SUPPLY_TYPE)),
        ]
    )
    for warehouse in warehouse_values(row, warehouses):
        data.append((f"{prefix}[supply_warehouse][]", warehouse))

    data.extend(
        [
            (f"{prefix}[supply_qty]", first_text(ss.get("supply_qty"), default="")),
            (f"{prefix}[compare_left_op]", first_text(ss.get("compare_left_op"), default="gt")),
            (f"{prefix}[compare_left_op_qty]", first_text(ss.get("compare_left_op_qty"), default="")),
            (f"{prefix}[compare_left_ac]", first_text(ss.get("compare_left_ac"), default="eq")),
            (f"{prefix}[compare_left_ac_qty]", first_text(ss.get("compare_left_ac_qty"), default="")),
            (f"{prefix}[compare_right_op]", first_text(ss.get("compare_right_op"), default="lt")),
            (f"{prefix}[compare_right_op_qty]", first_text(ss.get("compare_right_op_qty"), default="")),
            (f"{prefix}[compare_right_ac]", first_text(ss.get("compare_right_ac"), default="eq")),
            (f"{prefix}[compare_right_ac_qty]", first_text(ss.get("compare_right_ac_qty"), default="")),
            (f"{prefix}[status]", first_text(status, ss.get("status"), default=DEFAULT_STATUS)),
            (f"{prefix}[seller_sku]", seller_sku),
            (f"{prefix}[id]", listing_pk),
            (f"{prefix}[user_account]", user_account),
        ]
    )


def build_update_payload(
    rows: list[dict[str, Any]],
    handing_time: str,
    warehouses: list[str] | None,
    supply_type: str | None,
    status: str | None,
) -> list[tuple[str, str]]:
    data = [("replace_all", "0")]
    for row in rows:
        add_supply_fields(data, row, handing_time, warehouses, supply_type, status)
    return data


def chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def find_listings_in_chunks(
    session: requests.Session,
    skus: list[str],
    user_account: str,
    page_size: int,
    query_chunk_size: int,
) -> list[dict[str, Any]]:
    results = []
    seen = set()
    sku_groups = chunks(skus, query_chunk_size)
    for index, sku_group in enumerate(sku_groups, start=1):
        print(f"查询第 {index}/{len(sku_groups)} 批 SKU，数量 {len(sku_group)}...")
        rows = find_listings(session, sku_group, user_account, page_size)
        for row in rows:
            key = (
                str(row.get("listing_id") or ""),
                str(row.get("id") or ""),
                str(row.get("seller_sku") or row.get("seller_sku_org") or row.get("sku") or ""),
                str(row.get("user_account") or row.get("acc") or user_account),
            )
            if key in seen:
                continue
            seen.add(key)
            results.append(row)
    return results


def seller_sku_of(row: dict[str, Any]) -> str:
    return str(row.get("seller_sku") or row.get("seller_sku_org") or row.get("sku") or "")


def listing_key(row: dict[str, Any], user_account: str) -> tuple[str, str, str, str]:
    return (
        str(row.get("listing_id") or ""),
        str(row.get("id") or ""),
        seller_sku_of(row),
        str(row.get("user_account") or row.get("acc") or user_account),
    )


def save_listing_rows(
    session: requests.Session,
    rows: list[dict[str, Any]],
    handing_time: str,
    warehouses: list[str] | None,
    supply_type: str | None,
    status: str | None,
    chunk_size: int,
) -> tuple[int, int]:
    ok = 0
    failed = 0
    for index, group in enumerate(chunks(rows, chunk_size), start=1):
        payload = build_update_payload(group, handing_time, warehouses, supply_type, status)
        result = post_json(session, SAVE_PATH, payload)
        if is_success_result(result):
            ok += len(group)
            print(f"[成功] 保存批次 {index}，{len(group)} 条：{json.dumps(result, ensure_ascii=False)}", flush=True)
        else:
            failed += len(group)
            print(f"[失败] 保存批次 {index}，{len(group)} 条：{json.dumps(result, ensure_ascii=False)}", file=sys.stderr, flush=True)
    return ok, failed


def print_missing_summary(missing: list[str]) -> None:
    if not missing:
        return
    print(f"未查到 {len(missing)} 个 seller_sku，已跳过。")
    for sku in missing[:20]:
        print(f"  {sku}")
    if len(missing) > 20:
        print(f"  ... 还有 {len(missing) - 20} 个未列出")


def print_preview(rows: list[dict[str, Any]], handing_time: str) -> None:
    print(f"将更新 {len(rows)} 条 listing，handing_time={handing_time}")
    max_preview = 50
    for row in rows[:max_preview]:
        ss = supply_set(row)
        print(
            "  "
            f"listing_id={row.get('listing_id')} "
            f"id={row.get('id')} "
            f"seller_sku={row.get('seller_sku') or row.get('sku')} "
            f"user_account={row.get('user_account') or row.get('acc')} "
            f"current_handing_time={ss.get('handing_time') or ''}"
        )
    if len(rows) > max_preview:
        print(f"  ... 还有 {len(rows) - max_preview} 条未列出")


def is_success_result(result: dict[str, Any]) -> bool:
    if result.get("state") == 1:
        return True
    if result.get("ask") == 1:
        return True
    return str(result.get("message", "")).lower() == "success"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="批量查询易仓 Amazon listing，并更新 handing_time。")
    parser.add_argument("skus", nargs="*", help="seller_sku，支持空格或逗号分隔。不传则使用脚本 DEFAULT_SELLER_SKUS。")
    parser.add_argument("--sku-file", help="从文件读取 seller_sku，一行一个或用空格/逗号分隔。")
    parser.add_argument("--csv", help="从 CSV 文件读取 seller_sku。")
    parser.add_argument("--excel", help="从 .xlsx Excel 文件读取 seller_sku。")
    parser.add_argument("--sheet", help="Excel 工作表名或序号，默认读取第 1 个工作表。")
    parser.add_argument("--sku-column", help="CSV/Excel SKU 列名或列字母，例如 seller_sku、SKU、店铺SKU、A、B。")
    parser.add_argument("--limit", type=int, help="只处理前 N 个去重后的 SKU，适合先挑几条测试。")
    parser.add_argument("--account", default=DEFAULT_USER_ACCOUNT, help="易仓 user_account，默认 AmazonEPUS。")
    parser.add_argument("--handing-time", "--handling-time", dest="handing_time", default=DEFAULT_HANDING_TIME)
    parser.add_argument("--offset", type=int, default=0, help="Skip N deduped SKUs before applying limit.")
    parser.add_argument("--page-size", type=int, default=200, help="查询列表接口 pageSize。")
    parser.add_argument("--query-chunk-size", type=int, default=150, help="每批拿多少个 SKU 去查询易仓列表。")
    parser.add_argument("--chunk-size", type=int, default=150, help="保存接口每批提交多少条 listing。")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="单次请求超时时间，单位秒。")
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="请求失败时重试次数。")
    parser.add_argument("--cookie", help="浏览器复制出来的 Cookie 字符串；传入后会覆盖脚本里的同名 cookie。")
    parser.add_argument("--warehouses", nargs="*", help="覆盖供货仓库，支持空格或逗号分隔。")
    parser.add_argument("--supply-type", help="覆盖 supply_type；不传则优先沿用列表里的值，空值用抓包默认 1。")
    parser.add_argument("--status", help="覆盖 status；不传则优先沿用列表里的值，空值用抓包默认 1。")
    parser.add_argument("--dry-run", action="store_true", help="只查询和打印将提交的数据，不真正保存。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.offset < 0:
        print("offset must be greater than or equal to 0.", file=sys.stderr)
        return 2
    if args.limit is not None and args.limit <= 0:
        print("limit 必须大于 0。", file=sys.stderr)
        return 2

    skus = load_skus(args)
    if not skus:
        print("没有 seller_sku 可处理。", file=sys.stderr)
        return 2
    if args.page_size <= 0 or args.query_chunk_size <= 0 or args.chunk_size <= 0:
        print("page-size、query-chunk-size 和 chunk-size 必须大于 0。", file=sys.stderr)
        return 2
    if args.timeout <= 0 or args.retries <= 0:
        print("timeout 和 retries 必须大于 0。", file=sys.stderr)
        return 2

    warehouses = split_values(args.warehouses) if args.warehouses else None
    session = make_session(args.cookie, timeout=args.timeout, retries=args.retries)

    print(f"开始查询 {len(skus)} 个 seller_sku...")
    if not args.dry_run:
        sku_groups = chunks(skus, args.query_chunk_size)
        seen_rows = set()
        total_found = 0
        total_missing = 0
        ok = 0
        failed = 0
        for index, sku_group in enumerate(sku_groups, start=1):
            print(f"查询第 {index}/{len(sku_groups)} 批 SKU，数量 {len(sku_group)}...", flush=True)
            rows = find_listings(session, sku_group, args.account, args.page_size)
            new_rows = []
            for row in rows:
                key = listing_key(row, args.account)
                if key in seen_rows:
                    continue
                seen_rows.add(key)
                new_rows.append(row)

            found_skus = {seller_sku_of(row) for row in new_rows}
            missing = [sku for sku in sku_group if sku not in found_skus]
            total_found += len(new_rows)
            total_missing += len(missing)
            print(
                f"第 {index}/{len(sku_groups)} 批：查到 {len(new_rows)} 条，"
                f"跳过 {len(missing)} 个未查到 SKU，累计查到 {total_found} 条。",
                flush=True,
            )

            if not new_rows:
                continue
            batch_ok, batch_failed = save_listing_rows(
                session,
                new_rows,
                args.handing_time,
                warehouses,
                args.supply_type,
                args.status,
                args.chunk_size,
            )
            ok += batch_ok
            failed += batch_failed
            print(f"累计保存：成功 {ok} 条，失败 {failed} 条。", flush=True)

        print(f"完成：成功 {ok} 条，失败 {failed} 条，未查到并跳过 {total_missing} 个 SKU。")
        return 0 if failed == 0 else 1

    rows = find_listings_in_chunks(session, skus, args.account, args.page_size, args.query_chunk_size)
    found_skus = {seller_sku_of(row) for row in rows}
    missing = [sku for sku in skus if sku not in found_skus]
    print_missing_summary(missing)

    if not rows:
        print("没有查到可更新的 listing。")
        return 1

    print_preview(rows, args.handing_time)
    if args.dry_run:
        payload = build_update_payload(rows[:1], args.handing_time, warehouses, args.supply_type, args.status)
        print("dry-run：第一条 listing 将提交的表单字段如下：")
        for key, value in payload:
            print(f"  {key} = {value}")
        return 0

    ok, failed = save_listing_rows(
        session,
        rows,
        args.handing_time,
        warehouses,
        args.supply_type,
        args.status,
        args.chunk_size,
    )
    print(f"完成：成功 {ok} 条，失败 {failed} 条。")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
