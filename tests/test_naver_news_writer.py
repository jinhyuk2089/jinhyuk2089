"""naver_news_writer 단위·통합 테스트: 네이버 API와 기사 페이지를 로컬 서버로 흉내 낸다."""
import email.utils
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import naver_news_writer as nw  # noqa: E402

TITLES = [
    "동두천시, 노인맞춤돌봄서비스 대상자 확대",
    "노인맞춤돌봄서비스 대상자 확대… 동두천 어르신 혜택",
    "동두천 노인맞춤돌봄서비스 대상자 확대에 신청 급증",
    "경기도 노인 고독사 예방 사업 강화",
    "노인 고독사 예방, 경기도 사업 강화한다",
    "돌봄 인력 부족 심각… 처우 개선 요구",
    "사회복지사 처우 개선 예산 확대",
    "동두천 복지관 디지털 교육 인기",
    "어르신 디지털 문해교육 수요 늘어",
    "장기요양등급 신청 절차 간소화",
    "노인일자리 내년 모집 시작",
    "지역사회 통합돌봄 시범사업 확대",
    "기초연금 수급자 증가",
    "가족돌봄 청년 지원 강화",
]
BROKEN = {12}          # 404 페이지
MISMATCH = {13}        # 원문 제목이 전혀 다름


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == "/v1/search/news.json":
            if self.headers.get("X-Naver-Client-Id") != "id":
                self.send_response(401); self.end_headers(); return
            q = parse_qs(u.query)
            sort = q["sort"][0]
            now = datetime.now(timezone(timedelta(hours=9)))
            order = list(range(len(TITLES))) if sort == "sim" else list(reversed(range(len(TITLES))))
            items = [{
                "title": f"<b>{TITLES[i]}</b>",
                "originallink": f"http://127.0.0.1:{self.server.server_port}/article/{i}",
                "link": f"http://127.0.0.1:{self.server.server_port}/article/{i}",
                "description": f"{TITLES[i]}에 관한 &quot;요약&quot; 내용",
                "pubDate": email.utils.format_datetime(now - timedelta(days=i % 5)),
            } for i in order]
            body = json.dumps({"items": items}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
            return
        if u.path.startswith("/article/"):
            i = int(u.path.rsplit("/", 1)[1])
            if i in BROKEN:
                self.send_response(404); self.end_headers(); return
            title = "전혀 다른 연예 기사 제목입니다" if i in MISMATCH else TITLES[i]
            press = f"언론사{i % 7}"
            page = (f'<html><head><meta property="og:title" content="{title} - {press}">'
                    f'<meta property="og:site_name" content="{press}">'
                    f'<meta property="article:published_time" content="2026-10-1{i % 6}T09:00:00+09:00">'
                    f"<title>{title}</title></head><body>본문</body></html>").encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(page)
            return
        self.send_response(404); self.end_headers()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.env = mock.patch.dict(os.environ, {"NAVER_CLIENT_ID": "id", "NAVER_CLIENT_SECRET": "s",
                                               "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"})
        cls.env.start()
        cls.basep = mock.patch.object(nw, "NAVER_BASE", cls.base)
        cls.basep.start()

    @classmethod
    def tearDownClass(cls):
        cls.basep.stop(); cls.env.stop(); cls.srv.shutdown()


class UnitTests(unittest.TestCase):
    def test_clean_and_tokens(self):
        self.assertEqual(nw.clean("<b>동두천</b> &quot;복지&quot;"), '동두천 "복지"')
        self.assertIn("동두천", nw.tokens("동두천에서 복지를"))

    def test_parse_keywords(self):
        self.assertEqual(nw.parse_keywords(["노인복지:노인맞춤돌봄", "동두천 복지", "x: "]),
                         [("노인복지", "노인맞춤돌봄"), ("", "동두천 복지")])

    def test_source_type(self):
        self.assertEqual(nw.source_type("https://www.mohw.go.kr/x"), "공공·기관")
        self.assertEqual(nw.source_type("https://blog.naver.com/a"), "블로그")
        self.assertEqual(nw.source_type("https://www.hani.co.kr/a"), "언론")

    def test_cluster_groups_similar_titles(self):
        rows = [{"title": t, "press": f"p{i}"} for i, t in enumerate(TITLES[:3] + [TITLES[9]])]
        nw.cluster(rows)
        self.assertEqual(rows[0]["issue_id"], rows[1]["issue_id"])
        self.assertNotEqual(rows[0]["issue_id"], rows[3]["issue_id"])


class EndToEnd(Base):
    def run_main(self, *extra):
        out = tempfile.mkdtemp()
        links = Path(out) / "links.txt"
        links.write_text(f"# 주석\n{self.base}/article/7 | 디지털복지 | 직접 확인한 기사\nnot-a-url\n", encoding="utf-8")
        code = nw.main(["-k", "노인복지:노인맞춤돌봄서비스", "-k", "동두천 복지", "--per-sort", "14", "--out", out,
                        "--links", str(links), "--note", "복지관에서 신청 문의가 늘었다", *extra])
        return code, Path(out)

    def test_collect_verify_report(self):
        code, out = self.run_main("--no-write")
        self.assertEqual(code, 0)
        data = json.loads((out / "sources.json").read_text(encoding="utf-8"))
        urls = [s["url"] for s in data["sources"]]
        self.assertEqual(len(urls), len(set(urls)), "중복 제거")
        self.assertEqual(len(data["sources"]), len(TITLES) - 2, "404·제목 불일치 기사 제외")
        self.assertFalse(any(u.endswith(("/article/12", "/article/13")) for u in urls))
        self.assertEqual(data["dropped"], 2)
        flags = {f for s in data["sources"] for f in s["flags"]}
        self.assertTrue({"상위노출", "이슈", "픽"} <= flags)
        art7 = next(s for s in data["sources"] if s["url"].endswith("/article/7"))
        self.assertIn("디지털복지", art7["fields"])
        self.assertTrue(all("<b>" not in s["title"] for s in data["sources"]))
        self.assertIn("자료 검증 정리표", (out / "report.md").read_text(encoding="utf-8"))

    def test_writes_with_citations_prompt(self):
        seen = {}
        def fake(prompt):
            seen["p"] = prompt
            return "# 기사\n본문 [1]"
        with mock.patch.object(nw, "write_with_claude", fake):
            code, out = self.run_main("--mode", "article")
        self.assertEqual(code, 0)
        self.assertIn("[12]", seen["p"])
        self.assertIn("복지관에서 신청 문의가 늘었다", seen["p"])
        self.assertEqual((out / "article.md").read_text(encoding="utf-8"), "# 기사\n본문 [1]")

    def test_gate_below_minimum(self):
        with mock.patch.object(nw, "write_with_claude", side_effect=AssertionError("호출되면 안 됨")):
            code, _ = self.run_main("--min-verified", "50")
        self.assertEqual(code, 3)

    def test_bad_credentials(self):
        with mock.patch.dict(os.environ, {"NAVER_CLIENT_ID": "wrong"}):
            code, _ = self.run_main("--no-write")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
