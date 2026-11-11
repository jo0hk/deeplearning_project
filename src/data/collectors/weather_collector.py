"""기상청 API허브 기상 데이터 수집기.

수집 대상
  1. ASOS 시간자료 (kma_sfctm3): 서울(108) 시간별 기온·강수량·습도·풍속
  2. 기상특보 이력 (wrn_met_data): 호우주의보·폭염특보 등 — 이벤트 텍스트 소스

강수확률은 예보값이라 과거치를 다시 받기 어렵다. 학습용으로는 관측값(1)과 특보 이력(2)을 쓴다.

실행 예
  python -m src.data.collectors.weather_collector --start 2025-11-01 --end 2026-10-31
  python -m src.data.collectors.weather_collector --start 2025-11-01 --end 2026-10-31 --only asos

2026-08 실제 응답으로 확인한 것
  - ASOS: 46열, 열 순서는 ASOS_COLUMNS와 같다. 한 번에 최대 31일 조회 (API허브 문서).
  - ASOS 강수량(RN): -9(→None)인 시간은 모두 IR=3(무강수 또는 결측), 값이 있는 시간은 IR=1/4.
    즉 RN=None + IR=3 은 대부분 '비 안 옴'이다. 0으로 채울지는 전처리 단계에서 정한다.
  - 특보: 쉼표 구분 CSV, 줄 끝에 '=' 열이 붙는다. 열: WARNING_COLUMNS.
    응답에는 구역 이름(REG_NAME)이 없고 코드(REG_ID)만 온다.

확인 필요
  - 서울 특보구역 코드: 8월 자료에서 L1100100~L1100400 4개가 보여 서울 4개 권역으로 추정했다
    (SEOUL_REG_IDS). 특보구역 API(wrn_reg.php, 별도 활용신청 필요)로 이름을 대조해 확정할 것.
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

# wrn_met_data 응답 열 (help=1 응답의 헤더 설명 기준)
WARNING_COLUMNS = ["TM_FC", "TM_EF", "TM_IN", "STN", "REG_ID", "WRN", "LVL", "CMD", "GRD", "CNT", "RPT"]
WRN_NAMES = {
    "W": "강풍", "R": "호우", "C": "한파", "D": "건조", "O": "폭풍해일", "N": "지진해일",
    "V": "풍랑", "T": "태풍", "S": "대설", "Y": "황사", "H": "폭염", "F": "안개", "K": "열대야",
}
LVL_NAMES = {"1": "예비특보", "2": "주의보", "3": "경보", "4": "중대경보"}
CMD_NAMES = {"1": "발표", "2": "대치", "3": "해제", "4": "대치해제", "5": "연장", "6": "변경", "7": "변경해제"}
SEOUL_REG_IDS = {"L1100100", "L1100200", "L1100300", "L1100400"}  # 추정 — 모듈 설명 '확인 필요' 참고


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


def _kma_time(tm: str) -> str:
    """'202608011000' → '2026-08-01 10:00'"""
    return f"{tm[0:4]}-{tm[4:6]}-{tm[6:8]} {tm[8:10]}:{tm[10:12]}"


def parse_warnings(text: str) -> list[dict]:
    """wrn_met_data 응답(쉼표 CSV, 줄 끝 '=') → 특보 1건당 dict 1개.

    코드값 옆에 한글 이름(wrn_name, lvl_name, cmd_name)과 서울 여부(is_seoul, 추정)를 붙인다.
    """
    rows = []
    for ln in _data_lines(text):
        fields = [f.strip() for f in ln.split(",")]
        if fields and fields[-1] == "=":
            fields = fields[:-1]
        if len(fields) != len(WARNING_COLUMNS):
            raise ValueError(f"특보 열 개수가 맞지 않습니다 ({len(fields)}개): {ln[:120]}")
        rec = {c.lower(): v for c, v in zip(WARNING_COLUMNS, fields)}
        for k in ("tm_fc", "tm_ef", "tm_in"):
            rec[k] = _kma_time(rec[k])
        rec["wrn_name"] = WRN_NAMES.get(rec["wrn"])
        rec["lvl_name"] = LVL_NAMES.get(rec["lvl"])
        rec["cmd_name"] = CMD_NAMES.get(rec["cmd"])
        rec["is_seoul"] = rec["reg_id"] in SEOUL_REG_IDS
        rows.append(rec)
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
                "tmfc2": c_end.strftime("%Y%m%d") + "2359",  # 발표시각 기준이라 23:59까지 포함
                "disp": 0,
                "help": 1,  # 열 설명 포함 (원문 확인용)
                "authKey": key,
            },
        )
        save_text(text, OUT_DIR / "warnings_raw" / f"wrn_{c_start:%Y%m%d}_{c_end:%Y%m%d}.txt")
        check_error(text)
        part = parse_warnings(text)
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
        seoul = sum(1 for r in rows if r["is_seoul"])
        print(f"완료: {len(rows)}행 (서울 추정 {seoul}행) → {out}")


if __name__ == "__main__":
    main()
