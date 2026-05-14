"""
history.json 보정 스크립트
- advertiser_code 또는 brand가 누락된 항목을 staffs API + og:title로 다시 채움
- 한 번 실행하고 완료되면 다시 실행할 필요 없음

실행: python scripts/fix_history.py
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
STAFFS_API_TEMPLATE = "https://tvcf.co.kr/api/main/v1/play/{idx}/staffs"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
}

API_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "*/*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://tvcf.co.kr/",
}

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parent.parent
HISTORY_FILE = ROOT / "data" / "history.json"
BACKUP_FILE = ROOT / "data" / "history_backup_before_fix.json"

DELAY_SEC = 0.5


def fetch_brand_from_detail(url):
    """상세 페이지에서 brand (og:title) 추출"""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        og_title_tag = soup.find("meta", attrs={"property": "og:title"})
        if og_title_tag and og_title_tag.get("content"):
            return og_title_tag["content"].split("|")[0].strip()
    except Exception as e:
        print(f"    ⚠️  brand 추출 실패: {e}")
    return None


def fetch_advertiser_from_api(ad_id):
    """staffs API에서 진짜 광고주명 추출"""
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
        print(f"    ⚠️  staffs API 실패: {e}")
        return None, None

    if payload.get("status") != 200:
        return None, None

    data = payload.get("data") or {}
    results = data.get("results") or []
    if not results:
        return None, None

    staffs_obj = results[0].get("staffs") or {}
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
        return advertiser_name, advertiser_code

    return None, None


def is_broken(ad):
    """보정이 필요한 항목인지 판단:
    - advertiser_code가 없거나
    - brand 필드가 없거나
    - advertiser_fallback 플래그가 없음 (옛날 데이터)
    """
    if not ad.get("advertiser_code"):
        return True
    if "brand" not in ad:
        return True
    if "advertiser_fallback" not in ad:
        return True
    return False


def main():
    print(f"🔧 history.json 보정 시작: {datetime.now(KST).isoformat()}\n")

    if not HISTORY_FILE.exists():
        print(f"❌ {HISTORY_FILE} 파일이 없습니다.")
        return 1

    # 1. 읽기
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        history = json.load(f)
    print(f"📂 history.json 로드: 총 {len(history)}건")

    # 2. 백업 저장
    with open(BACKUP_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)
    print(f"💾 백업 저장: {BACKUP_FILE.name}")

    # 3. 보정 대상 식별
    broken_items = [ad for ad in history if is_broken(ad)]
    clean_items = [ad for ad in history if not is_broken(ad)]
    print(f"\n📊 분류:")
    print(f"   ✅ 정상 (그대로 유지): {len(clean_items)}건")
    print(f"   ❌ 보정 필요: {len(broken_items)}건")

    if not broken_items:
        print("\n✨ 보정할 항목이 없습니다.")
        return 0

    # 4. 각 항목 보정
    print(f"\n📋 {len(broken_items)}개 항목 보정 중...\n")
    fixed = []
    failed = []
    for i, ad in enumerate(broken_items, 1):
        ad_id = ad["ad_id"]
        print(f"   [{i}/{len(broken_items)}] {ad_id} ({ad.get('advertiser', '?')})", end=" ... ")

        # brand가 없으면 og:title에서 가져오기
        if not ad.get("brand"):
            url = ad.get("url") or f"https://tvcf.co.kr/play/{ad.get('prefix', '')}-{ad_id}"
            brand = fetch_brand_from_detail(url)
            if brand:
                ad["brand"] = brand
            time.sleep(DELAY_SEC)

        # staffs API로 광고주 가져오기
        advertiser, advertiser_code = fetch_advertiser_from_api(ad_id)

        if advertiser:
            old_adv = ad.get("advertiser")
            ad["advertiser"] = advertiser
            ad["advertiser_code"] = advertiser_code
            ad["advertiser_fallback"] = False
            if old_adv and old_adv != advertiser:
                print(f"✓ '{old_adv}' → '{advertiser}'")
            else:
                print(f"✓ '{advertiser}' (확정)")
            fixed.append(ad)
        else:
            # 광고주 못 가져옴: brand만이라도 유지하고 fallback 표시
            if ad.get("brand"):
                ad["advertiser_fallback"] = True
                ad["advertiser_code"] = None
                print(f"⚠ 광고주 못찾음 (brand: '{ad.get('brand')}' 유지)")
            else:
                ad["advertiser_fallback"] = True
                ad["advertiser_code"] = None
                ad["brand"] = ad.get("advertiser")  # 마지막 fallback
                print(f"⚠ 모두 못찾음 (advertiser를 brand로 복사)")
            failed.append(ad)

        time.sleep(DELAY_SEC)

    # 5. 합치고 저장
    merged = clean_items + fixed + failed
    # 정렬: registered_date 내림차순, ad_id 내림차순
    merged.sort(key=lambda x: (x.get("registered_date") or "", x.get("ad_id", "")), reverse=True)

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    print(f"\n💾 history.json 저장 완료: 총 {len(merged)}건")
    print(f"   - 기존 정상: {len(clean_items)}건")
    print(f"   - 보정 성공: {len(fixed)}건")
    print(f"   - 보정 실패: {len(failed)}건")
    print(f"\n✅ 완료!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
