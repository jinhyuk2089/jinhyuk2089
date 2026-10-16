#!/usr/bin/env python3
"""참여 도구를 브라우저에서 바로 여는 단일 HTML 파일로 만든다.

campaign/dongducheon-1day1post.html 은 claude.ai에 게시할 때 문서 틀이 자동으로 씌워지는
본문 조각이다. 이 스크립트는 같은 틀을 직접 씌워 오프라인·파일 공유용 완성본을 만든다.
"""
from pathlib import Path

root = Path(__file__).resolve().parents[1]
src = (root / "campaign/dongducheon-1day1post.html").read_text(encoding="utf-8")
head = ('<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<style>:root{padding-top:env(safe-area-inset-top,0px);padding-bottom:env(safe-area-inset-bottom,0px)}'
        'body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style></head><body>\n')
out = root / "campaign/dongducheon-1day1post-standalone.html"
out.write_text(head + src + "\n</body></html>\n", encoding="utf-8")
print(out.relative_to(root), f"{out.stat().st_size // 1024}KB")
