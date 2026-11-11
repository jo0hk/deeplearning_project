"""기상청 API허브 기상 데이터 수집기.

수집 대상
  1. ASOS 시간자료 (kma_sfctm3): 서울(108) 시간별 기온·강수량·습도·풍속
  2. 기상특보 이력 (wrn_met_data): 호우주의보·폭염특보 등 — 이벤트 텍스트 소스

강수확률은 예보값이라 과거치를 다시 받기 어렵다. 학습용으로는 관측값(1)과 특보 이력(2)을 쓴다.

실행 예
  python -m src.data.collectors.weather_collector --start 2025-11-01 --end 2026-10-31
  python -m src.data.collectors.weather_collector --start 2025-11-01 --end 2026-10-31 --only asos

확인 필요 (API허브 문서·실제 응답을 보고 맞출 것)
  - 두 엔드포인트의 URL·파라미터 이름 (ASOS_URL, WARNING_URL)
  - 한 번에 조회 가능한 최대 기간: 31일로 가정 (MAX_DAYS)
  - 결측값 표기: -9, -9.0, -99, -99.0 으로 가정. 강수량(RN)의 -9가 '결측'인지 '무강수'인지는
    전처리 단계에서 실제 비 온 날과 대조해 정한다. 여기서는 None으로만 바꾼다.
  - 특보 지역 필터(reg): 우선 전국(0)으로 받고, 서울 구역 코드는 응답을 보고 거른다.
"""

from __future__ import annotations

import argparse
from datetime import date

from .common import (
    RAW_DIR,
    date_chunks,
    get_with_retry,
    parse_date,
    require_env,
    save_json,
    save_text,
)

ASOS_URL = "https://apihub.kma.go.kr/api/typ01/url/kma_sfctm3.php"
WARNING_URL = "https://apihub.kma.go.kr/api/typ01/url/wrn_met_data.php"
SEOUL_STN = "108"
MAX_DAYS = 31

OUT_DIR = RAW_DIR / "weather"

# kma_sfctm3 응답 열 순서 (헤더에 GST·RN·SD·TE 등 같은 이름이 반복되어 고정 목록으로 이름을 붙인다)
ASOS_COLUMNS = [
    "YYMMDDHHMI", "STN", "WD", "WS", "GST_WD", "GST_WS", "GST_TM", "PA", "PS", "PT", "PR",
    "TA", "TD", "HM", "PV", "RN", "RN_DAY", "RN_JUN", "RN_INT", "SD_HR3", "SD_DAY", "SD_TOT",
    "WC", "WP", "WW", "CA_TOT", "CA_MID", "CH_MIN", "CT", "CT_TOP", "CT_MID", "CT_LOW",
    "VS", "SS", "SI", "ST_GD", "TS", "TE_005", "TE_01", "TE_02", "TE_03", "ST_SEA",
    "WH", "BF", "IR", "IX",
]
MISSING = {"-9", "-9.0", "-99", "-99.0", "-999", "-999.0"}


# ---------------------------------------------------------------------------
# 텍스트 표 파싱 (네트워크 없이 테스트 가능한 순수 함수)
# ---------------------------------------------------------------------------

def _data_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def _header_tokens(text: str, first_col: str) -> list[str] | None:
    """'#'로 시작하는 줄 중 first_col 로 시작하는 헤더를 찾아 토큰 목록으로 돌려준다."""
    for ln in text.splitlines():
        s = ln.lstrip("#").strip()
        if s.split()[:1] == [first_col]:
            return s.split()
    return None


def _dedupe(names: list[str]) -> list[str]:
    """같은 이름이 반복되면 두 번째부터 _2, _3 을 붙인다."""
    count: dict[str, int] = {}
    out = []
    for n in names:
        count[n] = count.get(n, 0) + 1
        out.append(n if count[n] == 1 else f"{n}_{count[n]}")
    return out


def _value(token: str):
    if token in MISSING:
        return None
    try:
        return float(token)
    except ValueError:
        return token


def check_error(text: str) -> None:
    """API허브는 키 오류 등을 본문 텍스트로 돌려준다. 데이터 줄이 하나도 없으면 원문 일부를 보여준다."""
    if not _data_lines(text):
        raise RuntimeError(f"데이터가 없거나 오류 응답입니다: {text[:300]!r}")


def parse_asos(text: str) -> list[dict]:
    """kma_sfctm3 응답 → [{datetime, stn, ta, rn, hm, ws, ...}, ...]"""
    rows = []
    header = _header_tokens(text, "YYMMDDHHMI")
    for ln in _data_lines(text):
        tokens = ln.split()
        if len(tokens) == len(ASOS_COLUMNS):
            names = ASOS_COLUMNS
        elif header and len(tokens) == len(header):
            names = _dedupe(header)
        else:
            raise ValueError(f"열 개수가 맞지 않습니다 ({len(tokens)}개): {ln[:120]}")
        rec = {name.lower(): _value(tok) for name, tok in zip(names, tokens)}
        tm = tokens[0]
        rec["datetime"] = f"{tm[0:4]}-{tm[4:6]}-{tm[6:8]} {tm[8:10]}:{tm[10:12]}"
        rec["stn"] = tokens[1]
        del rec["yymmddhhmi"]
        rows.append(rec)
    return rows


def parse_generic_table(text: str) -> list[dict]:
    """헤더 줄('#' 시작)의 열 이름으로 데이터 줄을 dict로 바꾼다.

    특보 응답 형식을 아직 확인하지 못해서 쓰는 범용 파서다. 헤더와 열 개수가 다른 줄은
    {'raw': 원문}으로 남겨 정보를 잃지 않게 한다. 형식을 확인하면 전용 파서로 바꿀 것.
    """
    lines = _data_lines(text)
    if not lines:
        return []
    n = len(lines[0].split())
    header = None
    for ln in text.splitlines():
        if ln.lstrip().startswith("#"):
            toks = ln.lstrip("#").strip().split()
            if len(toks) == n and not all(set(t) <= set("-=") for t in toks):
                header = _dedupe(toks)
    rows = []
    for ln in lines:
        toks = ln.split()
        if header and len(toks) == len(header):
            rows.append({h.lower(): t for h, t in zip(header, toks)})
        else:
            rows.append({"raw": ln})
    return rows


# ---------------------------------------------------------------------------
# API 호출
# ---------------------------------------------------------------------------

def _tm(d: date, end_of_day: bool = False) -> str:
    return d.strftime("%Y%m%d") + ("2300" if end_of_day else "0000")


def collect_asos(key: str, start: date, end: date, stn: str = SEOUL_STN) -> list[dict]:
    rows: list[dict] = []
    for c_start, c_end in date_chunks(start, end, MAX_DAYS):
        text = get_with_retry(
            ASOS_URL,
            params={"tm1": _tm(c_start), "tm2": _tm(c_end, True), "stn": stn, "help": 0, "authKey": key},
        )
        save_text(text, OUT_DIR / "asos_raw" / f"asos_{stn}_{c_start:%Y%m%d}_{c_end:%Y%m%d}.txt")
        check_error(text)
        part = parse_asos(text)
        rows.extend(part)
        print(f"  ASOS {c_start}~{c_end}: {len(part)}행")
    return rows


def collect_warnings(key: str, start: date, end: date, reg: str = "0") -> list[dict]:
    rows: list[dict] = []
    for c_start, c_end in date_chunks(start, end, MAX_DAYS):
        text = get_with_retry(
            WARNING_URL,
            params={
                "wrn": "A",  # 전체 특보 종류
                "reg": reg,
                "tmfc1": _tm(c_start),
                "tmfc2": _tm(c_end, True),
                "disp": 0,
                "help": 1,  # 헤더 설명 포함 — 형식 확인용
                "authKey": key,
            },
        )
        save_text(text, OUT_DIR / "warnings_raw" / f"wrn_{c_start:%Y%m%d}_{c_end:%Y%m%d}.txt")
        part = parse_generic_table(text)
        rows.extend(part)
        print(f"  특보 {c_start}~{c_end}: {len(part)}행")
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="기상청 API허브 기상 데이터 수집")
    ap.add_argument("--start", required=True, help="시작일 YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="종료일 YYYY-MM-DD")
    ap.add_argument("--only", choices=["asos", "warnings"], help="한 종류만 받기")
    ap.add_argument("--stn", default=SEOUL_STN, help="ASOS 지점 번호 (서울=108)")
    args = ap.parse_args()

    key = require_env("KMA_API_KEY")
    start, end = parse_date(args.start), parse_date(args.end)
    tag = f"{start:%Y%m%d}_{end:%Y%m%d}"

    if args.only in (None, "asos"):
        print(f"[ASOS] 지점 {args.stn}, {start} ~ {end}")
        rows = collect_asos(key, start, end, args.stn)
        out = save_json(rows, OUT_DIR / f"asos_{args.stn}_{tag}.json")
        print(f"완료: {len(rows)}행 → {out}")

    if args.only in (None, "warnings"):
        print(f"[특보] {start} ~ {end}")
        rows = collect_warnings(key, start, end)
        out = save_json(rows, OUT_DIR / f"warnings_{tag}.json")
        raw_only = sum(1 for r in rows if "raw" in r)
        print(f"완료: {len(rows)}행 → {out}")
        if raw_only:
            print(f"  헤더와 맞지 않아 원문으로 남긴 줄: {raw_only}개 — warnings_raw/ 를 보고 파서를 맞추세요.")


if __name__ == "__main__":
    main()
