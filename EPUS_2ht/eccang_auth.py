"""易仓 ERP 共享登录模块。

支持两个域：
- main: https://everpretty.eccang.com (头程/WMS 系统)
- eb:   https://everpretty-eb.eccang.com (商品/Listing 系统，走 SSO)

用法：
    import eccang_auth
    session = requests.Session()
    eccang_auth.login(session, domain='eb')   # 启动时
    # ... 调 API ...
    if eccang_auth.is_session_expired(resp):
        eccang_auth.relogin(session, domain='eb')
"""
import json
import os
import requests
from playwright.sync_api import sync_playwright


DOMAINS = {
    'main': 'http://everpretty.eccang.com',
    'eb': 'https://everpretty-eb.eccang.com',
}

HEALTH_CHECK_PATHS = {
    'main': '/system/home',
    'eb': '/product/amazon-merchant-list/list',
}

LOGIN_USER = os.environ.get('ECCANG_USER', 'CNSZ401')
LOGIN_PASS = os.environ.get('ECCANG_PASS', '')

_DEFAULT_DATA_DIR = os.environ.get('ECCANG_DATA_DIR') or os.path.dirname(os.path.abspath(__file__))
os.makedirs(_DEFAULT_DATA_DIR, exist_ok=True)


def _cookie_file(domain: str) -> str:
    suffix = '' if domain == 'main' else f'_{domain}'
    return os.path.join(_DEFAULT_DATA_DIR, f'.eccang_cookies{suffix}.json')


def _save_cookies(session: requests.Session, domain: str) -> None:
    cookies = {c.name: c.value for c in session.cookies}
    with open(_cookie_file(domain), 'w') as f:
        json.dump(cookies, f)


def _load_cookies(session: requests.Session, domain: str) -> bool:
    path = _cookie_file(domain)
    if not os.path.exists(path):
        return False
    try:
        with open(path) as f:
            cookies = json.load(f)
        session.cookies.update(cookies)
        return True
    except (json.JSONDecodeError, IOError):
        return False


def _check_session(session: requests.Session, domain: str) -> bool:
    try:
        url = DOMAINS[domain] + HEALTH_CHECK_PATHS[domain]
        resp = session.get(url, allow_redirects=False, timeout=10)
        if resp.status_code != 200:
            return False
        ct = resp.headers.get('Content-Type', '').lower()
        if 'text/html' in ct and 'login' in resp.text[:2000].lower():
            return False
        return True
    except Exception:
        return False


def is_session_expired(response: requests.Response) -> bool:
    """判定 API 响应是否表示 cookie 失效，触发 relogin 用。"""
    if response.status_code in (302, 401, 403):
        return True
    try:
        data = response.json()
    except ValueError:
        ct = response.headers.get('Content-Type', '').lower()
        if 'text/html' in ct:
            return True
        return False
    if data.get('state') in (1, '1') or data.get('ask') in (1, '1'):
        return False
    state = data.get('state')
    msg = str(data.get('message', '')).lower()
    if state in (-1, '-1') and ('登录' in msg or 'login' in msg or 'sign in' in msg):
        return True
    return False


def _login_via_playwright() -> dict[str, dict[str, str]]:
    """登录主站并触发 eb 子域 SSO，返回 {domain_key: {name: value}}."""
    print('  启动浏览器登录...')
    if not LOGIN_USER or not LOGIN_PASS:
        raise RuntimeError('ECCANG_USER and ECCANG_PASS must be set before automatic login.')
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        page.goto(f'{DOMAINS["main"]}/?company_code=everpretty')
        page.wait_for_load_state('networkidle')
        page.fill('#userName', LOGIN_USER)
        page.fill('#userPass', LOGIN_PASS)
        page.click('#login')
        page.wait_for_timeout(3000)
        page.wait_for_load_state('networkidle')

        try:
            page.goto(f'{DOMAINS["eb"]}/product/amazon-merchant-list/list')
            page.wait_for_load_state('networkidle', timeout=20000)
        except Exception as exc:
            print(f'  [warn] eb 子域跳转异常（继续收集 cookie）: {exc}')

        all_cookies = context.cookies()
        browser.close()

    grouped: dict[str, dict[str, str]] = {'main': {}, 'eb': {}}
    for c in all_cookies:
        d = c['domain'].lstrip('.')
        if 'everpretty-eb' in d:
            grouped['eb'][c['name']] = c['value']
        elif d.endswith('eccang.com'):
            grouped['main'][c['name']] = c['value']
            grouped['eb'].setdefault(c['name'], c['value'])
    return grouped


def login(session: requests.Session, domain: str = 'main') -> bool:
    """启动时调用：先试缓存，失效则触发 Playwright 重登。"""
    if _load_cookies(session, domain):
        print(f'  正在验证 {domain} 缓存登录状态...')
        if _check_session(session, domain):
            print(f'  {domain} session 有效，跳过登录!')
            return True
        print(f'  {domain} session 已失效，重新登录...')
        session.cookies.clear()

    return relogin(session, domain)


def relogin(session: requests.Session, domain: str = 'main') -> bool:
    """强制 Playwright 重登，刷新 session.cookies 并写缓存。"""
    print(f'  正在通过浏览器登录易仓 ERP（目标域: {domain}）...')
    grouped = _login_via_playwright()
    cookies = grouped.get(domain, {})
    if not cookies:
        print(f'  [error] {domain} 未取到 cookie')
        return False

    session.cookies.clear()
    session.cookies.update(cookies)

    if _check_session(session, domain):
        _save_cookies(session, domain)
        print(f'  {domain} 登录成功! (cookies 已缓存)')
        for other_domain, other_cookies in grouped.items():
            if other_domain != domain and other_cookies:
                with open(_cookie_file(other_domain), 'w') as f:
                    json.dump(other_cookies, f)
        return True
    print(f'  {domain} 登录失败：session 验证未通过')
    return False
