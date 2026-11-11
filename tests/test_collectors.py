"""수집기 파싱 로직 테스트 (네트워크 없이 실행).

아래 샘플 응답은 API 문서의 형식을 바탕으로 직접 만든 것이다. 실제 응답을 받으면
tests/fixtures/ 에 저장하고 이 테스트를 실제 응답 기준으로 바꿀 것.

실행: python -m unittest discover -s tests -v
"""

import unittest
from datetime import date

from src.data.collectors.common import date_chunks, parse_date
from src.data.collectors.kopis_collector import (
    KopisError,
    build_event_record,
    hall_seat_scale,
    kopis_date,
    parse_db_list,
)
from src.data.collectors.weather_collector import ASOS_COLUMNS, parse_asos, parse_generic_table

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
        self.assertEqual(hall_seat_scale("서울종합운동장 (보조경기장)", facility), 3000)
        self.assertIsNone(hall_seat_scale("서울종합운동장", facility))

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

    def test_parse_generic_table(self):
        text = "\n".join([
            "# TM_FC REG_ID WRN LVL",
            "202607140600 L1100100 H 1",
            "202607141800 L1100100 H 2 extra",
        ])
        rows = parse_generic_table(text)
        self.assertEqual(rows[0], {"tm_fc": "202607140600", "reg_id": "L1100100", "wrn": "H", "lvl": "1"})
        self.assertEqual(rows[1], {"raw": "202607141800 L1100100 H 2 extra"})


if __name__ == "__main__":
    unittest.main()
