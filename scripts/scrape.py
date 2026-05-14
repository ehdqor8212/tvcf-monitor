"""
TVCF 미집행 광고주 슬랙 알림 스크립트
- 매일 평일 KST 13시에 실행 (cron-job.org -> GitHub Actions)
- latest.json + decisions.json 조합해서 슬랙으로 전송
- 미집행 → 집행 → 확인 필요 순서로 표시
"""
import os
import sys
import json
import re
import unicodedata
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


def classify_ad(ad, decisions, x_decisions):
    """
    광고를 미집행/집행/확인 필요로 분류
    - decisions (광고주명): O 또는 ⚠ 영구 결정
    - x_decisions (ad_id): X 결정 (그 광고만)
    - 결정 없으면 'unknown' 으로 표시 (작업자가 아직 매칭 안 한 상태)
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


def build_slack_message(today_str, ads, decisions, x_decisions, tvcf_total):
    """슬랙 메시지 본문 생성

    Args:
        tvcf_total: TVCF에서 발견된 전체 광고 수 (count_total_before_dedup)
    """

    # 분류
    x_ads = []   # 미집행
    o_ads = []   # 집행
    maybe_ads = []  # 확인 필요
    unknown_ads = []  # 결정 없음

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

    # 미결정은 미집행으로 간주 (안전한 쪽)
    x_ads.extend(unknown_ads)

    total = len(x_ads) + len(o_ads) + len(maybe_ads)

    # ============================================================
    # 신규 광고가 0건일 때 — 두 가지 케이스 구분
    # ============================================================
    if total == 0:
        if tvcf_total == 0:
            # 케이스 1: TVCF 사이트에 아예 신규 등록이 없음
            return (
                f"*[공유] {today_str} TVCF 내역 — 📭 신규 등록 광고 없음*\n"
                f"_오늘 TVCF에 새로 올라온 광고가 없습니다. 자동 크롤링은 정상 동작했습니다._"
            )
        else:
            # 케이스 2: TVCF에 광고는 있지만 모두 기존 광고주
            return (
                f"*[공유] {today_str} TVCF 내역 — 💼 신규 광고주 없음*\n"
                f"_오늘 TVCF에 {tvcf_total}건의 광고가 올라왔지만, 모두 기존에 추적 중인 광고주입니다._"
            )

    # ============================================================
    # 신규 광고가 있을 때 — 기존 로직대로 분류해서 표시
    # ============================================================
    header = f"*[공유] {today_str} TVCF 내역*\n"
    header += f"<{SHAREPOINT_URL}|TVCF 미집행 광고주> 공유드립니다.\n"

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

    # latest.json 읽기
    if not LATEST_FILE.exists():
        print(f"❌ {LATEST_FILE} 파일 없음")
        send_to_slack(f"*[공유] {today_str} TVCF 내역 — 데이터 없음*\n_크롤링 결과 파일을 찾을 수 없습니다._")
        sys.exit(1)

    with open(LATEST_FILE, 'r', encoding='utf-8') as f:
        latest = json.load(f)
    ads = latest.get('new_ads', [])
    tvcf_total = latest.get('count_total_before_dedup', 0)  # TVCF 사이트의 전체 광고 수
    print(f"📊 신규 광고: {len(ads)}건 / TVCF 전체: {tvcf_total}건")

    # decisions.json 읽기 (없을 수도 있음)
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
    message = build_slack_message(today_str, ads, decisions, x_decisions, tvcf_total)
    print("\n=== 슬랙으로 전송할 메시지 ===")
    print(message)
    print("=== 끝 ===\n")

    # 전송
    send_to_slack(message)
    print("✅ 완료!")


if __name__ == '__main__':
    main()
