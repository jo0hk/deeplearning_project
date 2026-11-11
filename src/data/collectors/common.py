"""수집기 공통 유틸: .env 로드, 기간 분할, HTTP 재시도, 원본 저장."""

from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = PROJECT_ROOT / "data" / "raw"


def load_env(path: Path | None = None) -> None:
    """프로젝트 루트의 .env를 읽어 os.environ에 채운다. 이미 있는 값은 덮어쓰지 않는다."""
    path = path or PROJECT_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():  # 메모장 BOM 대비
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def require_env(name: str) -> str:
    load_env()
    value = os.environ.get(name, "")
    if not value or value == "your_key_here":
        raise RuntimeError(f"환경변수 {name}가 비어 있습니다. .env.example을 복사해 .env에 키를 넣으세요.")
    return value


def parse_date(text: str) -> date:
    """'2026-01-31' 또는 '20260131' 형식을 date로 바꾼다."""
    text = text.replace("-", "")
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def date_chunks(start: date, end: date, max_days: int) -> Iterator[tuple[date, date]]:
    """[start, end] 구간을 최대 max_days일짜리 조각으로 나눈다 (양 끝 포함)."""
    if start > end:
        raise ValueError(f"시작일({start})이 종료일({end})보다 늦습니다.")
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=max_days - 1), end)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


def get_with_retry(
    url: str,
    params: dict | None = None,
    retries: int = 3,
    timeout: int = 30,
    sleep: float = 1.0,
    encoding: str | None = None,
) -> str:
    """GET 요청을 재시도하며 본문 텍스트를 돌려준다.

    실패 메시지에는 API 키를 가린 내용만 남긴다 (requests 오류 메시지에는 키가 든 전체 URL이 들어간다).
    """
    last_msg = ""
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            resp.raise_for_status()
            if encoding:
                resp.encoding = encoding
            return resp.text
        except requests.RequestException as err:
            body = err.response.text[:300].strip() if err.response is not None else ""
            last_msg = redact(f"{type(err).__name__}: {err}" + (f"\n응답 본문: {body}" if body else ""), params)
            status = err.response.status_code if err.response is not None else None
            if status is not None and 400 <= status < 500 and status != 429:
                break  # 키·권한 문제 등 클라이언트 오류는 재시도해도 같다
            if attempt < retries:
                time.sleep(sleep * attempt)
    raise RuntimeError(f"요청 실패: {url} — {last_msg}") from None


SECRET_PARAM_NAMES = {"service", "servicekey", "authkey", "key", "apikey"}


def redact(text: str, params: dict | None) -> str:
    """text 안에 들어간 키 값을 '***'로 바꾼다."""
    for name, value in (params or {}).items():
        if name.lower() in SECRET_PARAM_NAMES and value:
            text = text.replace(str(value), "***")
            text = text.replace(requests.utils.quote(str(value), safe=""), "***")
    return text


def save_json(obj, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def save_text(text: str, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
