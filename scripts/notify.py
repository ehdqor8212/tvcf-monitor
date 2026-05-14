"""
TVCF 미집행 광고주 슬랙 알림 스크립트
- 매일 평일 KST 13시에 실행 (cron-job.org -> GitHub Actions)
- latest.json + history.json + decisions.json 조합해서 슬랙으로 전송
- 미집행 → 집행 → 확인 필요 → 검토 필요 순서로 표시
- 월요일에는 주말(토·일·월) 데이터까지 합쳐서 전송
- 평일에는 당일 데이터만
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

# 파일 경로
DATA_DIR = Path('data')
LATEST_FILE = DATA_DIR / 'latest.json'
HISTORY_FILE = DATA_DIR / 'history.json'
DECISIONS_FILE = DATA_DIR / 'decisions.json'


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


def get_target_dates(today):
    """알림 대상 날짜 범위 결정
    - 월요일(weekday=0): 토(-2), 일(-1), 월(0) 3일치
    - 그 외 평일: 당일만
    """
    if today.weekday() == 0:  # 월요일
        target_dates = [
            (today - timedelta(days=2)).date(),  # 토
            (today - timedelta(days=1)).date(),  # 일
            today.date(),                         # 월
        ]
    else:
        target_dates = [today.date()]
    return target_dates


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


def collect_ads_for_dates(target_dates):
    """대상 날짜의 광고를 latest.json + history.json에서 수집
    - 같은 ad_id 중복 제거
    """
    target_date_strs = {d.strftime('%Y-%m-%d') for d in target_dates}

    collected = {}  # ad_id -> ad

    # 1. latest.json 우선 (최신 정보)
    if LATEST_FILE.exists():
        with open(LATEST_FILE, 'r', encoding='utf-8') as f:
            latest = json.load(f)
        for ad in latest.get('new_ads', []):
            reg_date = (ad.get('registered_date') or '')[:10]
            if reg_date in target_date_strs:
                collected[ad['ad_id']] = ad

    # 2. history.json에서 보충 (latest에 없는 것)
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            history = json.load(f)
        for ad in history:
            reg_date = (ad.get('registered_date') or '')[:10]
            if reg_date in target_date_strs and ad['ad_id'] not in collected:
                collected[ad['ad_id']] = ad

    return list(collected.values())


def sort_ads_by_date_desc(ads):
    """등록일 내림차순 정렬 (최신순)"""
    return sorted(
        ads,
        key=lambda a: (a.get('registered_date') or '', a.get('ad_id', '')),
        reverse=True
    )


def build_slack_message(today_str, ads, decisions, x_decisions, tvcf_total, is_monday):
    """슬랙 메시지 본문 생성

    Args:
        today_str: "5/14(목)" 형식
        ads: 알림 대상 광고 목록
        decisions: 영구 결정 (광고주명 기준)
        x_decisions: 1회용 X 결정 (ad_id 기준)
        tvcf_total: 오늘 크롤링한 TVCF 전체 광고 수 (count_total_before_dedup)
        is_monday: 월요일이면 True (주말 합쳐서 알림)
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
        period_label = "주말~월요일 동안" if is_monday else "오늘"
        if tvcf_total == 0:
            return (
                f"*[공유] {today_str} TVCF 내역 공유 — 📭 신규 등록 광고 없음*\n"
                f"_{period_label} TVCF에 새로 올라온 광고가 없습니다. 자동 크롤링은 정상 동작했습니다._"
            )
        else:
            return (
                f"*[공유] {today_str} TVCF 내역 공유 — 💼 신규 광고주 없음*\n"
                f"_{period_label} TVCF에 {tvcf_total}건의 광고가 올라왔지만, 모두 기존에 추적 중인 광고주입니다._"
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
    is_monday = today.weekday() == 0
    print(f"📅 오늘: {today_str} (월요일={is_monday})")

    # 대상 날짜 범위 결정
    target_dates = get_target_dates(today)
    print(f"📆 알림 대상 날짜: {[d.strftime('%Y-%m-%d') for d in target_dates]}")

    # latest.json 메타 정보 (TVCF 전체 광고 수)
    tvcf_total = 0
    if LATEST_FILE.exists():"""
TVCF 미집행 광고주 슬랙 알림 스크립트
- 매일 평일 KST 13시에 실행 (cron-job.org -> GitHub Actions)
- latest.json + history.json + decisions.json 조합해서 슬랙으로 전송
- 미집행 → 집행 → 확인 필요 → 검토 필요 순서로 표시
- 월요일에는 주말(토·일·월) 데이터까지 합쳐서 전송
- 평일에는 당일 데이터만
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

# 파일 경로
DATA_DIR = Path('data')
LATEST_FILE = DATA_DIR / 'latest.json'
HISTORY_FILE = DATA_DIR / 'history.json'
DECISIONS_FILE = DATA_DIR / 'decisions.json'


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


def get_target_dates(today):
    """알림 대상 날짜 범위 결정
    - 월요일(weekday=0): 토(-2), 일(-1), 월(0) 3일치
    - 그 외 평일: 당일만
    """
    if today.weekday() == 0:  # 월요일
        target_dates = [
            (today - timedelta(days=2)).date(),  # 토
            (today - timedelta(days=1)).date(),  # 일
            today.date(),                         # 월
        ]
    else:
        target_dates = [today.date()]
    return target_dates


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


def collect_ads_for_dates(target_dates):
    """대상 날짜의 광고를 latest.json + history.json에서 수집
    - 같은 ad_id 중복 제거
    """
    target_date_strs = {d.strftime('%Y-%m-%d') for d in target_dates}

    collected = {}  # ad_id -> ad

    # 1. latest.json 우선 (최신 정보)
    if LATEST_FILE.exists():
        with open(LATEST_FILE, 'r', encoding='utf-8') as f:
            latest = json.load(f)
        for ad in latest.get('new_ads', []):
            reg_date = (ad.get('registered_date') or '')[:10]
            if reg_date in target_date_strs:
                collected[ad['ad_id']] = ad

    # 2. history.json에서 보충 (latest에 없는 것)
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
            history = json.load(f)
        for ad in history:
            reg_date = (ad.get('registered_date') or '')[:10]
            if reg_date in target_date_strs and ad['ad_id'] not in collected:
                collected[ad['ad_id']] = ad

    return list(collected.values())


def sort_ads_by_date_desc(ads):
    """등록일 내림차순 정렬 (최신순)"""
    return sorted(
        ads,
        key=lambda a: (a.get('registered_date') or '', a.get('ad_id', '')),
        reverse=True
    )


def build_slack_message(today_str, ads, decisions, x_decisions, tvcf_total, is_monday):
    """슬랙 메시지 본문 생성

    Args:
        today_str: "5/14(목)" 형식
        ads: 알림 대상 광고 목록
        decisions: 영구 결정 (광고주명 기준)
        x_decisions: 1회용 X 결정 (ad_id 기준)
        tvcf_total: 오늘 크롤링한 TVCF 전체 광고 수 (count_total_before_dedup)
        is_monday: 월요일이면 True (주말 합쳐서 알림)
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
        period_label = "주말~월요일 동안" if is_monday else "오늘"
        if tvcf_total == 0:
            return (
                f"*[공유] {today_str} TVCF 내역 공유 — 📭 신규 등록 광고 없음*\n"
                f"_{period_label} TVCF에 새로 올라온 광고가 없습니다. 자동 크롤링은 정상 동작했습니다._"
            )
        else:
            return (
                f"*[공유] {today_str} TVCF 내역 공유 — 💼 신규 광고주 없음*\n"
                f"_{period_label} TVCF에 {tvcf_total}건의 광고가 올라왔지만, 모두 기존에 추적 중인 광고주입니다._"
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
    is_monday = today.weekday() == 0
    print(f"📅 오늘: {today_str} (월요일={is_monday})")

    # 대상 날짜 범위 결정
    target_dates = get_target_dates(today)
    print(f"📆 알림 대상 날짜: {[d.strftime('%Y-%m-%d') for d in target_dates]}")

    # latest.json 메타 정보 (TVCF 전체 광고 수)
    tvcf_total = 0
    if LATEST_FILE.exists():
        try:
            with open(LATEST_FILE, 'r', encoding='utf-8') as f:
                latest = json.load(f)
            tvcf_total = latest.get('count_total_before_dedup', 0)
        except Exception as e:
            print(f"⚠ latest.json 읽기 실패: {e}")

    # 대상 날짜의 광고 수집
    ads = collect_ads_for_dates(target_dates)
    print(f"📊 대상 광고 수집: {len(ads)}건 / TVCF 오늘 전체: {tvcf_total}건")

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
        today_str, ads, decisions, x_decisions, tvcf_total, is_monday
    )
    print("\n=== 슬랙으로 전송할 메시지 ===")
    print(message)
    print("=== 끝 ===\n")

    # 전송
    send_to_slack(message)
    print("✅ 완료!")


if __name__ == '__main__':
    main()
        try:
            with open(LATEST_FILE, 'r', encoding='utf-8') as f:
                latest = json.load(f)
            tvcf_total = latest.get('count_total_before_dedup', 0)
        except Exception as e:
            print(f"⚠ latest.json 읽기 실패: {e}")

    # 대상 날짜의 광고 수집
    ads = collect_ads_for_dates(target_dates)
    print(f"📊 대상 광고 수집: {len(ads)}건 / TVCF 오늘 전체: {tvcf_total}건")

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
        today_str, ads, decisions, x_decisions, tvcf_total, is_monday
    )
    print("\n=== 슬랙으로 전송할 메시지 ===")
    print(message)
    print("=== 끝 ===\n")

    # 전송
    send_to_slack(message)
    print("✅ 완료!")


if __name__ == '__main__':
    main()
