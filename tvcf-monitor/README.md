# TVCF 신규 광고 모니터링

매일 한국시간 9시에 [TVCF](https://tvcf.co.kr) 사이트에서 신규 등록된 광고를 자동으로 수집해서 저장한다.

## 동작 방식

1. GitHub Actions가 매일 UTC 0시(=KST 9시)에 자동 실행
2. `scripts/scrape.py`가 TVCF 목록 페이지에서 신규 광고 ID 수집
3. 각 광고의 상세 페이지에서 광고주명, 캠페인명, 카테고리, 등록일 추출
4. 결과를 `data/` 폴더에 JSON으로 저장하고 commit

## 파일 구조

```
.
├── .github/workflows/daily.yml  # 자동 실행 설정
├── scripts/
│   ├── scrape.py                # 메인 크롤링 스크립트
│   └── requirements.txt         # Python 패키지
└── data/
    ├── latest.json              # 가장 최근 실행 결과
    ├── history.json             # 누적된 모든 신규 광고
    └── state.json               # 마지막 실행 시각, 알려진 광고 ID
```

## 수동 실행

GitHub repo 페이지 → **Actions** 탭 → **Daily TVCF Crawl** → **Run workflow** 클릭

## 로컬 테스트

```bash
pip install -r scripts/requirements.txt
python scripts/scrape.py
```
