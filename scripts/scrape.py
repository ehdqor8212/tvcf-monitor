"""
TVCF 신규 광고 크롤링 스크립트 v2
- 디버그 로깅 강화
- 봇 차단 회피용 헤더 강화
- HTTP 상태 코드 및 응답 길이 확인
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
LIST_PARAMS = {
    "mediaType_value": 1,
    "page": 1,
    "rows": 50,
    "sort_by": "registrated_date",
    "country_code_value": 410,
    "category_code_value": "0,1",
    "lang": "ko",
}

# 더 완전한 브라우저 헤더 세트
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

KST = timezone(timedelta(hours=9))

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
LATEST_FILE = DATA_DIR / "latest.json"
HISTORY_FILE = DATA_DIR / "history.json"
STATE_FILE = DATA_DIR / "state.json"
DEBUG_FILE = DATA_DIR / "debug_last_response.html"  # 디버그용

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
    """공통 fetch 함수 + 디버그 로깅"""
    print(f"   → GET {url}")
    if kwargs.get("params"):
        print(f"     params: {kwargs['params']}")
    r = requests.get(url, headers=HEADERS, timeout=20, **kwargs)
    print(f"     status: {r.status_code}, length: {len(r.text)} bytes")
    r.raise_for_status()
    return r.text


def fetch_list():
    """광고 목록 페이지에서 광고 ID 추출"""
    html = fetch(LIST_URL, params=LIST_PARAMS)

    # 디버그: 처음 응답 일부 저장
    try:
        DEBUG_FILE.write_text(html[:50000], encoding="utf-8")
        print(f"     💾 응답 일부 저장: {DEBUG_FILE}")
    except Exception as e:
        print(f"     ⚠️  디버그 파일 저장 실패: {e}")

    # HTML 구조 확인용 디버그
    soup = BeautifulSoup(html, "html.parser")
    all_links = soup.find_all("a")
    play_links = soup.find_all("a", href=re.compile(r"/play/"))
    print(f"     전체 a 태그: {len(all_links)}개")
    print(f"     /play/ 링크: {len(play_links)}개")

    # 처음 몇 개 링크 샘플 출력
    if play_links[:3]:
        print(f"     /play/ 링크 샘플:")
        for link in play_links[:3]:
            print(f"       - href={link.get('href')}, text='{link.get_text(strip=True)[:50]}'")
    else:
        # /play/ 링크가 없으면 다른 패턴 찾기
        href_samples = [a.get("href") for a in all_links[:20] if a.get("href")]
        print(f"     ⚠️  /play/ 링크 없음. 다른 href 샘플 20개:")
        for h in href_samples:
            print(f"       - {h}")

    ads = []
    seen_ids = set()

    for a in play_links:
        href = a.get("href", "")
        m = re.match(r"^/play/([a-z]+\d+)-(\d+)$", href)
        if not m:
            continue
        prefix, ad_id = m.group(1), m.group(2)
        if ad_id in seen_ids:
            continue
        seen_ids.add(ad_id)

        text = a.get_text(strip=True)
        if not text or text == "thumbnail":
            continue

        parsed = parse_card_text(text)
        ads.append({
            "ad_id": ad_id,
            "prefix": prefix,
            "url": f"https://tvcf.co.kr/play/{prefix}-{ad_id}",
            "card_title": parsed["raw_title"],
            "onair": parsed["onair"],
            "registered_short": parsed["registered_short"],
        })
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


def fetch_detail(ad):
    try:
        html = fetch(ad["url"])
    except Exception as e:
        print(f"  ⚠️  {ad['ad_id']} 상세 페이지 실패: {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")
    og_title_tag = soup.find("meta", attrs={"property": "og:title"})
    advertiser = None
    if og_title_tag and og_title_tag.get("content"):
        og_title = og_title_tag["content"]
        advertiser = og_title.split("|")[0].strip()

    registered_date = None
    date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", html)
    if date_match:
        registered_date = date_match.group(1)

    campaign = None
    if advertiser:
        esc_adv = re.escape(advertiser)
        m = re.search(esc_adv + r"\s*:\s*(.+?)\s*편(?:\s|<|$)", html)
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
        "advertiser": advertiser,
        "campaign": campaign,
        "registered_date": registered_date,
        "media_type": media_type,
        "main_category": main_category,
        "sub_categories": sub_categories,
    }


def main():
    print(f"🚀 TVCF 크롤링 시작: {now_kst().isoformat()}")
    print(f"📍 Python 버전: {sys.version}")
    print(f"📍 requests 버전: {requests.__version__}")

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
        print(f"   ✓ 목록에서 {len(ads)}개 광고 발견")
    except requests.HTTPError as e:
        print(f"❌ HTTP 에러: {e}")
        print(f"   응답 본문: {e.response.text[:500] if e.response else 'N/A'}")
        return 1
    except Exception as e:
        print(f"❌ 목록 페이지 가져오기 실패: {type(e).__name__}: {e}")
        return 1

    if is_first_run:
        yesterday = today_kst - timedelta(days=1)
        valid_dates = {
            today_kst.strftime("%m.%d"),
            yesterday.strftime("%m.%d"),
        }
        print(f"\n   첫 실행: 어제({yesterday}) + 오늘({today_kst}) 등록분 필터링")
        print(f"   유효 날짜 패턴: {valid_dates}")
        # 어떤 광고가 어떤 날짜에 등록됐는지 보여주기
        date_counts = {}
        for a in ads:
            d = a["registered_short"]
            date_counts[d] = date_counts.get(d, 0) + 1
        print(f"   광고별 등록일 분포: {dict(sorted(date_counts.items(), key=lambda x: x[0] or '', reverse=True)[:10])}")
        new_ads = [a for a in ads if a["registered_short"] in valid_dates]
    else:
        new_ads = [a for a in ads if a["ad_id"] not in known_ids]

    print(f"   → 신규 광고: {len(new_ads)}개")

    if not new_ads:
        print("\n✨ 새 광고 없음. 종료.")
        state["last_run"] = now_kst().isoformat()
        # 빈 실행이라도 known_ad_ids는 업데이트 (다음 실행에서 비교용)
        state["known_ad_ids"] = list(known_ids | {a["ad_id"] for a in ads})[-500:]
        save_json(STATE_FILE, state)
        save_json(LATEST_FILE, {
            "run_at": now_kst().isoformat(),
            "new_ads": [],
            "count": 0,
        })
        return 0

    print(f"\n📋 {len(new_ads)}개 광고 상세 정보 수집 중...")
    enriched = []
    for i, ad in enumerate(new_ads, 1):
        print(f"   [{i}/{len(new_ads)}] {ad['ad_id']}", end=" ... ")
        detail = fetch_detail(ad)
        if detail:
            ad.update(detail)
            print(f"✓ {detail['advertiser']} | {detail['main_category']}")
        else:
            print("✗")
        enriched.append(ad)
        time.sleep(DELAY_SEC)

    latest = {
        "run_at": now_kst().isoformat(),
        "count": len(enriched),
        "new_ads": enriched,
    }
    save_json(LATEST_FILE, latest)
    print(f"\n💾 latest.json 저장")

    existing_ids = {h["ad_id"] for h in history}
    for ad in enriched:
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

    print(f"\n✅ 완료! 신규 {len(enriched)}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
