"""
TVCF 신규 광고 크롤링 스크립트 v3
- v2 버그 수정: 같은 광고 ID에 빈 텍스트 링크와 제목 링크가 둘 다 있을 때,
  빈 텍스트 링크가 먼저 dedup에 등록되어 진짜 카드가 누락되던 문제 수정
- 처리 순서: (1) 빈 텍스트 skip → (2) regex 매칭 → (3) dedup
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
    print(f"   → GET {url}")
    r = requests.get(url, headers=HEADERS, timeout=20, **kwargs)
    print(f"     status: {r.status_code}, length: {len(r.text)} bytes")
    r.raise_for_status()
    return r.text


def fetch_list():
    """광고 목록 페이지에서 광고 ID 추출
    같은 광고 ID에 빈 텍스트 링크와 제목 링크가 둘 다 있을 수 있음.
    제목 링크(텍스트 있는 것)를 우선적으로 사용.
    """
    html = fetch(LIST_URL, params=LIST_PARAMS)
    soup = BeautifulSoup(html, "html.parser")

    play_links = soup.find_all("a", href=re.compile(r"^/play/"))
    print(f"     /play/ 링크 총 {len(play_links)}개 발견")

    # ad_id → 정보 매핑 (텍스트 있는 링크가 텍스트 없는 링크를 덮어쓰도록)
    ad_map = {}

    for a in play_links:
        href = a.get("href", "")
        m = re.match(r"^/play/([a-z]+\d+)-(\d+)$", href)
        if not m:
            continue
        prefix, ad_id = m.group(1), m.group(2)
        text = a.get_text(strip=True)

        # 빈 텍스트나 "thumbnail" 등은 무시 (광고 카드의 제목 링크가 아닌 썸네일 링크)
        if not text or text.lower() == "thumbnail":
            # ad_id는 일단 기록해두되 데이터는 채우지 않음
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
        # 텍스트 있는 링크가 들어왔으면 덮어쓰기 (placeholder를 진짜 데이터로)
        ad_map[ad_id] = {
            "ad_id": ad_id,
            "prefix": prefix,
            "url": f"https://tvcf.co.kr/play/{prefix}-{ad_id}",
            "card_title": parsed["raw_title"],
            "onair": parsed["onair"],
            "registered_short": parsed["registered_short"],
        }

    # 등록일 정보가 있는 광고만 (썸네일만 있는 placeholder 제외)
    ads = [v for v in ad_map.values() if v.get("registered_short") is not None]
    print(f"     → 유효한 광고 카드: {len(ads)}개")
    return ads


def parse_card_text(text):
    """카드 텍스트에서 날짜 추출
    예: "ILLIT (아일릿)'똑똑..엄마야?' 편 6s | It's Me | ILLIT (아일릿)05.12(05.12)"
    """
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
    except requests.HTTPError as e:
        print(f"❌ HTTP 에러: {e}")
        return 1
    except Exception as e:
        print(f"❌ 목록 페이지 실패: {type(e).__name__}: {e}")
        return 1

    if is_first_run:
        yesterday = today_kst - timedelta(days=1)
        valid_dates = {
            today_kst.strftime("%m.%d"),
            yesterday.strftime("%m.%d"),
        }
        print(f"\n   첫 실행: 어제({yesterday}) + 오늘({today_kst}) 등록분")
        print(f"   유효 날짜 패턴: {valid_dates}")
        date_counts = {}
        for a in ads:
            d = a["registered_short"]
            date_counts[d] = date_counts.get(d, 0) + 1
        print(f"   광고 등록일 분포 (상위 10개): {dict(sorted(date_counts.items(), key=lambda x: x[0] or '', reverse=True)[:10])}")
        new_ads = [a for a in ads if a["registered_short"] in valid_dates]
    else:
        new_ads = [a for a in ads if a["ad_id"] not in known_ids]

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
        })
        return 0

    print(f"\n📋 {len(new_ads)}개 광고 상세 정보 수집 중...")
    enriched = []
    for i, ad in enumerate(new_ads, 1):
        print(f"   [{i}/{len(new_ads)}] {ad['ad_id']}", end=" ... ")
        detail = fetch_detail(ad)
        if detail:
            ad.update(detail)
            print(f"✓ {detail.get('advertiser')} | {detail.get('main_category')}")
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
