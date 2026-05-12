"""
TVCF 신규 광고 크롤링 스크립트
- 매일 한 번 실행
- 직전 실행 이후 등록된 광고만 수집
- 광고주, 캠페인명, 카테고리, 등록일 추출
- 결과를 data/latest.json, data/history.json에 저장
"""
import json
import re
import time
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
    "sort_by": "registrated_date",  # 등록순 정렬
    "country_code_value": 410,       # 대한민국
    "category_code_value": "0,1",    # 일반작
    "lang": "ko",
}

# 일반 브라우저처럼 보이는 헤더
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://tvcf.co.kr/",
}

# 한국 시간대
KST = timezone(timedelta(hours=9))

# 경로
ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)
LATEST_FILE = DATA_DIR / "latest.json"
HISTORY_FILE = DATA_DIR / "history.json"
STATE_FILE = DATA_DIR / "state.json"

# 요청 간 딜레이 (서버 부담 방지)
DELAY_SEC = 0.5


# ── 유틸 ──────────────────────────────────────────────────────
def now_kst():
    return datetime.now(KST)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"⚠️  {path} 읽기 실패: {e}, 기본값 사용")
        return default


def save_json(path: Path, data):
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ── 크롤링 ────────────────────────────────────────────────────
def fetch(url, **kwargs):
    """공통 fetch 함수"""
    r = requests.get(url, headers=HEADERS, timeout=20, **kwargs)
    r.raise_for_status()
    return r.text


def fetch_list():
    """광고 목록 페이지에서 광고 ID, 등록일 추출"""
    html = fetch(LIST_URL, params=LIST_PARAMS)
    soup = BeautifulSoup(html, "html.parser")

    ads = []
    seen_ids = set()

    for a in soup.find_all("a", href=re.compile(r"^/play/")):
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
    """카드 텍스트에서 날짜 추출
    예: "ILLIT (아일릿)'똑똑..엄마야?' 편 6s | It's Me | ILLIT (아일릿)05.12(05.12)"
    """
    # 끝의 (MM.DD) = 등록일
    m = re.search(r"\((\d{2}\.\d{2})\)$", text)
    registered_short = m.group(1) if m else None
    rest = text[:m.start()] if m else text

    # 그 앞: 온에어일 (YYYY.MM.DD 또는 MM.DD)
    m2 = re.search(r"(\d{4}\.\d{2}\.\d{2}|\d{2}\.\d{2})$", rest)
    onair = m2.group(1) if m2 else None
    raw_title = (rest[:m2.start()] if m2 else rest).strip()

    return {
        "raw_title": raw_title,
        "onair": onair,
        "registered_short": registered_short,
    }


def fetch_detail(ad):
    """상세 페이지에서 광고주, 캠페인, 카테고리, 등록일 추출"""
    try:
        html = fetch(ad["url"])
    except Exception as e:
        print(f"  ⚠️  {ad['ad_id']} 상세 페이지 가져오기 실패: {e}")
        return None

    # og:title에서 광고주명
    soup = BeautifulSoup(html, "html.parser")
    og_title_tag = soup.find("meta", attrs={"property": "og:title"})
    advertiser = None
    if og_title_tag and og_title_tag.get("content"):
        og_title = og_title_tag["content"]
        # "롯데손해보험 | TVCF" 형태에서 광고주만 추출
        advertiser = og_title.split("|")[0].strip()

    # 등록일 (YYYY-MM-DD)
    registered_date = None
    date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", html)
    if date_match:
        registered_date = date_match.group(1)

    # 캠페인 제목
    campaign = None
    if advertiser:
        # 본문에서 "광고주명: 캠페인 편" 패턴 찾기
        esc_adv = re.escape(advertiser)
        m = re.search(esc_adv + r"\s*:\s*(.+?)\s*편(?:\s|<|$)", html)
        if m:
            campaign = m.group(1).strip()

    # 카테고리 경로: 대한민국 > 디지털(인터넷) > 출판/교육/문화 > 서적/음반/영화 > 음반
    main_category = None
    sub_categories = []
    media_type = None

    # 카테고리 링크들: pumone_code_value가 있는 a 태그가 메인 카테고리
    pumone_link = soup.find("a", href=re.compile(r"pumone_code_value"))
    if pumone_link:
        main_category = pumone_link.get_text(strip=True)

    # 미디어 타입 (디지털, 공중파 등): media_code_value만 있는 a 태그
    media_link = soup.find("a", href=re.compile(r"media_code_value=\d+(?:&|$)"))
    if media_link:
        media_type = media_link.get_text(strip=True)

    # 서브 카테고리는 텍스트에서 ">" 패턴으로 추출
    # 메인 카테고리 이후의 ">서적/음반/영화>음반" 같은 부분
    if main_category:
        # 본문에서 "[메인카테고리](...링크...)>서브1>서브2" 패턴
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


# ── 메인 ──────────────────────────────────────────────────────
def main():
    print(f"🚀 TVCF 크롤링 시작: {now_kst().isoformat()}")

    # 1) 상태 로드
    state = load_json(STATE_FILE, {
        "last_run": None,
        "known_ad_ids": [],
    })
    history = load_json(HISTORY_FILE, [])
    known_ids = set(state.get("known_ad_ids", []))

    is_first_run = state.get("last_run") is None
    today_kst = now_kst().date()

    print(f"📅 오늘 (KST): {today_kst}")
    print(f"📋 이전 실행: {state.get('last_run') or '(첫 실행)'}")
    print(f"📦 누적된 광고 ID 개수: {len(known_ids)}")

    # 2) 목록 페이지 크롤링
    print("\n📥 목록 페이지 크롤링 중...")
    try:
        ads = fetch_list()
        print(f"   목록에서 {len(ads)}개 광고 발견")
    except Exception as e:
        print(f"❌ 목록 페이지 가져오기 실패: {e}")
        return 1

    # 3) 첫 실행이면: 어제~오늘 등록분만
    # 이후 실행이면: known_ids에 없는 새로운 광고만
    if is_first_run:
        yesterday = today_kst - timedelta(days=1)
        # MM.DD 형태로 비교
        valid_dates = {
            today_kst.strftime("%m.%d"),
            yesterday.strftime("%m.%d"),
        }
        new_ads = [a for a in ads if a["registered_short"] in valid_dates]
        print(f"   첫 실행: 어제({yesterday}) + 오늘({today_kst}) 등록분만 → {len(new_ads)}개")
    else:
        new_ads = [a for a in ads if a["ad_id"] not in known_ids]
        print(f"   이전 실행 이후 새 광고: {len(new_ads)}개")

    if not new_ads:
        print("\n✨ 새 광고 없음. 종료.")
        state["last_run"] = now_kst().isoformat()
        save_json(STATE_FILE, state)
        save_json(LATEST_FILE, {
            "run_at": now_kst().isoformat(),
            "new_ads": [],
            "count": 0,
        })
        return 0

    # 4) 각 광고 상세 페이지에서 세부 정보 추출
    print(f"\n📋 {len(new_ads)}개 광고 상세 정보 수집 중...")
    enriched = []
    for i, ad in enumerate(new_ads, 1):
        print(f"   [{i}/{len(new_ads)}] {ad['ad_id']} ...", end=" ")
        detail = fetch_detail(ad)
        if detail:
            ad.update(detail)
            print(f"✓ {detail['advertiser']}")
        else:
            print("✗ 실패")
        enriched.append(ad)
        time.sleep(DELAY_SEC)

    # 5) 최신 결과 저장
    latest = {
        "run_at": now_kst().isoformat(),
        "count": len(enriched),
        "new_ads": enriched,
    }
    save_json(LATEST_FILE, latest)
    print(f"\n💾 data/latest.json 저장 완료")

    # 6) 누적 히스토리 업데이트
    existing_ids = {h["ad_id"] for h in history}
    for ad in enriched:
        if ad["ad_id"] not in existing_ids:
            history.append({**ad, "collected_at": now_kst().isoformat()})
            existing_ids.add(ad["ad_id"])

    # 등록일 기준 내림차순 정렬
    history.sort(key=lambda x: (x.get("registered_date") or "", x.get("ad_id", "")), reverse=True)
    save_json(HISTORY_FILE, history)
    print(f"💾 data/history.json 저장 완료 (총 {len(history)}건)")

    # 7) 상태 업데이트
    state["last_run"] = now_kst().isoformat()
    # 목록 페이지에 나온 모든 광고 ID를 known에 추가 (재방문 방지)
    state["known_ad_ids"] = list(known_ids | {a["ad_id"] for a in ads})
    # known_ids는 최대 500개까지만 유지 (오래된 건 자동 제거)
    state["known_ad_ids"] = state["known_ad_ids"][-500:]
    save_json(STATE_FILE, state)
    print(f"💾 data/state.json 저장 완료")

    print(f"\n✅ 완료! 새 광고 {len(enriched)}건 수집됨.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
