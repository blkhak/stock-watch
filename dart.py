"""OpenDART(전자공시) 조회. API 키는 https://opendart.fss.or.kr 에서 무료로 발급."""
import io
import json
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta, timezone

API = "https://opendart.fss.or.kr/api/"
KST = timezone(timedelta(hours=9))


def _get(path, key, **params):
    url = API + path + "?" + urllib.parse.urlencode({"crtfc_key": key, **params})
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def corp_codes(key, stock_codes):
    """종목코드(6자리) → DART 고유번호(8자리). 전체 목록 zip을 받으므로 결과는 호출 측에서 캐시할 것."""
    raw = _get("corpCode.xml", key)
    if not raw.startswith(b"PK"):   # 키가 틀리면 zip 대신 오류 메시지가 온다
        raise RuntimeError(f"DART 고유번호 조회 실패: {raw[:200].decode('utf-8', 'replace')}")
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        root = ET.fromstring(z.read(z.namelist()[0]))
    want = set(stock_codes)
    return {e.findtext("stock_code").strip(): e.findtext("corp_code")
            for e in root.iter("list") if (e.findtext("stock_code") or "").strip() in want}


def filings(key, corp_code, days=3):
    """최근 days일 공시 목록 (최신순). 항목: report_nm, rcept_no, flr_nm, rcept_dt 등."""
    today = datetime.now(KST)
    res = json.loads(_get("list.json", key, corp_code=corp_code, page_count=100,
                          bgn_de=f"{today - timedelta(days=days):%Y%m%d}", end_de=f"{today:%Y%m%d}"))
    if res["status"] == "013":   # 조회된 데이터 없음
        return []
    if res["status"] != "000":
        raise RuntimeError(f"DART 오류 {res['status']}: {res.get('message')}")
    return res["list"]


def filing_url(rcept_no):
    return f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
