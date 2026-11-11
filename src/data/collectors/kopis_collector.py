"""KOPIS(공연예술통합전산망) 공연 데이터 수집기.

KOPIS는 짧은 시간에 수백 번 호출하면 "Request Blocked"로 막는다 (2026-08 시험 수집에서 약 380회 후 차단).
그래서 공연마다 상세를 부르지 않고, 공연장 정보는 공연장당 한 번만 받아 재사용한다.

흐름
  1. 공연목록(pblprfr): 기간 + 지역(서울=11). 한 번 받은 기간은 파일을 재사용한다.
  2. 공연시설목록(prfplc): 서울 공연장 이름 → 시설 ID 대응표. 처음 한 번만 받는다.
  3. 공연시설상세(prfplc/{mt10id}): 주소, 위경도, 객석 수, 홀 목록. 공연장당 한 번.
  4. 공연상세(pblprfr/{mt20id}): 아래 경우에만 부른다.
       - 공연장 이름을 대응표에서 못 찾았을 때 (상세에서 시설 ID를 알아낸다)
       - 그 공연장의 가장 큰 홀이 --min-seats 이상일 때 (홀 ID, 공연 시간 안내가 필요)
     가장 큰 홀이 기준보다 작으면 그 공연은 기준 이상일 수 없으므로 건너뛴다.
  5. 공연 1건당 레코드 1개로 합쳐 data/raw/events/ 에 JSON 저장.

호출 수가 --max-calls 에 이르면 차단되기 전에 멈추고 캐시를 저장한다. 다시 실행하면 이어서 받는다.

결과의 venue_lat / venue_lon 은 이벤트→역 매핑(반경 1km)에,
hall_seat_scale(없으면 max_hall_seat_scale)은 이벤트 텍스트의 [예상 규모] 대용으로 쓸 수 있다.

실행 예
  python -m src.data.collectors.kopis_collector --start 2026-08-01 --end 2026-08-31
  python -m src.data.collectors.kopis_collector --start 2026-08-01 --end 2026-08-31 --min-seats 0  # 모든 공연 상세

확인된 것 (2026-08 실제 응답)
  - 공연목록 한 페이지 최대 100건, 31일 구간 조회 정상.
  - 목록의 fcltynm 은 시설 이름(홀 이름 아님). 같은 이름은 항상 같은 시설 ID.
  - 공연상세에 홀 ID(mt13id)가 있어 시설의 홀 목록과 ID로 맞출 수 있다.
  - 공연시설목록(서울 1,794곳)의 fcltynm 이 공연목록 fcltynm 과 같은 표기다. 8월 공연장 401곳 모두 찾았고
    공연상세의 시설 ID와 어긋난 경우 0건.
확인 필요
  - KOPIS 호출 제한의 정확한 기준. 시험에서 약 380회 연속 호출 후, 그리고 15분쯤 뒤 약 110회 후 막혔다.
    순간 속도보다 일정 시간 누적 호출 수로 막는 것으로 보인다 (추정). 막히면 받은 만큼 저장하고 멈춘다.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import date

from .common import (
    RAW_DIR,
    date_chunks,
    get_with_retry,
    parse_date,
    require_env,
    save_json,
)

KOPIS_BASE = "http://www.kopis.or.kr/openApi/restful"
LIST_MAX_DAYS = 31
ROWS = 100
MAX_PAGES = 200
SEOUL_SIGNGUCODE = "11"
CALL_INTERVAL = 1.0  # 연속 호출 간격(초). --interval 로 바꿀 수 있다
DEFAULT_MIN_SEATS = 1000  # 이 규모 이상 홀의 공연만 상세 조회. 기준은 팀에서 정할 것
DEFAULT_MAX_CALLS = 300

OUT_DIR = RAW_DIR / "events" / "kopis"


class KopisError(RuntimeError):
    pass


class CallBudgetExceeded(RuntimeError):
    pass


class KopisBlocked(RuntimeError):
    """KOPIS가 'Request Blocked'로 막았을 때. 시간을 두고 다시 실행하면 이어서 받는다."""


class CallBudget:
    """한 번 실행에서 쓸 수 있는 API 호출 수."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    def spend(self) -> None:
        if self.limit and self.used >= self.limit:
            raise CallBudgetExceeded(f"호출 한도 {self.limit}회에 도달")
        self.used += 1


# ---------------------------------------------------------------------------
# 파싱·판단 (네트워크 없이 테스트 가능한 순수 함수)
# ---------------------------------------------------------------------------

def _element_to_dict(el: ET.Element) -> dict:
    """자식이 없는 태그는 문자열로, 자식이 있는 태그는 리스트로 바꾼다."""
    out: dict = {}
    for child in el:
        if len(child) == 0:
            out[child.tag] = (child.text or "").strip()
        else:
            out[child.tag] = [
                _element_to_dict(c) if len(c) else (c.text or "").strip() for c in child
            ]
    return out


def parse_db_list(xml_text: str) -> list[dict]:
    """<dbs><db>...</db></dbs> 응답을 dict 리스트로 바꾼다. 오류 응답이면 KopisError."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as err:
        raise KopisError(f"XML 파싱 실패: {xml_text[:200]!r}") from err
    items = [_element_to_dict(db) for db in root.iter("db")]
    for item in items:
        if "returncode" in item or "errmsg" in item:
            raise KopisError(f"KOPIS 오류 응답: {item}")
    return items


def kopis_date(text: str) -> str | None:
    """'2026.01.31' → '2026-01-31'. 형식이 다르면 None."""
    m = re.fullmatch(r"(\d{4})\.(\d{2})\.(\d{2})", (text or "").strip())
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _to_float(text) -> float | None:
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    return value if value != 0 else None  # 좌표 0은 미입력으로 본다


def _to_int(text) -> int | None:
    digits = re.sub(r"[^\d]", "", str(text or ""))
    return int(digits) if digits else None


def _halls(facility: dict) -> list[dict]:
    return [h for h in (facility or {}).get("mt13s") or [] if isinstance(h, dict)]


def max_hall_seats(facility: dict | None) -> int | None:
    """시설에서 가장 큰 홀의 객석 수. 홀 목록이 없으면 시설 전체 객석 수."""
    if not facility:
        return None
    seats = [s for s in (_to_int(h.get("seatscale")) for h in _halls(facility)) if s is not None]
    return max(seats) if seats else _to_int(facility.get("seatscale"))


def needs_detail(facility_known: bool, facility: dict | None, min_seats: int) -> bool:
    """이 공연의 공연상세를 불러야 하는지.

    - 시설을 모르면 상세에서 시설 ID를 알아내야 하므로 True
    - 시설의 가장 큰 홀이 min_seats 미만이면 이 공연도 그보다 작으므로 False
    - 객석 수를 모르면 판단할 수 없으므로 True
    """
    if min_seats <= 0 or not facility_known:
        return True
    biggest = max_hall_seats(facility)
    return biggest is None or biggest >= min_seats


def build_venue_index(facility_list: list[dict]) -> dict[str, str]:
    """공연시설목록 → {시설 이름: 시설 ID}. 이름이 겹치는 시설은 넣지 않는다 (상세로 알아낸다)."""
    seen: dict[str, set] = {}
    for f in facility_list:
        name, fid = (f.get("fcltynm") or "").strip(), (f.get("mt10id") or "").strip()
        if name and fid:
            seen.setdefault(name, set()).add(fid)
    return {name: ids.pop() for name, ids in seen.items() if len(ids) == 1}


def hall_seat_scale(detail: dict, facility: dict) -> int | None:
    """공연이 열린 홀의 객석 수.

    1순위: 공연상세의 홀 ID(mt13id)와 시설의 홀 목록(mt13s)을 ID로 맞춘다.
    2순위: 공연상세 fcltynm('시설명 (홀명)') 안에 홀 이름이 들어 있는지 본다.
           홀 이름에 괄호가 또 있는 경우(예: '아틀리에홀 (아틀리에홀 (B1) )')가 있어 정규식 대신 포함 여부로 찾는다.
    3순위: 상세가 없어도 홀이 하나뿐인 시설이면 그 홀이다.
    """
    halls = _halls(facility)
    hall_id = (detail.get("mt13id") or "").strip()
    if hall_id:
        for h in halls:
            if h.get("mt13id", "").strip() == hall_id:
                return _to_int(h.get("seatscale"))
    name = detail.get("fcltynm") or ""
    matches = [h for h in halls if h.get("prfplcnm", "").strip() and f"({h['prfplcnm'].strip()}" in name]
    if matches:
        best = max(matches, key=lambda h: len(h["prfplcnm"]))
        return _to_int(best.get("seatscale"))
    if len(halls) == 1:
        return _to_int(halls[0].get("seatscale"))
    return None


def build_event_record(
    item: dict, detail: dict | None, facility: dict | None, facility_id: str | None = None
) -> dict:
    """공연목록 1건 + (있으면) 상세 + 시설 정보를 학습용 원본 레코드 1건으로 합친다."""
    detail = detail or {}
    facility = facility or {}
    return {
        "source": "kopis",
        "event_id": item.get("mt20id"),
        "name": item.get("prfnm") or detail.get("prfnm"),
        "genre": item.get("genrenm") or detail.get("genrenm"),
        "period_from": kopis_date(item.get("prfpdfrom") or detail.get("prfpdfrom", "")),
        "period_to": kopis_date(item.get("prfpdto") or detail.get("prfpdto", "")),
        "state": item.get("prfstate") or detail.get("prfstate"),
        "openrun": item.get("openrun"),
        "venue_name": detail.get("fcltynm") or item.get("fcltynm"),
        "facility_id": detail.get("mt10id") or facility_id or facility.get("mt10id"),
        "venue_address": facility.get("adres"),
        "venue_lat": _to_float(facility.get("la")),
        "venue_lon": _to_float(facility.get("lo")),
        "seat_scale": _to_int(facility.get("seatscale")),
        "max_hall_seat_scale": max_hall_seats(facility) if facility else None,
        "hall_id": detail.get("mt13id"),
        "hall_seat_scale": hall_seat_scale(detail, facility),
        "detail_fetched": bool(detail),
        "time_guidance": detail.get("dtguidance"),  # 예: "토요일(15:00,19:00)" — 파싱은 전처리 단계에서
        "runtime": detail.get("prfruntime"),
        "cast": detail.get("prfcast"),
    }


# ---------------------------------------------------------------------------
# API 호출
# ---------------------------------------------------------------------------

def _get(key: str, path: str, budget: CallBudget, params: dict | None = None) -> list[dict]:
    budget.spend()
    try:
        text = get_with_retry(f"{KOPIS_BASE}/{path}", params={"service": key, **(params or {})})
    except RuntimeError as err:
        if "Request Blocked" in str(err):
            raise KopisBlocked(f"KOPIS가 요청을 막았습니다 ({path}, 이번 실행 {budget.used}번째 호출)") from None
        raise
    time.sleep(CALL_INTERVAL)
    return parse_db_list(text)


def _paged(key: str, path: str, budget: CallBudget, params: dict, label: str) -> list[dict]:
    out: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        items = _get(key, path, budget, {**params, "cpage": page, "rows": ROWS})
        out.extend(items)
        print(f"  {label} p{page}: {len(items)}건 (누적 {len(out)})")
        if len(items) < ROWS:
            return out
    print(f"  경고: {label} 가 MAX_PAGES({MAX_PAGES})에 걸렸습니다.")
    return out


def fetch_performance_list(key: str, start: date, end: date, signgucode: str, budget: CallBudget) -> list[dict]:
    """기간 내 공연목록. LIST_MAX_DAYS 단위로 나눠 조회하고 mt20id로 중복 제거."""
    seen: dict[str, dict] = {}
    for c_start, c_end in date_chunks(start, end, LIST_MAX_DAYS):
        params = {"stdate": f"{c_start:%Y%m%d}", "eddate": f"{c_end:%Y%m%d}", "signgucode": signgucode}
        for it in _paged(key, "pblprfr", budget, params, f"공연목록 {c_start}~{c_end}"):
            if it.get("mt20id"):
                seen.setdefault(it["mt20id"], it)
    return list(seen.values())


def _load(name: str, default):
    path = OUT_DIR / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def collect(
    start: date,
    end: date,
    signgucode: str = SEOUL_SIGNGUCODE,
    min_seats: int = DEFAULT_MIN_SEATS,
    max_calls: int = DEFAULT_MAX_CALLS,
    refresh_list: bool = False,
) -> list[dict]:
    key = require_env("KOPIS_SERVICE_KEY")
    tag = f"{start:%Y%m%d}_{end:%Y%m%d}"
    budget = CallBudget(max_calls)

    details: dict = _load("details_cache.json", {})
    facilities: dict = _load("facilities_cache.json", {})
    venue_index: dict = _load("venue_index.json", {})  # 목록 fcltynm → 시설 ID
    items: list[dict] = [] if refresh_list else _load(f"list_{tag}.json", [])
    stopped = False

    def save_caches() -> None:
        save_json(details, OUT_DIR / "details_cache.json")
        save_json(facilities, OUT_DIR / "facilities_cache.json")
        save_json(venue_index, OUT_DIR / "venue_index.json")

    try:
        if items:
            print(f"[1/4] 공연목록: 저장된 파일 사용 ({len(items)}건). 새로 받으려면 --refresh-list")
        else:
            print(f"[1/4] 공연목록 수집 {start} ~ {end}")
            items = fetch_performance_list(key, start, end, signgucode, budget)
            save_json(items, OUT_DIR / f"list_{tag}.json")

        fac_list_path = OUT_DIR / f"facility_list_{signgucode}.json"
        if fac_list_path.exists():
            print("[2/4] 공연시설목록: 저장된 파일 사용")
        else:
            print("[2/4] 공연시설목록 수집")
            fac_list = _paged(key, "prfplc", budget, {"signgucode": signgucode}, "공연시설목록")
            save_json(fac_list, fac_list_path)
            for name, fid in build_venue_index(fac_list).items():
                venue_index.setdefault(name, fid)

        print(f"[3/4] 시설·공연 상세 (기준 {min_seats}석, 호출 한도 {max_calls or '없음'})")
        skipped_small = 0
        for i, it in enumerate(items, 1):
            pid, vname = it["mt20id"], (it.get("fcltynm") or "").strip()
            if (details.get(pid) or {}).get("mt10id"):  # 이전 실행에서 받은 상세로 대응표 채우기
                venue_index.setdefault(vname, details[pid]["mt10id"])
            fid = venue_index.get(vname)
            if fid and fid not in facilities:
                facilities[fid] = (_get(key, f"prfplc/{fid}", budget) or [{}])[0]
            if pid not in details:
                if needs_detail(bool(fid), facilities.get(fid), min_seats):
                    details[pid] = (_get(key, f"pblprfr/{pid}", budget) or [{}])[0]
                    learned = details[pid].get("mt10id")
                    if learned:
                        venue_index.setdefault(vname, learned)
                        if learned not in facilities:
                            facilities[learned] = (_get(key, f"prfplc/{learned}", budget) or [{}])[0]
                else:
                    skipped_small += 1
            if i % 100 == 0:
                save_caches()
                print(f"  {i}/{len(items)} (호출 {budget.used}회)")
        print(f"  작은 공연장이라 상세를 건너뛴 공연: {skipped_small}건")
    except (CallBudgetExceeded, KopisBlocked) as err:
        stopped = True
        print(f"  {err}. 캐시를 저장하고 멈춥니다. 시간을 두고 같은 명령을 다시 실행하면 이어서 받습니다.")
    finally:  # 오류가 나도 받은 만큼 저장
        save_caches()

    print("[4/4] 레코드 병합")
    records = []
    for it in items:
        d = details.get(it["mt20id"])
        fid = (d or {}).get("mt10id") or venue_index.get((it.get("fcltynm") or "").strip())
        records.append(build_event_record(it, d, facilities.get(fid or ""), fid))
    out = save_json(records, RAW_DIR / "events" / f"kopis_events_{tag}.json")

    no_fac = sum(1 for r in records if r["venue_lat"] is None)
    big = sum(1 for r in records if (r["hall_seat_scale"] or r["max_hall_seat_scale"] or 0) >= max(min_seats, 1))
    print(f"{'중간 저장' if stopped else '완료'}: {len(records)}건 → {out} (이번 호출 {budget.used}회)")
    print(f"  공연장 정보 없는 공연: {no_fac}건 / 기준 이상 공연장 공연: {big}건")
    return records


def main() -> None:
    global CALL_INTERVAL
    ap = argparse.ArgumentParser(description="KOPIS 공연 데이터 수집")
    ap.add_argument("--start", required=True, help="시작일 YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="종료일 YYYY-MM-DD")
    ap.add_argument("--signgucode", default=SEOUL_SIGNGUCODE, help="지역 코드 (서울=11)")
    ap.add_argument("--min-seats", type=int, default=DEFAULT_MIN_SEATS,
                    help="가장 큰 홀이 이 객석 수 이상인 공연장의 공연만 상세 조회 (0이면 전부)")
    ap.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS,
                    help="이번 실행의 최대 호출 수 (0이면 제한 없음)")
    ap.add_argument("--interval", type=float, default=CALL_INTERVAL, help="호출 간격(초)")
    ap.add_argument("--refresh-list", action="store_true", help="저장된 공연목록 무시하고 다시 받기")
    args = ap.parse_args()
    CALL_INTERVAL = args.interval
    collect(parse_date(args.start), parse_date(args.end), args.signgucode,
            args.min_seats, args.max_calls, args.refresh_list)


if __name__ == "__main__":
    main()
