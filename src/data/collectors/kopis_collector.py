"""KOPIS(공연예술통합전산망) 공연 데이터 수집기.

흐름
  1. 공연목록(pblprfr): 기간 + 지역(서울=11)으로 공연 ID 목록 수집
  2. 공연상세(pblprfr/{mt20id}): 공연시설 ID(mt10id), 공연 시간 안내(dtguidance) 등
  3. 공연시설상세(prfplc/{mt10id}): 주소, 위경도(la/lo), 객석 수(seatscale)
  4. 공연 1건당 레코드 1개로 합쳐 data/raw/events/ 에 JSON 저장

결과의 venue_lat / venue_lon 은 이벤트→역 매핑(반경 1km)에,
seat_scale / hall_seat_scale 은 이벤트 텍스트의 [예상 규모] 대용으로 쓸 수 있다.

실행 예
  python -m src.data.collectors.kopis_collector --start 2025-11-01 --end 2026-10-31

확인 필요 (KOPIS OpenAPI 문서 기준으로 실제 응답을 보고 맞출 것)
  - 공연목록 조회 기간 최대 일수: 31일로 가정 (LIST_MAX_DAYS)
  - 한 페이지 최대 행 수: 100으로 가정 (ROWS)
  - 오류 응답 형식: <returncode>/<errmsg> 태그로 가정
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
CALL_INTERVAL = 0.1  # 연속 호출 간격(초)

OUT_DIR = RAW_DIR / "events" / "kopis"


class KopisError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# XML 파싱 (네트워크 없이 테스트 가능한 순수 함수)
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


def hall_seat_scale(detail_fcltynm: str, facility: dict) -> int | None:
    """공연상세의 '시설명 (공연장명)'에서 공연장명을 뽑아 해당 홀의 객석 수를 찾는다."""
    m = re.search(r"\(([^()]*)\)\s*$", detail_fcltynm or "")
    if not m:
        return None
    hall = m.group(1).strip()
    for h in facility.get("mt13s") or []:
        if isinstance(h, dict) and h.get("prfplcnm", "").strip() == hall:
            return _to_int(h.get("seatscale"))
    return None


def build_event_record(item: dict, detail: dict | None, facility: dict | None) -> dict:
    """공연목록 1건 + 상세 + 시설 정보를 학습용 원본 레코드 1건으로 합친다."""
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
        "facility_id": detail.get("mt10id"),
        "venue_address": facility.get("adres"),
        "venue_lat": _to_float(facility.get("la")),
        "venue_lon": _to_float(facility.get("lo")),
        "seat_scale": _to_int(facility.get("seatscale")),
        "hall_seat_scale": hall_seat_scale(detail.get("fcltynm", ""), facility),
        "time_guidance": detail.get("dtguidance"),  # 예: "토요일(15:00,19:00)" — 파싱은 전처리 단계에서
        "runtime": detail.get("prfruntime"),
        "cast": detail.get("prfcast"),
    }


# ---------------------------------------------------------------------------
# API 호출
# ---------------------------------------------------------------------------

def fetch_performance_list(
    key: str, start: date, end: date, signgucode: str = SEOUL_SIGNGUCODE
) -> list[dict]:
    """기간 내 공연목록. 기간은 LIST_MAX_DAYS 단위로 나눠 조회하고 mt20id로 중복 제거."""
    seen: dict[str, dict] = {}
    for c_start, c_end in date_chunks(start, end, LIST_MAX_DAYS):
        for page in range(1, MAX_PAGES + 1):
            text = get_with_retry(
                f"{KOPIS_BASE}/pblprfr",
                params={
                    "service": key,
                    "stdate": c_start.strftime("%Y%m%d"),
                    "eddate": c_end.strftime("%Y%m%d"),
                    "cpage": page,
                    "rows": ROWS,
                    "signgucode": signgucode,
                },
            )
            items = parse_db_list(text)
            for it in items:
                if it.get("mt20id"):
                    seen.setdefault(it["mt20id"], it)
            print(f"  목록 {c_start}~{c_end} p{page}: {len(items)}건 (누적 {len(seen)})")
            time.sleep(CALL_INTERVAL)
            if len(items) < ROWS:
                break
        else:
            print(f"  경고: {c_start}~{c_end} 가 MAX_PAGES({MAX_PAGES})에 걸렸습니다. 기간을 줄이세요.")
    return list(seen.values())


def fetch_one(key: str, path: str) -> dict | None:
    items = parse_db_list(get_with_retry(f"{KOPIS_BASE}/{path}", params={"service": key}))
    time.sleep(CALL_INTERVAL)
    return items[0] if items else None


def _load_cache(name: str) -> dict:
    path = OUT_DIR / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def collect(start: date, end: date, signgucode: str = SEOUL_SIGNGUCODE, skip_detail: bool = False) -> list[dict]:
    key = require_env("KOPIS_SERVICE_KEY")
    tag = f"{start:%Y%m%d}_{end:%Y%m%d}"

    print(f"[1/3] 공연목록 수집 {start} ~ {end} (signgucode={signgucode})")
    items = fetch_performance_list(key, start, end, signgucode)
    save_json(items, OUT_DIR / f"list_{tag}.json")

    details: dict = {} if skip_detail else _load_cache("details_cache.json")
    facilities: dict = {} if skip_detail else _load_cache("facilities_cache.json")

    if not skip_detail:
        print(f"[2/3] 공연상세·시설상세 수집 ({len(items)}건, 캐시 {len(details)}건)")
        for i, it in enumerate(items, 1):
            pid = it["mt20id"]
            if pid not in details:
                details[pid] = fetch_one(key, f"pblprfr/{pid}") or {}
            fid = details[pid].get("mt10id")
            if fid and fid not in facilities:
                facilities[fid] = fetch_one(key, f"prfplc/{fid}") or {}
            if i % 100 == 0:  # 중간에 끊겨도 이어서 받을 수 있게 캐시 저장
                save_json(details, OUT_DIR / "details_cache.json")
                save_json(facilities, OUT_DIR / "facilities_cache.json")
                print(f"  {i}/{len(items)}")
        save_json(details, OUT_DIR / "details_cache.json")
        save_json(facilities, OUT_DIR / "facilities_cache.json")

    print("[3/3] 레코드 병합")
    records = []
    for it in items:
        d = details.get(it["mt20id"])
        f = facilities.get((d or {}).get("mt10id", ""))
        records.append(build_event_record(it, d, f))
    out = save_json(records, RAW_DIR / "events" / f"kopis_events_{tag}.json")

    no_coord = sum(1 for r in records if r["venue_lat"] is None)
    print(f"완료: {len(records)}건 → {out}")
    print(f"  좌표 없는 공연: {no_coord}건")
    return records


def main() -> None:
    ap = argparse.ArgumentParser(description="KOPIS 공연 데이터 수집")
    ap.add_argument("--start", required=True, help="시작일 YYYY-MM-DD")
    ap.add_argument("--end", required=True, help="종료일 YYYY-MM-DD")
    ap.add_argument("--signgucode", default=SEOUL_SIGNGUCODE, help="지역 코드 (서울=11)")
    ap.add_argument("--skip-detail", action="store_true", help="목록만 받기 (좌표·객석 수 없음)")
    args = ap.parse_args()
    collect(parse_date(args.start), parse_date(args.end), args.signgucode, args.skip_detail)


if __name__ == "__main__":
    main()
