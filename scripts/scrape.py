"""
TVCF 신규 광고 크롤링 스크립트 v4
- v3에 비해 추가/수정:
  1. staffs API 호출하여 실제 광고주명 (예: 한국MSD) 추출
  2. og:title은 "brand" 필드로 분리 (예: 가다실)
  3. 광고주+날짜 기준 중복 제거 (대표 1건만 남김, 나머지는 variants에 포함)
"""
import json
import os
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
DECISIONS_FILE = DATA_DIR / "decisions.json"
ERROR_STATUS_FILE = DATA_DIR / "error_status.json"

DELAY_SEC = 0.5


def normalize_name(name):
    """매칭용 광고주명 정규화 (index.html과 동일 로직)"""
    if not name:
        return ''
    import re as _re
    s = str(name).lower()
    s = _re.sub(r'\s+', '', s)
    s = _re.sub(r'[()()\[\]【】「」\'\',·.\-_/]', '', s)
    s = _re.sub(r'주식회사|㈜|\(주\)|inc\.?|corp\.?|ltd\.?|co\.?', '', s)
    return s.strip()


def load_master_advertisers():
    """환경변수 MASTER_ADVERTISERS에서 광고주 목록 로드
    
    형식: JSON 문자열
    {
      "advertisers": ["광고주1", "광고주2", ...],
      "count": N
    }
    
    환경변수 없으면 빈 리스트 반환 (매칭 안 함)
    """
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
    """광고주명을 마스터 리스트와 매칭
    
    Returns:
        ('o', matched_name) - 정확 매칭 (집행)
        ('maybe', matched_name) - 부분 매칭 (확인 필요)
        ('x', None) - 매칭 없음 (미집행)
    """
    if not name or not master_list:
        return None, None
    
    normalized = normalize_name(name)
    if not normalized:
        return None, None
    
    # 1. 정확 매칭
    for m in master_list:
        if normalize_name(m) == normalized:
            return 'o', m
    
    # 2. 부분 매칭 (포함 관계)
    for m in master_list:
        nm = normalize_name(m)
        if len(nm) > 1 and len(normalized) > 1:
            if nm in normalized or normalized in nm:
                return 'maybe', m
    
    # 3. 매칭 없음
    return 'x', None


def auto_match_decisions(new_ads, master_list):
    """크롤링된 새 광고를 마스터 리스트와 매칭해서 decisions.json 자동 업데이트
    - O/maybe: decisions[광고주명]에 영구 저장
    - X: x_decisions[ad_id]에 1회용 저장
    - 이미 결정된 항목은 건드리지 않음
    """
    if not master_list:
        return 0
    
    # 기존 decisions.json 로드
    decisions_data = load_json(DECISIONS_FILE, {
        "updated_at": "",
        "count": 0,
        "x_count": 0,
        "decisions": {},
        "x_decisions": {},
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
        
        # 이미 결정된 항목은 건드리지 않음
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
                    "advertiser": name,
                    "status": "x",
                    "updated_at": now_iso,
                    "auto": True,
                }
                added_count += 1
        else:
            # o or maybe → 영구 저장
            decisions[key] = {
                "display_name": name,
                "status": status,
                "matched_name": matched,
                "updated_at": now_iso,
                "auto": True,
            }
            added_count += 1
    
    if added_count == 0:
        return 0
    
    # 저장
    payload = {
        "updated_at": now_kst().isoformat(),
        "count": len(decisions),
        "x_count": len(x_decisions),
        "decisions": decisions,
        "x_decisions": x_decisions,
    }
    save_json(DECISIONS_FILE, payload)
    print(f"💾 decisions.json 업데이트: +{added_count}건 (영구 {len(decisions)} + 1회용 {len(x_decisions)})")
    return added_count



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
    """HTTP GET 요청 with 자동 재시도
    - 500, 502, 503, 504 같은 서버 에러 발생 시 자동 재시도
    - 1차 즉시 → 실패 시 60초 후 → 또 실패 시 10분 후
    - 모두 실패하면 마지막 에러 raise
    """
    headers = kwargs.pop("headers", HEADERS)
    max_retries = 3
    retry_delays = [60, 600]  # 1차 실패 후 60초, 2차 실패 후 10분(600초) 대기

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=20, **kwargs)
            # 5xx 서버 에러는 재시도
            if 500 <= r.status_code < 600 and attempt < max_retries:
                wait = retry_delays[attempt - 1]
                wait_label = f"{wait}초" if wait < 60 else f"{wait//60}분"
                print(f"     ⚠ 서버 에러 {r.status_code} (시도 {attempt}/{max_retries}). {wait_label} 대기 후 재시도...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            # 네트워크 에러도 재시도
            last_error = e
            if attempt < max_retries:
                wait = retry_delays[attempt - 1]
                wait_label = f"{wait}초" if wait < 60 else f"{wait//60}분"
                print(f"     ⚠ 네트워크 에러 (시도 {attempt}/{max_retries}): {type(e).__name__}. {wait_label} 대기 후 재시도...")
                time.sleep(wait)
            else:
                raise
        except requests.exceptions.HTTPError as e:
            # 4xx 클라이언트 에러는 즉시 실패 (재시도 무의미)
            if 400 <= e.response.status_code < 500:
                raise
            last_error = e
            if attempt < max_retries:
                wait = retry_delays[attempt - 1]
                wait_label = f"{wait}초" if wait < 60 else f"{wait//60}분"
                print(f"     ⚠ HTTP 에러 {e.response.status_code} (시도 {attempt}/{max_retries}). {wait_label} 대기 후 재시도...")
                time.sleep(wait)
            else:
                raise

    if last_error:
        raise last_error
    raise RuntimeError("fetch failed after retries")


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
        # 성공: error_status.json 파일이 있으면 삭제 (정상 복구 표시)
        if ERROR_STATUS_FILE.exists():
            try:
                ERROR_STATUS_FILE.unlink()
                print(f"   ✓ 이전 에러 상태 클리어")
            except Exception:
                pass
    except Exception as e:
        error_msg = f"{type(e).__name__}: {e}"
        print(f"❌ 목록 페이지 실패: {error_msg}")
        # 에러 상태 기록 (페이지에서 배너로 표시할 수 있도록)
        save_json(ERROR_STATUS_FILE, {
            "error_at": now_kst().isoformat(),
            "error_type": type(e).__name__,
            "error_message": str(e)[:300],  # 너무 길지 않게 제한
            "source": "fetch_list",
            "message": "TVCF 사이트에 일시적인 문제가 발생했습니다. 자동으로 재시도되며 곧 복구됩니다.",
        })
        return 1

    if is_first_run:
        yesterday = today_kst - timedelta(days=1)
        valid_dates = {
            today_kst.strftime("%m.%d"),
            yesterday.strftime("%m.%d"),
        }
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
            "new_ads": [],
            "count": 0,
            "count_total_before_dedup": 0,
        })

        # 신규 광고가 없어도 과거 history.json에 대해 자동 매칭은 수행
        # (마스터 목록 업데이트 후 미매칭 항목을 일괄 처리하기 위해)
        print(f"\n🔍 마스터 광고주 목록과 자동 매칭 중 (과거 데이터 포함)...")
        try:
            master_list = load_master_advertisers()
            if master_list and history:
                # history 전체 매칭
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
                    print(f"   ℹ️ 새로 결정된 광고 없음 (모두 기존에 결정됨)")
            elif not master_list:
                print(f"   ⏭️ 마스터 목록 없어서 매칭 건너뜀")
            else:
                print(f"   ⏭️ history 데이터 없음")
        except Exception as e:
            import traceback
            print(f"   ⚠️ 자동 매칭 중 오류: {type(e).__name__}: {e}")
            traceback.print_exc()

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

    # ===== 신규 광고 자동 매칭 (decisions.json 업데이트) =====
    print(f"\n🔍 마스터 광고주 목록과 자동 매칭 중...")
    try:
        master_list = load_master_advertisers()
        if master_list:
            # 신규 광고 + history.json 전체 데이터 모두 매칭
            # (과거 데이터까지 한 번에 정리)
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
                print(f"   ℹ️ 새로 결정된 광고 없음 (모두 기존에 결정됨)")
        else:
            print(f"   ⏭️ 마스터 목록 없어서 매칭 건너뜀")
    except Exception as e:
        # 자동 매칭 실패해도 크롤링 결과는 살리고 경고만 출력
        import traceback
        print(f"   ⚠️ 자동 매칭 중 오류 (크롤링 결과는 정상 저장됨): {type(e).__name__}: {e}")
        traceback.print_exc()

    print(f"\n✅ 완료! 대표 {len(deduped)}건 (전체 {len(enriched)}건)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
