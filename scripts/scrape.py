"""
TVCF 신규 광고 크롤링 스크립트 v5
- v4에 비해 추가/수정:
  1. ★ 온에어일 기준 필터링 추가 (최근 3일 이내만 신규로 인정)
     이전엔 TVCF에 늦게 등록된 과거 광고도 신규로 잡혔지만,
     이제는 온에어일이 최근이어야 신규로 잡힘
  2. registered_short(등록일) + registered_date(온에어일) 둘 다 체크
"""
import json
import re
import time
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests
from bs4 import BeautifulSoup

# ── 설정 ──────────────────────────────────────────────────────
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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

API_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "*/*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://tvcf.co.kr/",
}

KST = timezone(timedelta(hours=9))

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
LATEST_FILE = DATA_DIR / "latest.json"
HISTORY_FILE = DATA_DIR / "history.json"
STATE_FILE = DATA_DIR / "state.json"

DELAY_SEC = 0.5


def now_kst():
    return datetime.now(KST)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  {path} 읽기 실패: {e}")
        return default


def save_json(path: Path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def fetch(url, **kwargs):
    headers = kwargs.pop("headers", HEADERS)
    r = requests.get(url, headers=headers, timeout=20, **kwargs)
    r.raise_for_status()
    return r


def fetch_list():
    """광고 목록 페이지에서 광고 ID 추출"""
    print(f"   → GET {LIST_URL}")
    r = fetch(LIST_URL, params=LIST_PARAMS)
    print(f"     status: {r.status_code}, length: {len(r.text)} bytes")
    soup = BeautifulSoup(r.text, "html.parser")
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
                "ad_id": ad_id,
                "prefix": prefix,
                "url": f"https://tvcf.co.kr/play/{prefix}-{ad_id}",
                "card_title": None,
                "onair": None,
                "registered_short": None,
            })
            continue
        parsed = parse_card_text(text)
        ad_map[ad_id] = {
            "ad_id": ad_id,
            "prefix": prefix,
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
    return {
        "raw_title": raw_title,
        "onair": onair,
        "registered_short": registered_short,
    }


def fetch_detail_html(ad):
    """상세 페이지 HTML에서 브랜드, 카테고리, 등록일 추출"""
    try:
        r = fetch(ad["url"])
        html = r.text
    except Exception as e:
        print(f"  ⚠️  {ad['ad_id']} 상세 페이지 실패: {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")

    # og:title → 브랜드명 (예: "가다실", "ILLIT (아일릿)")
    brand = None
    og_title_tag = soup.find("meta", attrs={"property": "og:title"})
    if og_title_tag and og_title_tag.get("content"):
        brand = og_title_tag["content"].split("|")[0].strip()

    # 등록일
    registered_date = None
    date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", html)
    if date_match:
        registered_date = date_match.group(1)

    # 캠페인 제목
    campaign = None
    if brand:
        esc = re.escape(brand)
        m = re.search(esc + r"\s*:\s*(.+?)\s*편(?:\s|<|$)", html)
        if m:
            campaign = m.group(1).strip()

    # 카테고리
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
        "brand": brand,
        "campaign": campaign,
        "registered_date": registered_date,
        "media_type": media_type,
        "main_category": main_category,
        "sub_categories": sub_categories,
    }


def fetch_advertiser_from_api(ad_id):
    """staffs API 호출하여 진짜 광고주명 추출
    URL: https://tvcf.co.kr/api/main/v1/play/{idx}/staffs?content_type=AD
    응답 구조: data.results[0].staffs.<key>.광고주.staff_list[0].user_name
    """
    url = STAFFS_API_TEMPLATE.format(idx=ad_id)
    try:
        r = requests.get(
            url,
            headers=API_HEADERS,
            params={"content_type": "AD"},
            timeout=15,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception as e:
        print(f"    ⚠️  staffs API 실패 ({ad_id}): {e}")
        return None, None

    if payload.get("status") != 200:
        return None, None

    data = payload.get("data") or {}
    results = data.get("results") or []
    if not results:
        return None, None

    staffs_obj = results[0].get("staffs") or {}
    # staffs 안에 1, 2, 3... 같은 숫자 키로 그룹화되어 있음
    # 각 그룹 안에 "광고주", "BGM", "모델", "촬영지" 같은 한글 role이 있음
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
    """상세 페이지 + staffs API → 통합 정보"""
    detail = fetch_detail_html(ad)
    if not detail:
        return None

    # staffs API에서 진짜 광고주 가져오기
    advertiser, advertiser_code = fetch_advertiser_from_api(ad["ad_id"])

    detail["advertiser"] = advertiser
    detail["advertiser_code"] = advertiser_code

    # 광고주가 없으면 fallback: 브랜드를 광고주로 (legacy 호환)
    if not advertiser and detail.get("brand"):
        detail["advertiser_fallback"] = True
    else:
        detail["advertiser_fallback"] = False

    return detail


def dedup_by_advertiser_and_date(ads):
    """광고주+날짜 기준으로 중복 제거.
    같은 광고주의 같은 날짜 광고는 하나의 대표로 합치고,
    나머지 광고 정보는 variants에 저장.
    """
    groups = {}
    for ad in ads:
        # 광고주 키 (없으면 브랜드로 fallback, 그것도 없으면 ad_id 단독)
        adv = ad.get("advertiser") or ad.get("brand") or ad.get("ad_id")
        date = ad.get("registered_date") or ad.get("registered_short") or "unknown"
        key = (adv, date)
        if key not in groups:
            groups[key] = []
        groups[key].append(ad)

    deduped = []
    for key, items in groups.items():
        # 첫 번째 광고를 대표로
        rep = dict(items[0])
        # 나머지를 variants에
        if len(items) > 1:
            rep["variants"] = [
                {
                    "ad_id": v["ad_id"],
                    "url": v.get("url"),
                    "brand": v.get("brand"),
                    "campaign": v.get("campaign"),
                    "card_title": v.get("card_title"),
                }
                for v in items[1:]
            ]
            rep["variant_count"] = len(items)
        else:
            rep["variant_count"] = 1
        deduped.append(rep)

    return deduped


def main():
    print(f"🚀 TVCF 크롤링 시작: {now_kst().isoformat()}")
    print(f"📍 Python {sys.version.split()[0]}, requests {requests.__version__}")

    state = load_json(STATE_FILE, {"last_run": None, "known_ad_ids": []})
    history = load_json(HISTORY_FILE, [])
    known_ids = set(state.get("known_ad_ids", []))

    is_first_run = state.get("last_run") is None
    today_kst = now_kst().date()

    print(f"📅 오늘 (KST): {today_kst}")
    print(f"📋 이전 실행: {state.get('last_run') or '(첫 실행)'}")
    print(f"📦 누적 광고 ID: {len(known_ids)}개")

    print("\n📥 목록 페이지 크롤링 중...")
    try:
        ads = fetch_list()
        print(f"   ✓ 유효 광고: {len(ads)}개")
    except Exception as e:
        print(f"❌ 목록 페이지 실패: {type(e).__name__}: {e}")
        return 1

    # ── 온에어일 필터: 최근 3일 이내만 신규로 인정 ──
    # TVCF에 늦게 등록된 과거 광고는 제외
    ONAIR_DAYS_LIMIT = 3
    onair_cutoff = today_kst - timedelta(days=ONAIR_DAYS_LIMIT)
    onair_cutoff_str = onair_cutoff.strftime("%Y-%m-%d")
    print(f"\n   온에어일 필터: {onair_cutoff_str} 이후 광고만 (최근 {ONAIR_DAYS_LIMIT + 1}일)")

    def is_recent_onair(ad):
        """광고의 온에어일(registered_date)이 최근 N일 이내인지 확인"""
        reg = ad.get("registered_date", "")
        if not reg or len(reg) < 10:
            return False
        return reg[:10] >= onair_cutoff_str

    if is_first_run:
        yesterday = today_kst - timedelta(days=1)
        valid_dates = {
            today_kst.strftime("%m.%d"),
            yesterday.strftime("%m.%d"),
        }
        print(f"\n   첫 실행: 어제({yesterday}) + 오늘({today_kst}) 등록분")
        new_ads = [a for a in ads if a["registered_short"] in valid_dates and is_recent_onair(a)]
    else:
        # 1차: 처음 보는 ad_id (중복 체크)
        # 2차: 온에어일이 최근 N일 이내
        new_ads = [a for a in ads if a["ad_id"] not in known_ids and is_recent_onair(a)]

        # 필터링 통계 출력
        ids_not_in_history = [a for a in ads if a["ad_id"] not in known_ids]
        filtered_by_onair = len(ids_not_in_history) - len(new_ads)
        if filtered_by_onair > 0:
            print(f"   ⚠ 온에어일 필터로 {filtered_by_onair}건 제외 (TVCF 늦게 등록된 과거 광고)")

    print(f"   → 신규 광고: {len(new_ads)}개")

    if not new_ads:
        print("\n✨ 새 광고 없음. 종료.")
        state["last_run"] = now_kst().isoformat()
        state["known_ad_ids"] = list(known_ids | {a["ad_id"] for a in ads})[-500:]
        save_json(STATE_FILE, state)
        save_json(LATEST_FILE, {
            "run_at": now_kst().isoformat(),
            "new_ads": [],
            "count": 0,
            "count_total_before_dedup": 0,
        })
        return 0

    print(f"\n📋 {len(new_ads)}개 광고 상세 정보 수집 중 (HTML + staffs API)...")
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

    # 광고주+날짜 기준 중복 제거
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

    # 히스토리 (중복 제거된 버전으로 저장)
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

    print(f"\n✅ 완료! 대표 {len(deduped)}건 (전체 {len(enriched)}건)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
