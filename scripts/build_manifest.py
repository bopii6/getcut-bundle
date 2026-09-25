#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 fetch_assets.py 的回执（registry.json）转成 manifest.json，顺序跟口播推进走。

用法：
    python3 build_manifest.py <素材包目录> [--generated-by 名字]

规则：图片一律不写选段时间；视频已由 fetch_assets.py 裁成单镜头，同样不写。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MAX_SUMMARY, MAX_TAG, MAX_ANCHOR = 240, 32, 240


def clean(s: str, limit: int) -> str:
    return " ".join(str(s).split())[:limit]


def main() -> int:
    ap = argparse.ArgumentParser(description="registry.json → manifest.json")
    ap.add_argument("root", help="素材包目录")
    ap.add_argument("--generated-by", default="getcut-bundle/fetch_assets")
    a = ap.parse_args()

    root = Path(a.root).resolve()
    reg = root / "registry.json"
    if not reg.is_file():
        print(f"缺回执文件：{reg}（先跑 fetch_assets.py）")
        return 2
    rows = json.loads(reg.read_text(encoding="utf-8"))
    assets, skipped = [], []
    for r in sorted(rows, key=lambda x: str(x.get("num", ""))):
        rel = r.get("file", "")
        if not (root / rel).is_file():
            skipped.append(rel or "(空回执)")
            continue
        kind = r.get("kind", "image")
        tags = [clean(t, MAX_TAG) for t in (r.get("tags") or [])][:8]
        if len(tags) < 2:
            tags = (tags + ["素材", "资料"])[:5]
        entry = {
            "file": rel,
            "kind": kind,
            "summary": clean(r.get("summary", ""), MAX_SUMMARY),
            "tags": tags[:5],
        }
        if r.get("source_url"):
            entry["source_url"] = r["source_url"]
        if r.get("anchor"):
            entry["script_anchor"] = clean(r["anchor"], MAX_ANCHOR)
        if r.get("license"):
            entry["license"] = clean(r["license"], 64)
        if kind == "video" and r.get("clip_start_seconds") is not None:
            entry["clip_start_seconds"] = r["clip_start_seconds"]
            entry["clip_end_seconds"] = r["clip_end_seconds"]
        assets.append(entry)

    n_img = sum(1 for x in assets if x["kind"] == "image")
    n_vid = len(assets) - n_img
    man = {
        "schema_version": 1,
        "generated_by": clean(a.generated_by, 200),
        "notes": (f"由 slots.json 槽位表并发抓取：{n_img} 张图 + {n_vid} 段视频，"
                  "顺序按口播推进排布；视频已裁成单镜头，无需再填选段时间。"),
        "assets": assets,
    }
    out = root / "manifest.json"
    out.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"manifest.json：{len(assets)} 条（图 {n_img} / 视频 {n_vid}）")
    if skipped:
        print("跳过的空回执：" + ", ".join(skipped[:8]))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.exit(main())
