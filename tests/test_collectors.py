"""수집기 파싱 로직 테스트 (네트워크 없이 실행).

기상청(ASOS·특보) 테스트 일부는 2026-08 실제 응답의 줄을 그대로 쓴다.
KOPIS 샘플은 문서 형식을 바탕으로 직접 만든 것이다 (목록 응답 형식은 실제와 일치 확인).

실행: python -m unittest discover -s tests -v
"""

import unittest
from datetime import date

from src.data.collectors.common import date_chunks, parse_date, redact
from src.data.collectors.kopis_collector import (
    CallBudget,
    CallBudgetExceeded,
    KopisError,
    build_event_record,
    build_venue_index,
    hall_seat_scale,
    kopis_date,
    max_hall_seats,
    needs_detail,
    parse_db_list,
)
from src.data.collectors.weather_collector import ASOS_COLUMNS, parse_asos, parse_warnings

KOPIS_LIST_XML = """<?xml version="1.0" encoding="UTF-8"?>
<dbs>
  <db>
    <mt20id>PF000001</mt20id>
    <prfnm>테스트 콘서트</prfnm>
    <prfpdfrom>2026.08.15</prfpdfrom>
    <prfpdto>2026.08.16</prfpdto>
    <fcltynm>잠실종합운동장</fcltynm>
    <area>서울특별시</area>
    <genrenm>대중음악</genrenm>
    <openrun>N</openrun>
    <prfstate>공연완료</prfstate>
  </db>
  <db>
    <mt20id>PF000002</mt20id>
    <prfnm>테스트 뮤지컬</prfnm>
    <prfpdfrom>2026.07.01</prfpdfrom>
    <prfpdto>2026.09.30</prfpdto>
    <fcltynm>샤롯데씨어터</fcltynm>
    <genrenm>뮤지컬</genrenm>
  </db>
</dbs>"""

KOPIS_DETAIL_XML = """<dbs><db>
  <mt20id>PF000001</mt20id>
  <mt10id>FC000001</mt10id>
  <prfnm>테스트 콘서트</prfnm>
  <fcltynm>서울종합운동장 (주경기장)</fcltynm>
  <dtguidance>토요일(18:00), 일요일(17:00)</dtguidance>
  <prfruntime>2시간</prfruntime>
  <styurls><styurl>http://example.com/a.jpg</styurl></styurls>
</db></dbs>"""

KOPIS_FACILITY_XML = """<dbs><db>
  <fcltynm>서울종합운동장</fcltynm>
  <mt10id>FC000001</mt10id>
  <seatscale>69,950</seatscale>
  <adres>서울특별시 송파구 올림픽로 25</adres>
  <la>37.5154</la>
  <lo>127.0728</lo>
  <mt13s>
    <mt13><prfplcnm>주경기장</prfplcnm><mt13id>FC000001-01</mt13id><seatscale>69950</seatscale></mt13>
    <mt13><prfplcnm>보조경기장</prfplcnm><mt13id>FC000001-02</mt13id><seatscale>3000</seatscale></mt13>
  </mt13s>
</db></dbs>"""

KOPIS_ERROR_XML = """<dbs><db><returncode>02</returncode><errmsg>SERVICE KEY IS NOT REGISTERED</errmsg></db></dbs>"""


def _asos_line(tm: str, ta: str, rn: str, hm: str) -> str:
    values = {c: "0.0" for c in ASOS_COLUMNS}
    values.update(YYMMDDHHMI=tm, STN="108", TA=ta, RN=rn, HM=hm, WW="-")
    return " ".join(values[c] for c in ASOS_COLUMNS)


ASOS_TEXT = "\n".join([
    "#START7777",
    "#" + "-" * 40,
    "#  " + " ".join(["YYMMDDHHMI", "STN", "WD", "WS", "GST", "GST", "GST"]) + " ...",
    "#" + "-" * 40,
    _asos_line("202608152000", "28.4", "-9.0", "81.0"),
    _asos_line("202608152100", "27.9", "3.5", "-9"),
    "#7777END",
])


class CommonTest(unittest.TestCase):
    def test_parse_date(self):
        self.assertEqual(parse_date("2026-08-15"), date(2026, 8, 15))
        self.assertEqual(parse_date("20260815"), date(2026, 8, 15))

    def test_date_chunks_cover_range_without_gap(self):
        chunks = list(date_chunks(date(2025, 11, 1), date(2026, 2, 10), 31))
        self.assertEqual(chunks[0][0], date(2025, 11, 1))
        self.assertEqual(chunks[-1][1], date(2026, 2, 10))
        for (_, a_end), (b_start, _) in zip(chunks, chunks[1:]):
            self.assertEqual((b_start - a_end).days, 1)
        for s, e in chunks:
            self.assertLessEqual((e - s).days + 1, 31)

    def test_redact_hides_keys(self):
        msg = "404 for url: http://x/api?service=AB%2BC&authKey=AB+C&stdate=20260801"
        out = redact(msg, {"service": "AB+C", "authKey": "AB+C", "stdate": "20260801"})
        self.assertNotIn("AB", out)
        self.assertIn("stdate=20260801", out)

    def test_date_chunks_rejects_reversed_range(self):
        with self.assertRaises(ValueError):
            list(date_chunks(date(2026, 2, 1), date(2026, 1, 1), 31))


class KopisTest(unittest.TestCase):
    def test_parse_list(self):
        items = parse_db_list(KOPIS_LIST_XML)
        self.assertEqual([i["mt20id"] for i in items], ["PF000001", "PF000002"])
        self.assertEqual(items[0]["prfnm"], "테스트 콘서트")

    def test_parse_error_response(self):
        with self.assertRaises(KopisError):
            parse_db_list(KOPIS_ERROR_XML)

    def test_parse_broken_xml(self):
        with self.assertRaises(KopisError):
            parse_db_list("<html>Service Unavailable")

    def test_kopis_date(self):
        self.assertEqual(kopis_date("2026.08.15"), "2026-08-15")
        self.assertIsNone(kopis_date(""))

    def test_nested_tags_become_lists(self):
        detail = parse_db_list(KOPIS_DETAIL_XML)[0]
        self.assertEqual(detail["styurls"], ["http://example.com/a.jpg"])
        facility = parse_db_list(KOPIS_FACILITY_XML)[0]
        self.assertEqual(len(facility["mt13s"]), 2)

    def test_hall_seat_scale(self):
        facility = parse_db_list(KOPIS_FACILITY_XML)[0]
        # 1순위: 홀 ID
        self.assertEqual(hall_seat_scale({"mt13id": "FC000001-02", "fcltynm": "엉뚱한 이름"}, facility), 3000)
        # 2순위: 이름 포함
        self.assertEqual(hall_seat_scale({"fcltynm": "서울종합운동장 (보조경기장)"}, facility), 3000)
        self.assertIsNone(hall_seat_scale({"fcltynm": "서울종합운동장"}, facility))

    def test_hall_seat_scale_nested_parentheses(self):
        # 실제 응답에서 이름 비교가 실패했던 형태
        facility = {"mt13s": [{"prfplcnm": "아틀리에홀 (B1)", "mt13id": "FC1-01", "seatscale": "120"}]}
        self.assertEqual(hall_seat_scale({"fcltynm": "아틀리에홀 (아틀리에홀 (B1) )"}, facility), 120)

    def test_build_event_record(self):
        item = parse_db_list(KOPIS_LIST_XML)[0]
        detail = parse_db_list(KOPIS_DETAIL_XML)[0]
        facility = parse_db_list(KOPIS_FACILITY_XML)[0]
        rec = build_event_record(item, detail, facility)
        self.assertEqual(rec["event_id"], "PF000001")
        self.assertEqual(rec["period_from"], "2026-08-15")
        self.assertAlmostEqual(rec["venue_lat"], 37.5154)
        self.assertAlmostEqual(rec["venue_lon"], 127.0728)
        self.assertEqual(rec["seat_scale"], 69950)
        self.assertEqual(rec["hall_seat_scale"], 69950)
        self.assertEqual(rec["time_guidance"], "토요일(18:00), 일요일(17:00)")

    def test_max_hall_seats(self):
        facility = parse_db_list(KOPIS_FACILITY_XML)[0]
        self.assertEqual(max_hall_seats(facility), 69950)
        self.assertEqual(max_hall_seats({"seatscale": "300"}), 300)  # 홀 목록 없으면 시설 전체
        self.assertIsNone(max_hall_seats(None))

    def test_needs_detail(self):
        small = {"mt13s": [{"seatscale": "120"}, {"seatscale": "80"}]}
        big = {"mt13s": [{"seatscale": "120"}, {"seatscale": "2500"}]}
        self.assertTrue(needs_detail(False, None, 1000))   # 시설 모름
        self.assertFalse(needs_detail(True, small, 1000))  # 가장 큰 홀이 기준 미만
        self.assertTrue(needs_detail(True, big, 1000))
        self.assertTrue(needs_detail(True, {}, 1000))      # 객석 수 모름
        self.assertTrue(needs_detail(True, small, 0))      # 기준 0이면 전부

    def test_build_venue_index_skips_duplicate_names(self):
        fac_list = [
            {"fcltynm": "A홀", "mt10id": "FC1"},
            {"fcltynm": "B홀", "mt10id": "FC2"},
            {"fcltynm": "B홀", "mt10id": "FC3"},
        ]
        self.assertEqual(build_venue_index(fac_list), {"A홀": "FC1"})

    def test_call_budget(self):
        b = CallBudget(2)
        b.spend(); b.spend()
        with self.assertRaises(CallBudgetExceeded):
            b.spend()
        unlimited = CallBudget(0)
        for _ in range(5):
            unlimited.spend()

    def test_blocked_response_becomes_kopis_blocked(self):
        from unittest import mock
        from src.data.collectors import kopis_collector as kc
        err = RuntimeError("요청 실패: ... 400 Client Error\n응답 본문: <H1>Request Blocked</H1>")
        with mock.patch.object(kc, "get_with_retry", side_effect=err), mock.patch.object(kc, "CALL_INTERVAL", 0):
            with self.assertRaises(kc.KopisBlocked):
                kc._get("KEY", "prfplc/FC1", CallBudget(0))
        other = RuntimeError("요청 실패: 500 Server Error")
        with mock.patch.object(kc, "get_with_retry", side_effect=other):
            with self.assertRaises(RuntimeError) as ctx:
                kc._get("KEY", "prfplc/FC1", CallBudget(0))
            self.assertNotIsInstance(ctx.exception, kc.KopisBlocked)

    def test_build_event_record_facility_only(self):
        # 상세 없이 시설 정보만 붙는 경우 (작은 공연장)
        item = parse_db_list(KOPIS_LIST_XML)[1]
        facility = {"mt10id": "FC9", "la": "37.5", "lo": "127.0", "seatscale": "300",
                    "mt13s": [{"prfplcnm": "소극장", "mt13id": "FC9-01", "seatscale": "300"}]}
        rec = build_event_record(item, None, facility, "FC9")
        self.assertEqual(rec["facility_id"], "FC9")
        self.assertEqual(rec["hall_seat_scale"], 300)  # 홀이 하나뿐이라 확정
        self.assertEqual(rec["max_hall_seat_scale"], 300)
        self.assertFalse(rec["detail_fetched"])
        self.assertIsNone(rec["time_guidance"])

    def test_build_event_record_without_detail(self):
        item = parse_db_list(KOPIS_LIST_XML)[1]
        rec = build_event_record(item, None, None)
        self.assertEqual(rec["name"], "테스트 뮤지컬")
        self.assertIsNone(rec["venue_lat"])
        self.assertIsNone(rec["seat_scale"])


class WeatherTest(unittest.TestCase):
    def test_parse_asos(self):
        rows = parse_asos(ASOS_TEXT)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["datetime"], "2026-08-15 20:00")
        self.assertEqual(rows[0]["stn"], "108")
        self.assertEqual(rows[0]["ta"], 28.4)
        self.assertIsNone(rows[0]["rn"])  # -9.0 → None
        self.assertEqual(rows[1]["rn"], 3.5)
        self.assertIsNone(rows[1]["hm"])  # -9 → None
        self.assertEqual(rows[0]["ww"], "-")

    def test_parse_asos_wrong_column_count(self):
        with self.assertRaises(ValueError):
            parse_asos("202608152000 108 1.0 2.0")

    def test_parse_asos_real_line(self):
        # 2026-08 실제 응답에서 가져온 줄 (46열)
        text = "\n".join([
            "#START7777",
            "# YYMMDDHHMI STN  WD   WS GST  GST  GST     PA     PS PT    PR    TA",
            "202608010300 108  25  1.7  -9 -9.0   -9  997.3 1006.9  6  -0.1  28.0  25.2  85.0  32.1"
            "   -9.0   -9.0   -9.0   -9.0   -9.0   -9.0   -9.0 10  0 19                       4   4"
            "    4 -         -9  -9  -9   890 -9.0 -9.00 -9  27.3  28.5  28.7  28.9  28.7  -9 -9.0 -9 3 2",
            "#7777END",
        ])
        row = parse_asos(text)[0]
        self.assertEqual(row["datetime"], "2026-08-01 03:00")
        self.assertEqual((row["ta"], row["hm"], row["ws"]), (28.0, 85.0, 1.7))
        self.assertIsNone(row["rn"])
        self.assertEqual(row["ir"], 3.0)

    def test_parse_warnings_real_format(self):
        # 2026-08 실제 응답 형식: 쉼표 CSV + 줄 끝 '='
        text = "\n".join([
            "#START7777",
            "#      TM_FC,        TM_EF,        TM_IN, STN,   REG_ID, WRN, LVL, CMD, GRD, CNT,   RPT, =",
            "202608011000, 202608011100, 202608010901, 156, L1050700,   H,   3,   6,  00,   4,   101, =",
            "202608312300, 202608312300, 202608312252, 109, L1100200,   R,   2,   3,  00,   4,   101, =",
            "#7777END",
        ])
        rows = parse_warnings(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["tm_fc"], "2026-08-01 10:00")
        self.assertEqual((rows[0]["wrn_name"], rows[0]["lvl_name"], rows[0]["cmd_name"]), ("폭염", "경보", "변경"))
        self.assertFalse(rows[0]["is_seoul"])
        self.assertEqual((rows[1]["wrn_name"], rows[1]["lvl_name"], rows[1]["cmd_name"]), ("호우", "주의보", "해제"))
        self.assertTrue(rows[1]["is_seoul"])

    def test_parse_warnings_wrong_column_count(self):
        with self.assertRaises(ValueError):
            parse_warnings("202608011000, 202608011100, =")


if __name__ == "__main__":
    unittest.main()
