#!/usr/bin/env python3
"""네이버 뉴스 자동 수집·검증·종합 글쓰기 프로그램.

키워드별로 네이버 검색 API(뉴스)에서 정확도순(상위)과 최신순 기사를 모으고,
링크를 하나씩 열어 검증한 뒤, 여러 언론사가 함께 다룬 이슈를 묶어 점수를 매깁니다.
검증된 자료가 기준(기본 10개) 이상이면 Claude가 출처 번호를 단 기사·블로그 글을 씁니다.

필요한 환경변수
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET   네이버 개발자센터에서 발급 (검색 API)
  ANTHROPIC_API_KEY                      글쓰기 단계에서만 필요 (--no-write면 불필요)

예시
  python tools/naver_news_writer.py -k "노인복지:노인맞춤돌봄서비스" -k "동두천 복지" \
      --links links.txt --note "복지관 현장에서 신청 문의가 늘었다" --mode both
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import time
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests

NAVER_BASE = os.environ.get("NAVER_API_BASE", "https://openapi.naver.com")
MODEL = "claude-opus-5-5"
UA = "Mozilla/5.0 (compatible; DDC-1day1post-collector/1.0; +https://github.com/jinhyuk2089)"
KST = dt.timezone(dt.timedelta(hours=9))

# 조사·어미를 대략 떼어 내 제목 비교에 쓰는 단어만 남긴다
_PARTICLES = re.compile(r"(은|는|이|가|을|를|의|에|에서|으로|로|와|과|도|만|까지|부터|이다|한다|했다|된다)$")
_TAG = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[가-힣A-Za-z0-9]{2,}")

OFFICIAL_SUFFIXES = (".go.kr", ".or.kr", ".re.kr", ".ac.kr", "korea.kr")


# ---------------------------------------------------------------- 유틸
def clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub("", text or ""))).strip()


def tokens(text: str) -> set[str]:
    out = set()
    for w in _WORD.findall(clean(text)):
        w = _PARTICLES.sub("", w)
        if len(w) >= 2:
            out.add(w)
    return out


def norm_url(url: str) -> str:
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    host = p.netloc.lower().removeprefix("www.").removeprefix("m.")
    return urlunsplit(("https", host, p.path.rstrip("/"), p.query, ""))


def domain(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower().removeprefix("www.").removeprefix("m.")
    except ValueError:
        return ""


def source_type(url: str) -> str:
    d = domain(url)
    if any(d.endswith(s) for s in OFFICIAL_SUFFIXES):
        return "공공·기관"
    if "blog" in d or "tistory" in d or "brunch" in d:
        return "블로그"
    return "언론"


def parse_pubdate(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=KST)
    return d.astimezone(KST)


# ---------------------------------------------------------------- 1. 수집
class NaverError(RuntimeError):
    pass


def naver_search(query: str, sort: str, display: int, session: requests.Session) -> list[dict]:
    cid, secret = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    if not cid or not secret:
        raise NaverError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수가 없습니다. 네이버 개발자센터에서 검색 API를 신청하세요.")
    r = session.get(
        f"{NAVER_BASE}/v1/search/news.json",
        params={"query": query, "display": max(1, min(display, 100)), "start": 1, "sort": sort},
        headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret},
        timeout=15,
    )
    if r.status_code == 401:
        raise NaverError("네이버 API 인증에 실패했습니다. Client ID와 Secret, 검색 API 사용 설정을 확인하세요.")
    if r.status_code == 429:
        raise NaverError("네이버 API 하루 호출 한도를 넘었습니다. 내일 다시 실행하세요.")
    if r.status_code != 200:
        raise NaverError(f"네이버 API 오류 {r.status_code}: {r.text[:200]}")
    return r.json().get("items", [])


def collect(keywords: list[tuple[str, str]], per_sort: int, days: int, session: requests.Session) -> list[dict]:
    """키워드마다 정확도순·최신순을 모아 URL 기준으로 합친다."""
    since = dt.datetime.now(KST) - dt.timedelta(days=days)
    merged: dict[str, dict] = {}
    for field, kw in keywords:
        for sort in ("sim", "date"):
            for rank, it in enumerate(naver_search(kw, sort, per_sort, session), start=1):
                url = it.get("originallink") or it.get("link") or ""
                if not url:
                    continue
                pub = parse_pubdate(it.get("pubDate", ""))
                if pub and pub < since:
                    continue
                key = norm_url(url)
                row = merged.get(key)
                if row is None:
                    row = merged[key] = {
                        "title": clean(it.get("title", "")),
                        "summary": clean(it.get("description", "")),
                        "url": url,
                        "naver_link": it.get("link", "") if "naver.com" in it.get("link", "") else "",
                        "press": domain(url),
                        "type": source_type(url),
                        "date": pub.isoformat() if pub else "",
                        "keywords": [],
                        "fields": [],
                        "sim_rank": None,
                        "date_rank": None,
                        "origin": "네이버 뉴스 검색",
                    }
                if kw not in row["keywords"]:
                    row["keywords"].append(kw)
                if field and field not in row["fields"]:
                    row["fields"].append(field)
                rk = f"{sort}_rank"
                if row[rk] is None or rank < row[rk]:
                    row[rk] = rank
            time.sleep(0.15)
    return list(merged.values())


def load_links(path: str | None) -> list[dict]:
    """사용자가 추가한 링크 파일: 한 줄에 'URL | 분야 | 메모' (분야·메모는 선택)."""
    if not path:
        return []
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        url = parts[0]
        if not re.match(r"^https?://", url):
            continue
        rows.append({
            "title": "", "summary": parts[2] if len(parts) > 2 else "", "url": url, "naver_link": "",
            "press": domain(url), "type": source_type(url), "date": "", "keywords": [],
            "fields": [parts[1]] if len(parts) > 1 and parts[1] else ["추가 링크"],
            "sim_rank": None, "date_rank": None, "origin": "사용자 추가",
        })
    return rows


# ---------------------------------------------------------------- 2. 검증
_META = re.compile(r"<meta\s+[^>]*>", re.I)
_ATTR = re.compile(r'(\w[\w:-]*)\s*=\s*("([^"]*)"|\'([^\']*)\')')
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def page_meta(body: str) -> dict:
    meta = {}
    for tag in _META.findall(body[:200_000]):
        attrs = {m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(4)) for m in _ATTR.finditer(tag)}
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        if key and "content" in attrs and key not in meta:
            meta[key] = html.unescape(attrs["content"]).strip()
    t = _TITLE.search(body[:200_000])
    if t:
        meta.setdefault("title", clean(t.group(1)))
    return meta


def similar(a: str, b: str) -> float:
    a, b = clean(a), clean(b)
    if not a or not b:
        return 0.0
    ta, tb = tokens(a), tokens(b)
    jac = len(ta & tb) / max(1, len(ta | tb))
    return max(SequenceMatcher(None, a, b).ratio(), jac, 1.0 if a in b or b in a else 0.0)


def verify(row: dict, session: requests.Session) -> dict:
    """링크를 실제로 열어 응답, 제목 일치, 발행일을 확인한다."""
    checks = []
    targets = [u for u in (row["url"], row.get("naver_link")) if u]
    resp = None
    for u in targets:
        try:
            r = session.get(u, headers={"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"}, timeout=12, allow_redirects=True)
        except requests.RequestException as e:
            checks.append(f"접속 실패: {u} ({type(e).__name__})")
            continue
        if r.status_code < 400:
            resp = r
            break
        checks.append(f"응답 {r.status_code}: {u}")
    if resp is None:
        row.update(verified=False, checks=checks + ["열 수 있는 링크 없음"])
        return row
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding
    meta = page_meta(resp.text)
    page_title = meta.get("og:title") or meta.get("twitter:title") or meta.get("title", "")
    checks.append(f"접속 성공 ({resp.status_code})")
    ok_title = True
    if row["title"]:
        s = similar(row["title"], page_title)
        ok_title = s >= 0.45
        checks.append(f"제목 일치도 {s:.2f}" + ("" if ok_title else " (낮음: 원문 확인 필요)"))
    else:
        row["title"] = clean(page_title)[:200]
        ok_title = bool(row["title"])
        checks.append("원문에서 제목 확인" if ok_title else "원문 제목을 찾지 못함")
    if not row["summary"]:
        row["summary"] = clean(meta.get("og:description") or meta.get("description", ""))[:300]
    pub = meta.get("article:published_time") or meta.get("og:article:published_time") or meta.get("pubdate") or meta.get("date")
    if not row["date"] and pub:
        d = parse_pubdate(pub)
        if d:
            row["date"] = d.isoformat()
    if meta.get("og:site_name"):
        row["press"] = meta["og:site_name"][:40]
    checks.append("발행일 " + (row["date"][:10] if row["date"] else "확인 못 함"))
    row.update(verified=ok_title, checks=checks, final_url=resp.url)
    return row


# ---------------------------------------------------------------- 3. 이슈 묶기와 점수
def cluster(rows: list[dict], threshold: float = 0.3) -> None:
    """제목이 비슷한 기사를 하나의 이슈로 묶는다(여러 언론이 다룬 이슈일수록 크다)."""
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    toks = [tokens(r["title"]) for r in rows]
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if toks[i] and toks[j] and len(toks[i] & toks[j]) / len(toks[i] | toks[j]) >= threshold:
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in range(len(rows)):
        groups.setdefault(find(i), []).append(i)
    for gid, (root, idx) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1])), start=1):
        outlets = len({rows[i]["press"] for i in idx})
        for i in idx:
            rows[i]["issue_id"] = gid
            rows[i]["issue_size"] = outlets


def score(rows: list[dict]) -> None:
    now = dt.datetime.now(KST)
    for r in rows:
        s = 0.0
        if r["sim_rank"]:
            s += max(0, 30 - r["sim_rank"]) * 1.5          # 정확도순 상위
        if r["date_rank"]:
            s += max(0, 30 - r["date_rank"]) * 0.6         # 최신순 상위
        s += min(r.get("issue_size", 1), 8) * 6             # 여러 언론이 다룬 이슈
        s += len(r["keywords"]) * 5                          # 여러 키워드에 걸친 기사
        if r["date"]:
            age = (now - dt.datetime.fromisoformat(r["date"])).days
            s += max(0, 14 - age)
        if r["type"] == "공공·기관":
            s += 12
        if r["origin"] == "사용자 추가":
            s += 15
        flags = []
        if r["sim_rank"] and r["sim_rank"] <= 10:
            flags.append("상위노출")
        if r.get("issue_size", 1) >= 3:
            flags.append("이슈")
        r["score"] = round(s, 1)
        r["flags"] = flags
    top = sorted(rows, key=lambda r: -r["score"])[:5]
    for r in top:
        r["flags"].append("픽")


# ---------------------------------------------------------------- 4. 글쓰기 (Claude)
WRITER_SYSTEM = (
    "너는 박진혁 교수의 논설국장·전문기자 문체로 사회복지·지역사회 분야 기사를 쓰는 편집자다. "
    "제공된 자료 목록과 작성자 메모만 근거로 쓴다. 자료에 없는 수치, 날짜, 인용, 기관 정보는 만들지 않고 "
    "필요하면 [확인 필요]로 표시한다. 기사 문장은 원문을 베끼지 않고 재구성한다. "
    "사실을 쓸 때마다 문장 끝에 [번호]로 출처를 단다. 마크다운으로 쓴다."
)


def build_prompt(sources: list[dict], notes: list[str], mode: str, length: int, keywords: list[tuple[str, str]]) -> str:
    lines = []
    for s in sources:
        lines.append(
            f"[{s['n']}] {s['title']} | {s['press']} | {s['date'][:10] or '날짜 미상'} | 분야: {', '.join(s['fields']) or '-'} | "
            f"이슈 규모: 언론사 {s.get('issue_size', 1)}곳 | 요약: {s['summary'][:280]} | {s['url']}"
        )
    note_block = "\n".join(f"- {n}" for n in notes) or "(없음)"
    kw = ", ".join(f"{f}:{k}" if f else k for f, k in keywords)
    want = {
        "article": "기획기사 1편",
        "blog": "네이버 블로그 글 1편",
        "both": "기획기사 1편과, 같은 자료로 만든 네이버 블로그 글 1편",
    }[mode]
    return f"""키워드(분야:키워드): {kw}
쓸 글: {want}, 각 공백 제외 약 {length}자

검증된 자료 {len(sources)}건:
{chr(10).join(lines)}

작성자 메모(현장 경험·별도 텍스트, 경험은 이 내용만 쓴다):
{note_block}

기획기사 구조:
# 제목 / ## 부제목
리드(무엇이 왜 지금 쟁점인지 3문장) → ## 핵심 쟁점 3가지(여러 언론이 함께 다룬 이슈 우선) → ## 분야별 정리(분야마다 소제목)
→ ## 현장에서 본 의미(작성자 메모가 있을 때만, 메모 내용으로) → ## 전문가 제언(정책·현장·주민 각각 1개)
→ ## 독자가 할 수 있는 일(체크리스트) → ## 자주 묻는 질문(Q/A 3쌍) → ## 참고자료(번호, 제목, 언론사, 날짜, 링크 표)

블로그 글 구조(요청된 경우):
제목 3안, 부제목, 한눈에 보는 요약 → 왜 지금 → 핵심 정리 → 현장 경험(메모가 있을 때만) → 이용 방법·체크리스트 → 자주 묻는 질문 → 참고자료 → 해시태그 10개
"""


def write_with_claude(prompt: str) -> str:
    import anthropic

    client = anthropic.Anthropic()
    try:
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=64000,
            system=WRITER_SYSTEM,
            output_config={"effort": "high"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError:
        raise SystemExit("Anthropic API 키를 확인하세요 (ANTHROPIC_API_KEY).")
    except anthropic.RateLimitError:
        raise SystemExit("Anthropic API 호출 한도에 걸렸습니다. 잠시 뒤 다시 실행하세요.")
    except anthropic.APIStatusError as e:
        raise SystemExit(f"Anthropic API 오류 {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise SystemExit("Anthropic API에 연결하지 못했습니다. 네트워크를 확인하세요.")
    if msg.stop_reason == "refusal":
        raise SystemExit("모델이 이 요청의 작성을 거절했습니다. 키워드와 메모를 확인하세요.")
    text = "".join(b.text for b in msg.content if b.type == "text")
    if msg.stop_reason == "max_tokens":
        text += "\n\n(분량 한도에 걸려 글이 중간에 끝났습니다. --length를 줄여 다시 실행하세요.)"
    return text


# ---------------------------------------------------------------- 5. 출력
def report_md(sources: list[dict], dropped: list[dict], keywords) -> str:
    out = ["# 자료 검증 정리표\n", f"- 생성: {dt.datetime.now(KST):%Y-%m-%d %H:%M} (KST)",
           f"- 키워드: {', '.join(f'{f}:{k}' if f else k for f, k in keywords)}",
           f"- 검증 통과 {len(sources)}건 / 제외 {len(dropped)}건\n",
           "| 번호 | 표시 | 제목 | 언론사·기관 | 날짜 | 분야 | 점수 | 검증 |", "|---|---|---|---|---|---|---|---|"]
    for s in sources:
        title = s["title"].replace("|", "/")
        out.append(f"| {s['n']} | {' '.join(s['flags'])} | [{title}]({s['url']}) | {s['press']} | {s['date'][:10]} | "
                   f"{', '.join(s['fields'])} | {s['score']} | {'; '.join(s['checks'][-2:])} |")
    issues: dict[int, list[dict]] = {}
    for s in sources:
        issues.setdefault(s["issue_id"], []).append(s)
    out.append("\n## 여러 언론이 함께 다룬 이슈\n")
    for _, group in sorted(issues.items(), key=lambda kv: -kv[1][0]["issue_size"]):
        if group[0]["issue_size"] < 2:
            continue
        out.append(f"- **{group[0]['title']}** · 언론사 {group[0]['issue_size']}곳 · 자료 " + ", ".join(f"[{g['n']}]" for g in group))
    if dropped:
        out.append("\n## 검증에서 제외한 자료\n")
        for d in dropped[:40]:
            out.append(f"- {d['title'] or d['url']} · {'; '.join(d.get('checks', []))}")
    return "\n".join(out) + "\n"


def parse_keywords(values: list[str]) -> list[tuple[str, str]]:
    out = []
    for v in values:
        field, kw = v.split(":", 1) if ":" in v else ("", v)
        if kw.strip():
            out.append((field.strip(), kw.strip()))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="네이버 뉴스를 수집·검증해 종합 기사와 블로그 글을 씁니다.")
    ap.add_argument("-k", "--keyword", action="append", required=True, help="'분야:키워드' 또는 '키워드'. 여러 번 쓸 수 있습니다.")
    ap.add_argument("--per-sort", type=int, default=30, help="키워드·정렬별로 가져올 기사 수 (최대 100, 기본 30)")
    ap.add_argument("--days", type=int, default=30, help="최근 며칠 이내 기사만 (기본 30)")
    ap.add_argument("--links", help="추가 링크 파일: 한 줄에 'URL | 분야 | 메모'")
    ap.add_argument("--note", action="append", default=[], help="현장 경험·별도 텍스트 (여러 번 가능)")
    ap.add_argument("--note-file", help="별도 텍스트 파일 (빈 줄로 구분된 문단마다 메모 1개)")
    ap.add_argument("--min-verified", type=int, default=10, help="글을 쓰기 위한 최소 검증 자료 수 (기본 10)")
    ap.add_argument("--max-sources", type=int, default=20, help="글에 쓸 최대 자료 수 (기본 20)")
    ap.add_argument("--mode", choices=["article", "blog", "both"], default="both")
    ap.add_argument("--length", type=int, default=2500, help="글 분량, 공백 제외 글자 수 (기본 2500)")
    ap.add_argument("--out", help="결과 폴더 (기본 news-output/날짜-시각)")
    ap.add_argument("--no-write", action="store_true", help="수집·검증만 하고 글은 쓰지 않음")
    ap.add_argument("--force", action="store_true", help="검증 자료가 기준보다 적어도 글을 씀")
    a = ap.parse_args(argv)

    keywords = parse_keywords(a.keyword)
    notes = [n.strip() for n in a.note if n.strip()]
    if a.note_file:
        notes += [p.strip() for p in re.split(r"\n\s*\n", Path(a.note_file).read_text(encoding="utf-8")) if p.strip()]
    out = Path(a.out or f"news-output/{dt.datetime.now(KST):%Y%m%d-%H%M}")
    out.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    print(f"1/4 수집: 키워드 {len(keywords)}개 × 정확도순·최신순 {a.per_sort}건")
    try:
        rows = collect(keywords, a.per_sort, a.days, session)
    except NaverError as e:
        print(f"오류: {e}", file=sys.stderr)
        return 2
    by_url = {norm_url(r["url"]): r for r in rows}
    for link in load_links(a.links):
        same = by_url.get(norm_url(link["url"]))
        if same is None:
            rows.append(link)
            by_url[norm_url(link["url"])] = link
            continue
        same["origin"] = "사용자 추가"   # 검색에도 나온 링크: 하나로 합치고 분야·메모를 보탠다
        same["fields"] += [f for f in link["fields"] if f not in same["fields"]]
        if link["summary"]:
            same["summary"] = f"{link['summary']} / {same['summary']}".strip(" /")
    print(f"   중복 제거 후 {len(rows)}건")

    print("2/4 검증: 링크를 하나씩 열어 확인합니다")
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        rows = list(ex.map(lambda r: verify(r, requests.Session()), rows))
    good = [r for r in rows if r.get("verified")]
    dropped = [r for r in rows if not r.get("verified")]
    print(f"   검증 통과 {len(good)}건, 제외 {len(dropped)}건")

    print("3/4 이슈 묶기와 점수 계산")
    cluster(good)
    score(good)
    good.sort(key=lambda r: -r["score"])
    picked = good[: a.max_sources]
    for i, r in enumerate(picked, start=1):
        r["n"] = i

    payload = {
        "generated": dt.datetime.now(KST).isoformat(timespec="seconds"),
        "keywords": [{"field": f, "keyword": k} for f, k in keywords],
        "notes": notes,
        "sources": [{k: r.get(k) for k in ("n", "title", "press", "url", "date", "type", "fields", "keywords", "summary",
                                            "flags", "score", "issue_id", "issue_size", "sim_rank", "date_rank", "origin", "checks")}
                    for r in picked],
        "dropped": len(dropped),
    }
    (out / "sources.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "report.md").write_text(report_md(picked, dropped, keywords), encoding="utf-8")
    print(f"   {out / 'sources.json'} (참여 도구 '뉴스 종합' 탭에서 불러오기)")
    print(f"   {out / 'report.md'}")

    if a.no_write:
        return 0
    if len(picked) < a.min_verified and not a.force:
        print(f"4/4 중단: 검증 자료가 {len(picked)}건으로 기준 {a.min_verified}건보다 적습니다. "
              "키워드를 늘리거나 --links로 자료를 추가하세요. (--force로 강제 작성 가능)", file=sys.stderr)
        return 3
    print(f"4/4 글쓰기: Claude가 자료 {len(picked)}건으로 작성합니다")
    text = write_with_claude(build_prompt(picked, notes, a.mode, a.length, keywords))
    (out / "article.md").write_text(text, encoding="utf-8")
    print(f"   완료: {out / 'article.md'}  발행 전 사람이 사실과 출처를 확인하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
