"""
TVCF 미집행 광고주 슬랙 알림 스크립트
- 매일 평일 KST 13시에 실행 (cron-job.org -> GitHub Actions)
- latest.json + history.json + decisions.json 조합해서 슬랙으로 전송
- 미집행 → 집행 → 확인 필요 → 검토 필요 순서로 표시

[발송 이력(sent.json) 기반 로직]
- "아직 슬랙에 보내지 않은 광고 전부"를 대상으로 함 (온에어 날짜 무관)
- 발송한 광고의 ad_id는 sent.json에 기록 → 다음 실행 때 제외
- 이렇게 하면 직전 알림 이후 추가/갱신된 광고도 누락 없이 다음 알림에 포함됨
- 평일에만 실행되므로, 주말에 쌓인 광고는 월요일에 한꺼번에 발송됨 (자동)

[첫 실행 폭탄 방지]
- SINCE_DATE 이전(온에어 기준)의 광고는 "이미 처리된 것"으로 간주하고 발송하지 않음
- sent.json이 없거나 비어있어도, SINCE_DATE 이전 광고는 대상에서 제외됨
"""
import os
import sys
import json
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

KST = timezone(timedelta(hours=9))

# 환경 변수
SLACK_WEBHOOK_URL = os.environ.get('SLACK_WEBHOOK_URL', '')
SHAREPOINT_URL = "https://ehdqor8212.github.io/tvcf-monitor/share.html"

# 발송 대상 기준일 (이 날짜 이전 온에어 광고는 이미 처리된 것으로 간주, 발송 안 함)
# 6/30까지는 처리 완료로 보고, 7/1부터 발송 대상
SINCE_DATE = '2026-07-01'

# 파일 경로
DATA_DIR = Path('data')
LATEST_FILE = DATA_DIR / 'latest.json'
HISTORY_FILE = DATA_DIR / 'history.json'
DECISIONS_FILE = DATA_DIR / 'decisions.json'
SENT_FILE = DATA_DIR / 'sent.json'


def normalize_name(name):
    """매칭용 광고주명 정규화 (index.html과 동일 로직)"""
    if not name:
        return ''
    s = str(name).lower()
    s = re.sub(r'\s+', '', s)
    s = re.sub(r'[()()\[\]【】「」\'\',·.\-_/]', '', s)
    s = re.sub(r'주식회사|㈜|\(주\)|inc\.?|corp\.?|ltd\.?|co\.?', '', s)
    return s.strip()


def get_weekday_kr(d):
    """요일을 한글로 (월, 화, 수, 목, 금, 토, 일)"""
    return ['월', '화', '수', '목', '금', '토', '일'][d.weekday()]


def format_date_kr(date_str):
    """YYYY-MM-DD → M/D"""
    if not date_str:
        return ''
    try:
        d = datetime.fromisoformat(date_str[:10])
        return f"{d.month}/{d.day}"
    except Exception:
        return date_str


def classify_ad(ad, decisions, x_decisions):
    """
    광고를 미집행(X) / 집행(O) / 확인 필요(maybe) / 검토 필요(unknown)로 분류
    - x_decisions (ad_id): X 결정 (그 광고만)
    - decisions (광고주명): O 또는 ⚠ 영구 결정
    - 결정 없으면 'unknown' (검토 필요)
    """
    advertiser = ad.get('advertiser') or ad.get('brand') or ''
    ad_id = ad.get('ad_id')

    # 1. X 결정 (ad_id 기준) 우선
    if ad_id and str(ad_id) in x_decisions:
        return 'x'

    # 2. O/⚠ 결정 (광고주명 기준)
    if advertiser:
        key = normalize_name(advertiser)
        if key in decisions:
            return decisions[key].get('status', 'unknown')

    return 'unknown'


def build_ad_line(ad):
    """슬랙 메시지에 표시할 한 줄"""
    date = format_date_kr(ad.get('registered_date'))
    category = ad.get('main_category') or ''
    advertiser = ad.get('advertiser') or '—'
    # 브랜드가 비어있으면 광고주명으로 채움 (페이지와 동일하게)
    brand = ad.get('brand') or ad.get('advertiser') or '—'
    return f"• {date} | {category} | {advertiser} | {brand}"


def group_ads_by_advertiser(ads):
    """같은 (날짜+카테고리+광고주) 광고를 묶고 브랜드만 합침
    - 브랜드 3개까지 슬래시(/)로 연결
    - 그 이상이면 "외 N건" 추가
    - ad_id는 첫 번째 광고 것 유지 (정렬 안정성)
    """
    groups = {}  # (date, category, advertiser) -> list of brands
    order = []   # 순서 보존용

    for ad in ads:
        date = (ad.get('registered_date') or '')[:10]
        category = ad.get('main_category') or ''
        advertiser = ad.get('advertiser') or ad.get('brand') or ''
        brand = ad.get('brand') or advertiser

        key = (date, category, advertiser)
        if key not in groups:
            groups[key] = {
                'ad': ad,           # 대표 광고 (첫 번째)
                'brands': [],
                'brand_set': set(),
            }
            order.append(key)
        # 중복 브랜드 제거
        if brand and brand not in groups[key]['brand_set']:
            groups[key]['brands'].append(brand)
            groups[key]['brand_set'].add(brand)

    # 합쳐서 새 광고 객체 만들기
    merged_ads = []
    for key in order:
        group = groups[key]
        brands = group['brands']
        if len(brands) > 3:
            brand_text = '/'.join(brands[:3]) + f" 외 {len(brands) - 3}건"
        else:
            brand_text = '/'.join(brands)

        merged_ad = {**group['ad'], 'brand': brand_text}
        merged_ads.append(merged_ad)

    return merged_ads


def load_sent_ids():
    """이미 발송한 ad_id 집합 로드"""
    if SENT_FILE.exists():
        try:
            with open(SENT_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return set(str(x) for x in data.get('sent_ad_ids', []))
        except Exception as e:
            print(f"⚠ sent.json 읽기 실패 (빈 목록으로 시작): {e}")
    return set()


def save_sent_ids(sent_ids, newly_sent_count):
    """발송한 ad_id 집합 저장"""
    payload = {
        'updated_at': datetime.now(KST).isoformat(),
        'count': len(sent_ids),
        'last_sent_count': newly_sent_count,
        'sent_ad_ids': sorted(sent_ids),
    }
    DATA_DIR.mkdir(exist_ok=True)
    with open(SENT_FILE, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"💾 sent.json 저장: 누적 {len(sent_ids)}건 (이번 발송 {newly_sent_count}건)")


def collect_unsent_ads(sent_ids):
    """아직 발송하지 않은 광고를 latest.json + history.json에서 수집
    - SINCE_DATE 이전(온에어 기준) 광고는 제외 (첫 실행 폭탄 방지)
    - sent_ids에 있는 광고는 제외 (이미 발송함)
    - 같은 ad_id 중복 제거
    """
    collected = {}  # ad_id -> ad

    def consider(ad):
        ad_id = ad.get('ad_id')
        if ad_id is None:
            return
        ad_id_str = str(ad_id)
        # 이미 발송한 광고 제외
        if ad_id_str in sent_ids:
            return
        # 이미 수집된 광고면 스킵 (latest 우선)
        if ad_id_str in collected:
            return
        # SINCE_DATE 이전 온에어 광고는 제외
        reg_date = (ad.get('registered_date') or '')[:10]
        if reg_date and reg_date < SINCE_DATE:
            return
        collected[ad_id_str] = ad

    # 1. latest.json 우선 (최신 정보)
    if LATEST_FILE.exists():
        with open(LATEST_FILE, 'r', encoding='utf-8') as f:
            latest = json.load(f)
        for ad in latest.get('new_ads', []):
            consider(ad)

    # 2. history.json에서 보충 (latest에 없는 것)
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            history = json.load(f)
        for ad in history:
            consider(ad)

    return list(collected.values())


def sort_ads_by_date_desc(ads):
    """등록일 내림차순 정렬 (최신순)"""
    return sorted(
        ads,
        key=lambda a: (a.get('registered_date') or '', str(a.get('ad_id', ''))),
        reverse=True
    )


def build_slack_message(today_str, ads, decisions, x_decisions, tvcf_total):
    """슬랙 메시지 본문 생성

    Args:
        today_str: "5/14(목)" 형식
        ads: 알림 대상 광고 목록 (아직 발송 안 한 것)
        decisions: 영구 결정 (광고주명 기준)
        x_decisions: 1회용 X 결정 (ad_id 기준)
        tvcf_total: 오늘 크롤링한 TVCF 전체 광고 수 (count_total_before_dedup)
    """

    # 분류
    x_ads = []        # 미집행 (확정)
    o_ads = []        # 집행 (확정)
    maybe_ads = []    # 확인 필요
    unknown_ads = []  # 검토 필요 (아직 판단 안 함)

    for ad in ads:
        status = classify_ad(ad, decisions, x_decisions)
        if status == 'x':
            x_ads.append(ad)
        elif status == 'o':
            o_ads.append(ad)
        elif status == 'maybe':
            maybe_ads.append(ad)
        else:
            unknown_ads.append(ad)

    # 날짜 내림차순 정렬 (최신순)
    x_ads = sort_ads_by_date_desc(x_ads)
    o_ads = sort_ads_by_date_desc(o_ads)
    maybe_ads = sort_ads_by_date_desc(maybe_ads)
    unknown_ads = sort_ads_by_date_desc(unknown_ads)

    # 같은 광고주+날짜는 묶고 브랜드만 합치기
    x_ads = group_ads_by_advertiser(x_ads)
    o_ads = group_ads_by_advertiser(o_ads)
    maybe_ads = group_ads_by_advertiser(maybe_ads)
    unknown_ads = group_ads_by_advertiser(unknown_ads)

    total = len(x_ads) + len(o_ads) + len(maybe_ads) + len(unknown_ads)

    # ============================================================
    # 광고가 0건일 때 — 두 가지 케이스 구분
    # ============================================================
    if total == 0:
        if tvcf_total == 0:
            return (
                f"*[공유] {today_str} TVCF 내역 공유 — 📭 신규 등록 광고 없음*\n"
                f"_직전 알림 이후 TVCF에 새로 올라온 광고가 없습니다. 자동 크롤링은 정상 동작했습니다._"
            )
        else:
            return (
                f"*[공유] {today_str} TVCF 내역 공유 — 💼 신규 광고주 없음*\n"
                f"_직전 알림 이후 TVCF에 올라온 광고는 모두 기존에 추적 중인 광고주입니다._"
            )

    # ============================================================
    # 광고가 있을 때
    # ============================================================
    header = f"*[공유] {today_str} TVCF 내역 공유*\n"
    header += f"상세내역은 <{SHAREPOINT_URL}|TVCF 미집행 광고주>에서 확인해 주세요.\n"

    body = ""
    if x_ads:
        body += f"\n🔴 *미집행 ({len(x_ads)}건)*\n"
        body += "\n".join(build_ad_line(a) for a in x_ads)
        body += "\n"
    if o_ads:
        body += f"\n🟢 *집행 ({len(o_ads)}건)*\n"
        body += "\n".join(build_ad_line(a) for a in o_ads)
        body += "\n"
    if maybe_ads:
        body += f"\n🟡 *확인 필요 ({len(maybe_ads)}건)*\n"
        body += "\n".join(build_ad_line(a) for a in maybe_ads)
        body += "\n"
    if unknown_ads:
        body += f"\n⚪ *검토 필요 ({len(unknown_ads)}건)*\n"
        body += "\n".join(build_ad_line(a) for a in unknown_ads)
        body += "\n"

    return header + body


def send_to_slack(message):
    """Slack Webhook으로 메시지 전송"""
    if not SLACK_WEBHOOK_URL:
        print("❌ SLACK_WEBHOOK_URL 환경 변수가 설정되지 않았습니다.")
        sys.exit(1)

    payload = json.dumps({"text": message}).encode('utf-8')
    req = urllib.request.Request(
        SLACK_WEBHOOK_URL,
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"✓ Slack 전송 성공: HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        print(f"❌ Slack 전송 실패: HTTP {e.code} - {e.read().decode('utf-8', errors='replace')}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ Slack 전송 실패: {e}")
        sys.exit(1)


def main():
    today = datetime.now(KST)
    today_str = f"{today.month}/{today.day}({get_weekday_kr(today)})"
    print(f"📅 오늘: {today_str}")
    print(f"📌 발송 기준일(SINCE_DATE): {SINCE_DATE} 이후 온에어만 대상")

    # latest.json 메타 정보 (TVCF 전체 광고 수)
    tvcf_total = 0
    if LATEST_FILE.exists():
        try:
            with open(LATEST_FILE, 'r', encoding='utf-8') as f:
                latest = json.load(f)
            tvcf_total = latest.get('count_total_before_dedup', 0)
        except Exception as e:
            print(f"⚠ latest.json 읽기 실패: {e}")

    # 발송 이력 로드
    sent_ids = load_sent_ids()
    print(f"📮 기존 발송 이력: {len(sent_ids)}건")

    # 아직 발송 안 한 광고 수집
    ads = collect_unsent_ads(sent_ids)
    print(f"📊 이번 발송 대상(미발송): {len(ads)}건 / TVCF 오늘 전체: {tvcf_total}건")

    # decisions.json 읽기
    decisions = {}
    x_decisions = {}
    if DECISIONS_FILE.exists():
        try:
            with open(DECISIONS_FILE, 'r', encoding='utf-8') as f:
                d = json.load(f)
            decisions = d.get('decisions', {})
            x_decisions = d.get('x_decisions', {})
            print(f"📋 결정사항: {len(decisions)} 영구 + {len(x_decisions)} 1회용")
        except Exception as e:
            print(f"⚠ decisions.json 읽기 실패 (무시): {e}")

    # 메시지 생성
    message = build_slack_message(
        today_str, ads, decisions, x_decisions, tvcf_total
    )
    print("\n=== 슬랙으로 전송할 메시지 ===")
    print(message)
    print("=== 끝 ===\n")

    # 전송
    send_to_slack(message)

    # 발송 성공 후 이력 갱신 (이번에 대상이 된 ad_id 전부 기록)
    newly_sent = [str(a.get('ad_id')) for a in ads if a.get('ad_id') is not None]
    sent_ids.update(newly_sent)
    save_sent_ids(sent_ids, len(newly_sent))

    print("✅ 완료!")


if __name__ == '__main__':
    main()
