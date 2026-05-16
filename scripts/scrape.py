"""
TVCF 신규 광고 크롤링 스크립트 v5 (Playwright)
- requests → Playwright (실제 브라우저로 봇 차단 우회)
- 핵심 기능 유지: staffs API, 광고주+날짜 중복 제거, 자동 매칭, 에러 기록
"""
import json
import os
import re
import time
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

MAIN_URL = "https://tvcf.co.kr/"
LIST_URL = "https://tvcf.co.kr/worked/cf"
STAFFS_API_TEMPLATE = "https://tvcf.co.kr/api/main/v1/play/{idx}/staffs"

LIST_PARAMS = {
    "mediaType_value": 1,
    "page": 1,
    "rows": 50,
    "sort_by": "registrated_date",
    "country_code_value": 410,
    "category_code_value": "0,1",
    "lang": "ko",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.5 Safari/605.1.15"
)

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
LATEST_FILE = DATA_DIR / "latest.json"
HISTORY_FILE = DATA_DIR / "history.json"
STATE_FILE = DATA_DIR / "state.json"
DECISIONS_FILE = DATA_DIR / "decisions.json"
ERROR_STATUS_FILE = DATA_DIR / "error_status.json"

DELAY_SEC = 1.0

_PW_PAGE = None
_PW_CONTEXT = None
_PW_BROWSER = None
_PW_PLAYWRIGHT = None


def now_kst():
    return datetime.now(KST)


def load_json(path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  {path} 읽기 실패: {e}")
        return default


def save_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_name(name):
    if not name:
        return ''
    s = str(name).lower()
    s = re.sub(r'\s+', '', s)
    s = re.sub(r'[()()\[\]【】「」\'\',·.\-_/]', '', s)
    s = re.sub(r'주식회사|㈜|\(주\)|inc\.?|corp\.?|ltd\.?|co\.?', '', s)
    return s.strip()


def load_master_advertisers():
    master_json = os.environ.get('MASTER_ADVERTISERS', '')
    if not master_json:
        print("⚠ MASTER_ADVERTISERS 환경변수 없음. 자동 매칭 건너뜀.")
        return []
    try:
        data = json.loads(master_json)
        advertisers = data.get('advertisers', [])
        print(f"📋 마스터 광고주 목록 로드: {len(advertisers)}명")
        return advertisers
    except Exception as e:
        print(f"⚠ MASTER_ADVERTISERS 파싱 실패: {e}")
        return []


def match_advertiser(name, master_list):
    if not name or not master_list:
        return None, None
    normalized = normalize_name(name)
    if not normalized:
        return None, None
    for m in master_list:
        if normalize_name(m) == normalized:
            return 'o', m
    for m in master_list:
        nm = normalize_name(m)
        if len(nm) > 1 and len(normalized) > 1:
            if nm in normalized or normalized in nm:
                return 'maybe', m
    return 'x', None


def auto_match_decisions(new_ads, master_list):
    if not master_list:
        return 0
    decisions_data = load_json(DECISIONS_FILE, {
        "updated_at": "", "count": 0, "x_count": 0,
        "decisions": {}, "x_decisions": {},
    })
    decisions = decisions_data.get('decisions', {})
    x_decisions = decisions_data.get('x_decisions', {})
    added_count = 0
    for ad in new_ads:
        name = ad.get('advertiser') or ad.get('brand')
        if not name:
            continue
        ad_id = str(ad.get('ad_id', ''))
        key = normalize_name(name)
        if not key:
            continue
        if key in decisions:
            continue
        if ad_id and ad_id in x_decisions:
            continue
        status, matched = match_advertiser(name, master_list)
        if status is None:
            continue
        now_iso = now_kst().isoformat()
        if status == 'x':
            if ad_id:
                x_decisions[ad_id] = {
                    "advertiser": name, "status": "x",
                    "updated_at": now_iso, "auto": True,
                }
                added_count += 1
        else:
            decisions[key] = {
                "display_name": name, "status": status,
                "matched_name": matched,
                "updated_at": now_iso, "auto": True,
            }
            added_count += 1
    if added_count == 0:
        return 0
    payload = {
        "updated_at": now_kst().isoformat(),
        "count": len(decisions), "x_count": len(x_decisions),
        "decisions": decisions, "x_decisions": x_decisions,
    }
    save_json(DECISIONS_FILE, payload)
    print(f"💾 decisions.json 업데이트: +{added_count}건 (영구 {len(decisions)} + 1회용 {len(x_decisions)})")
    return added_count


def init_browser():
    """Playwright 브라우저 초기화"""
    global _PW_PAGE, _PW_CONTEXT, _PW_BROWSER, _PW_PLAYWRIGHT
    if _PW_PAGE is not None:
        return _PW_PAGE
    print("   🌐 Playwright Chromium 실행 중...")
    _PW_PLAYWRIGHT = sync_playwright().start()
    _PW_BROWSER = _PW_PLAYWRIGHT.chromium.launch(
        headless=True,
        args=[
            '--disable-blink-features=AutomationControlled',
            '--no-sandbox',
            '--disable-dev-shm-usage',
        ]
    )
    _PW_CONTEXT = _PW_BROWSER.new_context(
        user_agent=USER_AGENT,
        viewport={'width': 1920, 'height': 1080},
        locale='ko-KR',
        timezone_id='Asia/Seoul',
        extra_http_headers={
            'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7',
        }
    )
    _PW_CONTEXT.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko', 'en-US', 'en']});
    """)
    _PW_PAGE = _PW_CONTEXT.new_page()
    print("   ✓ 브라우저 준비 완료")
    return _PW_PAGE


def close_browser():
    global _PW_PAGE, _PW_CONTEXT, _PW_BROWSER, _PW_PLAYWRIGHT
    try:
        if _PW_CONTEXT:
            _PW_CONTEXT.close()
        if _PW_BROWSER:
            _PW_BROWSER.close()
        if _PW_PLAYWRIGHT:
            _PW_PLAYWRIGHT.stop()
    except Exception:
        pass
    _PW_PAGE = _PW_CONTEXT = _PW_BROWSER = _PW_PLAYWRIGHT = None


def fetch_html(url, params=None):
    """Playwright로 페이지 HTML 가져오기 (재시도 포함)"""
    page = init_browser()
    if params:
        from urllib.parse import urlencode
        full_url = f"{url}?{urlencode(params)}"
    else:
        full_url = url
    max_retries = 3
    retry_delays = [60, 600]
    for attempt in range(1, max_retries + 1):
        try:
            print(f"     → GOTO {full_url}")
            response = page.goto(full_url, wait_until='domcontentloaded', timeout=30000)
            if response is None:
                raise RuntimeError("No response")
            status = response.status
            print(f"     status: {status}")
            if 500 <= status < 600 and attempt < max_retries:
                wait = retry_delays[attempt - 1]
                wait_label = f"{wait}초" if wait < 60 else f"{wait//60}분"
                print(f"     ⚠ 서버 에러 {status} (시도 {attempt}/{max_retries}). {wait_label} 대기 후 재시도...")
                time.sleep(wait)
                continue
            if status >= 400:
                raise RuntimeError(f"HTTP {status} for {full_url}")
            try:
                page.wait_for_load_state('networkidle', timeout=10000)
            except PlaywrightTimeout:
                pass
            html = page.content()
            print(f"     length: {len(html)} bytes")
            return html
        except PlaywrightTimeout as e:
            if attempt < max_retries:
                wait = retry_delays[attempt - 1]
                wait_label = f"{wait}초" if wait < 60 else f"{wait//60}분"
                print(f"     ⚠ 타임아웃 (시도 {attempt}/{max_retries}). {wait_label} 대기 후 재시도...")
                time.sleep(wait)
            else:
                raise
        except Exception as e:
            if attempt < max_retries:
                wait = retry_delays[attempt - 1]
                wait_label = f"{wait}초" if wait < 60 else f"{wait//60}분"
                print(f"     ⚠ 에러 (시도 {attempt}/{max_retries}): {type(e).__name__}: {e}. {wait_label} 대기 후 재시도...")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("fetch_html failed after retries")


def fetch_json_api(url):
    """JSON API 호출"""
    page = init_browser()
    try:
        response = page.goto(url, wait_until='domcontentloaded', timeout=15000)
        if response is None or response.status >= 400:
            return None
        body_text = page.evaluate('document.body.innerText')
        try:
            return json.loads(body_text)
        except (json.JSONDecodeError, TypeError):
            return None
    except Exception as e:
        print(f"    ⚠️  API 호출 실패: {e}")
        return None


def init_session():
    """메인 페이지 먼저 방문"""
    print(f"   🔓 세션 초기화 (메인 페이지 방문)...")
    try:
        page = init_browser()
        response = page.goto(MAIN_URL, wait_until='domcontentloaded', timeout=20000)
        status = response.status if response else 'unknown'
        cookies = _PW_CONTEXT.cookies()
        print(f"     status: {status}, 받은 쿠키: {len(cookies)}개")
        if cookies:
            cookie_names = ", ".join(c['name'] for c in cookies[:5])
            print(f"     쿠키 종류: {cookie_names}")
        time.sleep(2)
        return True
    except Exception as e:
        print(f"     ⚠ 세션 초기화 실패 (계속 진행): {type(e).__name__}: {e}")
        return False


def fetch_list():
    html = fetch_html(LIST_URL, params=LIST_PARAMS)
    soup = BeautifulSoup(html, "html.parser")
    play_links = soup.find_all("a", href=re.compile(r"^/play/"))
    print(f"     /play/ 링크 총 {len(play_links)}개 발견")
    ad_map = {}
    for a in play_links:
        href = a.get("href", "")
        m = re.match(r"^/play/([a-z]+\d+)-(\d+)$", href)
        if not m:
            continue
        prefix, ad_id = m.group(1), m.group(2)
        text = a.get_text(strip=True)
        if not text or text.lower() == "thumbnail":
            ad_map.setdefault(ad_id, {
                "ad_id": ad_id, "prefix": prefix,
                "url": f"https://tvcf.co.kr/play/{prefix}-{ad_id}",
                "card_title": None, "onair": None, "registered_short": None,
            })
            continue
        parsed = parse_card_text(text)
        ad_map[ad_id] = {
            "ad_id": ad_id, "prefix": prefix,
            "url": f"https://tvcf.co.kr/play/{prefix}-{ad_id}",
            "card_title": parsed["raw_title"],
            "onair": parsed["onair"],
            "registered_short": parsed["registered_short"],
        }
    ads = [v for v in ad_map.values() if v.get("registered_short") is not None]
    print(f"     → 유효한 광고 카드: {len(ads)}개")
    return ads


def parse_card_text(text):
    m = re.search(r"\((\d{2}\.\d{2})\)$", text)
    registered_short = m.group(1) if m else None
    rest = text[:m.start()] if m else text
    m2 = re.search(r"(\d{4}\.\d{2}\.\d{2}|\d{2}\.\d{2})$", rest)
    onair = m2.group(1) if m2 else None
    raw_title = (rest[:m2.start()] if m2 else rest).strip()
    return {"raw_title": raw_title, "onair": onair, "registered_short": registered_short}


def fetch_detail_html(ad):
    try:
        html = fetch_html(ad["url"])
    except Exception as e:
        print(f"  ⚠️  {ad['ad_id']} 상세 페이지 실패: {e}")
        return None
    soup = BeautifulSoup(html, "html.parser")
    brand = None
    og_title_tag = soup.find("meta", attrs={"property": "og:title"})
    if og_title_tag and og_title_tag.get("content"):
        brand = og_title_tag["content"].split("|")[0].strip()
    registered_date = None
    date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", html)
    if date_match:
        registered_date = date_match.group(1)
    campaign = None
    if brand:
        esc = re.escape(brand)
        m = re.search(esc + r"\s*:\s*(.+?)\s*편(?:\s|<|$)", html)
        if m:
            campaign = m.group(1).strip()
    main_category = None
    sub_categories = []
    media_type = None
    pumone_link = soup.find("a", href=re.compile(r"pumone_code_value"))
    if pumone_link:
        main_category = pumone_link.get_text(strip=True)
    media_link = soup.find("a", href=re.compile(r"media_code_value=\d+(?:&|$)"))
    if media_link:
        media_type = media_link.get_text(strip=True)
    if main_category:
        pattern = re.escape(main_category) + r"\][^>]*>([^<\n]+?)(?:\n|좋아요|<)"
        m = re.search(pattern, html)
        if m:
            sub_text = m.group(1).strip()
            sub_categories = [s.strip() for s in sub_text.split(">") if s.strip()]
    return {
        "brand": brand, "campaign": campaign,
        "registered_date": registered_date, "media_type": media_type,
        "main_category": main_category, "sub_categories": sub_categories,
    }


def fetch_advertiser_from_api(ad_id):
    url = f"{STAFFS_API_TEMPLATE.format(idx=ad_id)}?content_type=AD"
    payload = fetch_json_api(url)
    if not payload or payload.get("status") != 200:
        return None, None
    data = payload.get("data") or {}
    results = data.get("results") or []
    if not results:
        return None, None
    staffs_obj = results[0].get("staffs") or {}
    advertiser_name = None
    advertiser_code = None
    for group_key, group_val in staffs_obj.items():
        if not isinstance(group_val, dict):
            continue
        adv_role = group_val.get("광고주")
        if not adv_role:
            continue
        staff_list = adv_role.get("staff_list") or []
        if not staff_list:
            continue
        first = staff_list[0]
        advertiser_name = first.get("user_name") or first.get("name")
        advertiser_code = first.get("user_id") or first.get("staff_idx")
        break
    return advertiser_name, advertiser_code


def fetch_detail(ad):
    detail = fetch_detail_html(ad)
    if not detail:
        return None
    advertiser, advertiser_code = fetch_advertiser_from_api(ad["ad_id"])
    detail["advertiser"] = advertiser
    detail["advertiser_code"] = advertiser_code
    detail["advertiser_fallback"] = bool(not advertiser and detail.get("brand"))
    return detail


def dedup_by_advertiser_and_date(ads):
    groups = {}
    for ad in ads:
        adv = ad.get("advertiser") or ad.get("brand") or ad.get("ad_id")
        date = ad.get("registered_date") or ad.get("registered_short") or "unknown"
        key = (adv, date)
        if key not in groups:
            groups[key] = []
        groups[key].append(ad)
    deduped = []
    for key, items in groups.items():
        rep = dict(items[0])
        if len(items) > 1:
            rep["variants"] = [
                {"ad_id": v["ad_id"], "url": v.get("url"),
                 "brand": v.get("brand"), "campaign": v.get("campaign"),
                 "card_title": v.get("card_title")}
                for v in items[1:]
            ]
            rep["variant_count"] = len(items)
        else:
            rep["variant_count"] = 1
        deduped.append(rep)
    return deduped


def main():
    print(f"🚀 TVCF 크롤링 시작 (Playwright): {now_kst().isoformat()}")
    print(f"📍 Python {sys.version.split()[0]}")
    state = load_json(STATE_FILE, {"last_run": None, "known_ad_ids": []})
    history = load_json(HISTORY_FILE, [])
    known_ids = set(state.get("known_ad_ids", []))
    is_first_run = state.get("last_run") is None
    today_kst = now_kst().date()
    print(f"📅 오늘 (KST): {today_kst}")
    print(f"📋 이전 실행: {state.get('last_run') or '(첫 실행)'}")
    print(f"📦 누적 광고 ID: {len(known_ids)}개")

    print("\n📥 목록 페이지 크롤링 중...")
    init_session()
    try:
        ads = fetch_list()
        print(f"   ✓ 유효 광고: {len(ads)}개")
        if ERROR_STATUS_FILE.exists():
            try:
                ERROR_STATUS_FILE.unlink()
                print(f"   ✓ 이전 에러 상태 클리어")
            except Exception:
                pass
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        print(f"❌ 목록 페이지 실패: {error_msg}")
        save_json(ERROR_STATUS_FILE, {
            "error_at": now_kst().isoformat(),
            "error_type": type(e).__name__,
            "error_message": str(e)[:300],
            "source": "fetch_list",
            "message": "TVCF 사이트에 일시적인 문제가 발생했습니다. 자동으로 재시도되며 곧 복구됩니다.",
        })
        close_browser()
        return 1

    if is_first_run:
        yesterday = today_kst - timedelta(days=1)
        valid_dates = {today_kst.strftime("%m.%d"), yesterday.strftime("%m.%d")}
        print(f"\n   첫 실행: 어제({yesterday}) + 오늘({today_kst}) 등록분")
        new_ads = [a for a in ads if a["registered_short"] in valid_dates]
    else:
        new_ads = [a for a in ads if a["ad_id"] not in known_ids]

    print(f"   → 신규 광고: {len(new_ads)}개")

    if not new_ads:
        print("\n✨ 새 광고 없음.")
        state["last_run"] = now_kst().isoformat()
        state["known_ad_ids"] = list(known_ids | {a["ad_id"] for a in ads})[-500:]
        save_json(STATE_FILE, state)
        save_json(LATEST_FILE, {
            "run_at": now_kst().isoformat(),
            "new_ads": [], "count": 0, "count_total_before_dedup": 0,
        })
        print(f"\n🔍 마스터 광고주 자동 매칭 (과거 데이터 포함)...")
        try:
            master_list = load_master_advertisers()
            if master_list and history:
                seen_ids = set()
                unique_ads = []
                for ad in history:
                    aid = ad.get('ad_id')
                    if aid and aid not in seen_ids:
                        seen_ids.add(aid)
                        unique_ads.append(ad)
                print(f"   📊 매칭 대상: 누적 {len(history)} → 고유 {len(unique_ads)}건")
                added = auto_match_decisions(unique_ads, master_list)
                if added > 0:
                    print(f"   ✓ {added}건 자동 결정 추가됨")
                else:
                    print(f"   ℹ️ 새로 결정된 광고 없음")
        except Exception as e:
            import traceback
            print(f"   ⚠️ 자동 매칭 중 오류: {type(e).__name__}: {e}")
            traceback.print_exc()
        close_browser()
        return 0

    print(f"\n📋 {len(new_ads)}개 광고 상세 정보 수집 중...")
    enriched = []
    for i, ad in enumerate(new_ads, 1):
        print(f"   [{i}/{len(new_ads)}] {ad['ad_id']}", end=" ... ")
        detail = fetch_detail(ad)
        if detail:
            ad.update(detail)
            adv_label = detail.get("advertiser") or f"(브랜드:{detail.get('brand')})"
            cat_label = detail.get("main_category") or "?"
            print(f"✓ {adv_label} | {cat_label}")
        else:
            print("✗")
        enriched.append(ad)
        time.sleep(DELAY_SEC)

    print(f"\n🔄 광고주+날짜 기준 중복 제거 중...")
    deduped = dedup_by_advertiser_and_date(enriched)
    print(f"   {len(enriched)}건 → {len(deduped)}건")

    latest = {
        "run_at": now_kst().isoformat(),
        "count": len(deduped),
        "count_total_before_dedup": len(enriched),
        "new_ads": deduped,
    }
    save_json(LATEST_FILE, latest)
    print(f"\n💾 latest.json 저장")

    existing_ids = {h["ad_id"] for h in history}
    for ad in deduped:
        if ad["ad_id"] not in existing_ids:
            history.append({**ad, "collected_at": now_kst().isoformat()})
            existing_ids.add(ad["ad_id"])
    history.sort(key=lambda x: (x.get("registered_date") or "", x.get("ad_id", "")), reverse=True)
    save_json(HISTORY_FILE, history)
    print(f"💾 history.json: 총 {len(history)}건")

    state["last_run"] = now_kst().isoformat()
    state["known_ad_ids"] = list(known_ids | {a["ad_id"] for a in ads})[-500:]
    save_json(STATE_FILE, state)
    print(f"💾 state.json 저장")

    print(f"\n🔍 마스터 광고주 자동 매칭 중...")
    try:
        master_list = load_master_advertisers()
        if master_list:
            all_ads_to_match = deduped + history
            seen_ids = set()
            unique_ads = []
            for ad in all_ads_to_match:
                aid = ad.get('ad_id')
                if aid and aid not in seen_ids:
                    seen_ids.add(aid)
                    unique_ads.append(ad)
            print(f"   📊 매칭 대상: 신규 {len(deduped)} + 누적 {len(history)} = 고유 {len(unique_ads)}건")
            added = auto_match_decisions(unique_ads, master_list)
            if added > 0:
                print(f"   ✓ {added}건 자동 결정 추가됨")
            else:
                print(f"   ℹ️ 새로 결정된 광고 없음")
    except Exception as e:
        import traceback
        print(f"   ⚠️ 자동 매칭 중 오류: {type(e).__name__}: {e}")
        traceback.print_exc()

    print(f"\n✅ 완료! 대표 {len(deduped)}건 (전체 {len(enriched)}건)")
    close_browser()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        close_browser()
