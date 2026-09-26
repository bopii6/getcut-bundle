#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按「槽位表」并发抓取素材：一格一个空位，抓完自动验货去重，并出一张联络表供目检。

用法：
    python3 fetch_assets.py <素材包目录>                 # 全表并发抓
    python3 fetch_assets.py <素材包目录> --only 14,25    # 只补不合格的那几格
    python3 fetch_assets.py <素材包目录> --report        # 只打印当前填写状态

槽位表放在 <素材包目录>/slots.json：
{
 "assets": [
  {"num":"01","kind":"image","site":"pexels","queries":["programming code on screen"],
   "summary":"显示器上密排的程序代码","tags":["代码","屏幕"],
   "anchor":"你让它看一段代码"},
  {"num":"02","kind":"video","site":"mixkit","queries":["server|datacenter"],
   "summary":"机房成排机柜指示灯","tags":["机房"],
   "anchor":"在后台把代码库整个打包上传"},
  {"num":"03","kind":"image","site":"og","queries":["https://www.nsfocus.com.cn/"],
   "summary":"绿盟科技官网横幅","tags":["绿盟"],
   "anchor":"信通院和绿盟完成首轮核查"}
 ]
}

site 四种取法：
  pexels  图库 API（要 PEXELS_API_KEY，没 key 自动降级到 bing）——概念图主力
  mixkit  素材站分类页先建索引、再按关键词挑 ID——B-roll 主力
  og      抓该网页的 og:image / 首图——品牌、机构、产品真图
  wikimedia  Commons API 按词搜图，只收 CC/公有领域，license 随条目带回——概念图兜底主力
  bing    搜索引擎抓取（最后一道兜底，已被风控时整格填不上，抽象词极易搜成字面物）

每格会拿多个候选逐个验，不合格就换下一个；回执写 <素材包目录>/registry.json，
之后用 build_manifest.py 生成 manifest.json。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MIN_IMG_W, MIN_IMG_H, MIN_RATIO = 1000, 520, 1.25
IMG_BYTES_MIN, VID_BYTES_MIN = 60_000, 400_000
CUT_SECONDS = 9.0
MAX_CAND = 6
INDEX_TTL = 24 * 3600
MIXKIT_CATS = ("computer", "programming", "technology", "business", "work", "office",
               "city", "money", "internet", "data", "security", "science")
# 单 IP 对同一站点的礼貌上限：窗口内最多这么多次请求，超了就排队
THROTTLE = {"api.pexels.com": (20, 60.0), "cn.bing.com": (24, 60.0),
            "commons.wikimedia.org": (20, 60.0),
            "assets.mixkit.co": (8, 60.0), "other": (24, 60.0)}
# 水印图库与跑题高发域，直接不要
BAD_HOST = ("699pic", "nipic", "588ku", "51miz", "upsku", "huitu", "veer", "123rf",
            "shutterstock", "istockphoto", "gettyimages", "wallpaper", "soogif",
            "360buyimg", "autoimg.cn", "52desk", "nximg", "waizi", "39.net",
            "vpic_cover", "myqcloud.com/banner")

_LOCK = threading.Lock()
_MK_LOCK = threading.Lock()
_TALLY: dict[str, list[float]] = {}
_MK_CACHE: dict[str, list] = {}


def throttle(host: str) -> None:
    """按站点限速：窗口内打满就排队，别把用户的 IP 送进别人黑名单。"""
    cap, win = THROTTLE.get(host, THROTTLE["other"])
    while True:
        with _LOCK:
            now = time.time()
            hits = [t for t in _TALLY.get(host, []) if now - t < win]
            if len(hits) < cap:
                hits.append(now)
                _TALLY[host] = hits
                return
            wait = round(hits[0] + win - now, 2)
        time.sleep(max(0.2, min(wait, 5.0)))


def http_get(url: str, timeout: int = 40, headers: dict | None = None) -> bytes:
    throttle(urllib.parse.urlparse(url).netloc)
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    h.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return r.read()


def img_dims(data: bytes) -> tuple[int, int, str] | None:
    """认魔数拿真实宽高，顺带挡掉扩展名骗人、0 字节、html 冒充图片。"""
    if data[:3] == b"\xff\xd8\xff":
        i = 2
        while i < len(data) - 9:
            if data[i] != 0xFF:
                i += 1
                continue
            mk = data[i + 1]
            if mk in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB,
                      0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h, "jpg"
            if mk in (0xD8, 0x01, 0xDD):
                break
            i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", data[16:24])
        return w, h, "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if data[12:16] == b"VP8X":
            return (int.from_bytes(data[24:27], "little") + 1,
                    int.from_bytes(data[27:30], "little") + 1, "webp")
        if data[12:16] == b"VP8 ":
            return (struct.unpack("<H", data[26:28])[0] & 0x3FFF,
                    struct.unpack("<H", data[28:30])[0] & 0x3FFF, "webp")
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        return w, h, "gif"
    return None


def probe_video(path: Path) -> float:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "format=duration", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out)
    except Exception:  # noqa: BLE001
        return 0.0


def cut_video(src: Path, dst: Path, want: float = CUT_SECONDS) -> float:
    """素材站原片没有台标，取中段最稳；裁完只留短片。"""
    d = probe_video(src)
    if d <= 0:
        return 0.0
    if d >= want + 1.5:
        start, end = round((d - want) / 2, 2), round((d - want) / 2 + want, 2)
    else:
        start, end = 0.3, round(max(1.0, d - 0.3), 2)
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-to", str(end),
                        "-i", str(src), "-map", "0:v:0", "-an", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "21",
                        "-movflags", "+faststart", str(dst)],
                       capture_output=True, text=True, timeout=300)
    return 0.0 if r.returncode else probe_video(dst)


# ---------------------------------------------------------------- 四种取法
# 取法只负责「列出候选」，下载与验货统一在 fill_one 里做：
# 一个候选不合格就换下一个，别一上来就把整格判死。
def cand_pexels(queries: list[str], key: str) -> list[dict]:
    out: list[dict] = []
    for q in queries:
        url = ("https://api.pexels.com/v1/search?query=" + urllib.parse.quote(q) +
               "&per_page=12&orientation=landscape&size=large")
        try:
            data = json.loads(http_get(url, 30, {"Authorization": key}).decode("utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for p in data.get("photos", []):
            if p.get("width", 0) < MIN_IMG_W or p.get("height", 0) < MIN_IMG_H:
                continue
            src = (p.get("src") or {}).get("large2x")
            if src:
                out.append({"url": src, "page": p.get("url", ""), "tag": q,
                            "license": "Pexels License 免费商用"})
            if len(out) >= MAX_CAND:
                return out
    return out


def mixkit_index(root: Path) -> list[dict]:
    """分类页先落一份 24 小时索引：挑 ID 靠 slug，绝不拿正则猜文件名。"""
    f = root / "_mixkit_index.json"
    ck = str(f)
    if ck in _MK_CACHE:
        return _MK_CACHE[ck]
    if f.is_file() and time.time() - f.stat().st_mtime < INDEX_TTL:
        try:
            rows = json.loads(f.read_text(encoding="utf-8"))
            _MK_CACHE[ck] = rows
            return rows
        except Exception:  # noqa: BLE001
            pass
    with _MK_LOCK:
        if ck in _MK_CACHE:
            return _MK_CACHE[ck]
        seen, rows, pages = set(), [], 0
        for cat in MIXKIT_CATS:
            try:
                html = http_get(f"https://mixkit.co/free-stock-video/{cat}/",
                                30).decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                continue
            pages += 1
            for slug, vid in re.findall(r'href="/free-stock-video/([a-z0-9-]+?)-(\d+)/"', html):
                if vid not in seen:
                    seen.add(vid)
                    rows.append({"slug": slug, "id": vid})
            time.sleep(0.2)
        if rows:
            tmp = f.with_name(f.name + ".tmp")
            tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, f)          # 原子落盘，并发不会写出半个文件
            _MK_CACHE[ck] = rows
        print(f"  mixkit 索引：{len(rows)} 条（分类页 {pages}/{len(MIXKIT_CATS)}）")
        return rows


def cand_mixkit(root: Path, queries: list[str], want_id: str | None) -> list[dict]:
    rows = mixkit_index(root)
    picked: list[dict] = []
    if want_id:
        picked = [r for r in rows if r["id"] == want_id]
    else:
        for q in queries:
            try:
                pat = re.compile(q.lower())
            except re.error:
                pat = re.compile(re.escape(q.lower()))
            hits = [r for r in rows if pat.search(r["slug"].replace("-", " "))]
            hits.sort(key=lambda r: len(r["slug"]))
            picked.extend(hits)
    out = []
    for r in list({c["id"]: c for c in picked}.values())[:MAX_CAND]:
        out.append({"url": f"https://assets.mixkit.co/videos/{r['id']}/{r['id']}-720.mp4",
                    "alt": f"https://assets.mixkit.co/videos/{r['id']}/{r['id']}-1080.mp4",
                    "page": f"https://mixkit.co/free-stock-video/{r['slug']}-{r['id']}/",
                    "tag": r["slug"], "license": "Mixkit 免费商用，无需署名"})
    return out


def cand_og(sites: list[str]) -> list[dict]:
    """queries 里既可以填网页地址（抓它的 og:image），也可以直接填一张图的地址。

    机构官网首页往往只有几十像素的 logo，真图通常在新闻报道页或产品页里。
    """
    out = []
    for site in sites:
        if re.search(r"\.(jpe?g|png|webp|gif)(_|$|\?)", site, re.I):
            out.append({"url": site, "page": site,
                        "tag": Path(urllib.parse.urlparse(site).path).stem,
                        "license": "来源方公开图，商用授权需自行确认"})
            continue
        try:
            html = http_get(site, 30).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            continue
        found = []
        for pat in (r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
                    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
                    r'<img[^>]+src=["\']([^"\']+\.(?:png|jpe?g|webp))[^"\']*["\']'):
            for m in re.findall(pat, html, re.I):
                found.append(urllib.parse.urljoin(site, m.replace("&amp;", "&")))
        for u in dict.fromkeys(found):
            out.append({"url": u, "page": site, "referer": site,
                        "min_w": 420, "min_h": 260, "min_bytes": 8_000, "min_ratio": 0.8,
                        "tag": urllib.parse.urlparse(site).netloc.replace(".", "-"),
                        "license": "来源方公开素材，商用授权需自行确认"})
    return out[:MAX_CAND]


def cand_wikimedia(queries: list[str]) -> list[dict]:
    """Commons API 按关键词搜图：只收 CC/公有领域，license 随条目带回 manifest。

    bing 被风控回壳页时的概念图主力；queries 用英文实物词，抽象词搜不到。
    """
    out = []
    for q in queries:
        api = ("https://commons.wikimedia.org/w/api.php?action=query&format=json"
               "&generator=search&gsrsearch=" + urllib.parse.quote("filetype:bitmap " + q) +
               "&gsrnamespace=6&gsrlimit=10&prop=imageinfo"
               "&iiprop=url%7Csize%7Cextmetadata&iiurlwidth=1600")
        try:
            data = json.loads(http_get(api, 30).decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            continue
        pages = (data.get("query") or {}).get("pages") or {}
        rows = [p for p in pages.values() if p.get("imageinfo")]
        rows.sort(key=lambda p: p.get("index", 99))
        for p in rows:
            info = p["imageinfo"][0]
            lic = ((info.get("extmetadata") or {}).get("LicenseShortName") or {}).get("value", "")
            if lic and "CC" not in lic.upper() and "PUBLIC" not in lic.upper():
                continue  # fair use 之类商用不明的直接不要
            u = info.get("thumburl") or info.get("url")
            if not u:
                continue
            out.append({"url": u, "page": info.get("descriptionurl", u), "tag": q,
                        "license": f"Wikimedia {lic}" if lic else "Wikimedia Commons",
                        "min_w": 420, "min_h": 260, "min_ratio": 1.1})
    return out[:MAX_CAND * 2]


def cand_bing(queries: list[str]) -> list[dict]:
    out = []
    for q in queries:
        url = ("https://cn.bing.com/images/search?q=" + urllib.parse.quote(q) +
               "&form=HDRSC2&first=1&count=35"
               "&qft=+filterui:photo-photo+filterui:imagesize-large")
        try:
            html = http_get(url, 30).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            continue
        hits = [m.replace("\\/", "/").replace("&amp;", "&")
                for m in re.findall(r'murl&quot;:&quot;(.*?)&quot;', html)]
        for u in dict.fromkeys(hits):
            if not u.startswith("http") or any(b in u for b in BAD_HOST):
                continue
            out.append({"url": u, "page": u, "tag": q,
                        "license": "网络公开图，商用授权未确认"})
    return out[:MAX_CAND * 2]


def gather(root: Path, site: str, queries: list[str], key: str,
           slot: dict) -> list[dict]:
    try:
        if site == "pexels":
            return cand_pexels(queries, key) if key else cand_wikimedia(queries)
        if site == "mixkit":
            return cand_mixkit(root, queries, slot.get("mixkit_id"))
        if site == "og":
            return cand_og(queries)
        if site == "wikimedia":
            return cand_wikimedia(queries)
        return cand_bing(queries)
    except Exception as e:  # noqa: BLE001
        print(f"  [{slot.get('num')}] {site} 取法异常 {type(e).__name__}")
        return []


def grab_blob(cand: dict) -> bytes | None:
    """下载一个候选；主地址不通就试备用清晰度。"""
    for u in (cand.get("url"), cand.get("alt")):
        if not u:
            continue
        is_vid = ".mp4" in u
        try:
            data = http_get(u, 240 if is_vid else 60,
                            {"Referer": cand["referer"]} if cand.get("referer") else None)
        except Exception:  # noqa: BLE001
            continue
        if len(data) < (VID_BYTES_MIN if is_vid
                        else cand.get("min_bytes", IMG_BYTES_MIN)):
            continue
        if not is_vid and data[:200].find(b"<html") >= 0:
            continue
        return data
    return None


# ---------------------------------------------------------------- 落盘与调度
def write_asset(root: Path, num: str, cand: dict, blob: bytes) -> dict | None:
    """图片入库即验：魔数、真实宽高、最小尺寸，不合格就丢、换下一个候选。

    品牌/机构格（og）会带更低的尺寸下限——小尺寸的真 logo 好过大尺寸的概念图。
    """
    d = root / "images"
    d.mkdir(parents=True, exist_ok=True)
    got = img_dims(blob)
    if not got:
        return None
    w, h, ext = got
    if w < cand.get("min_w", MIN_IMG_W) or h < cand.get("min_h", MIN_IMG_H):
        return None
    if w / h < cand.get("min_ratio", MIN_RATIO):
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", str(cand.get("tag", "")).lower()).strip("-")[:26] or "asset"
    for old in sorted(d.glob(f"{num}-*")):
        old.unlink()
    dest = d / f"{num}-{slug}.{ext}"
    dest.write_bytes(blob)
    return {"file": f"images/{dest.name}", "md5": hashlib.md5(blob).hexdigest(),
            "bytes": len(blob), "w": w, "h": h}


def write_video(root: Path, num: str, tag: str, blob: bytes) -> dict | None:
    """原片先裁成单镜头再入库，只留短片。"""
    d = root / "videos"
    d.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", tag.lower()).strip("-")[:26] or "clip"
    for old in sorted(d.glob(f"{num}-*")):
        old.unlink()
    raw = root / ".tmp" / f"{num}.raw.mp4"
    raw.parent.mkdir(exist_ok=True)
    raw.write_bytes(blob)
    dest = d / f"{num}-{slug}.mp4"
    got = cut_video(raw, dest)
    raw.unlink(missing_ok=True)
    if got <= 0 or not dest.is_file():
        dest.unlink(missing_ok=True)
        return None
    out = dest.read_bytes()
    return {"file": f"videos/{dest.name}", "md5": hashlib.md5(out).hexdigest(),
            "bytes": len(out), "duration": round(got, 2)}


def fill_one(root: Path, slot: dict, key: str, seen_md5: set) -> dict | None:
    num = slot["num"]
    kind = slot.get("kind", "image")
    site = slot.get("site", "pexels" if kind == "image" else "mixkit")
    queries = slot.get("queries") or []
    cands = gather(root, site, queries, key, slot)
    if kind == "image" and site not in ("bing", "wikimedia") and len(cands) < 2:
        print(f"  [{num}] {site} 候选不足，追加搜索引擎兜底候选")
        cands += gather(root, "bing", queries, key, slot)
    for cand in cands[:MAX_CAND * 2]:
        blob = grab_blob(cand)
        if not blob:
            continue
        rec = (write_video(root, num, cand["tag"], blob) if kind == "video"
               else write_asset(root, num, cand, blob))
        if not rec:
            continue
        if rec["md5"] in seen_md5:
            (root / rec["file"]).unlink(missing_ok=True)
            print(f"  [{num}] 这张已用过，换下一个候选")
            continue
        seen_md5.add(rec["md5"])
        rec.update({"num": num, "kind": kind, "site": site, "query": cand.get("tag", ""),
                    "source_url": cand.get("page", ""), "license": cand.get("license", ""),
                    "summary": slot.get("summary", ""), "tags": slot.get("tags", []),
                    "anchor": slot.get("anchor", "")})
        tail = (f" {rec['w']}x{rec['h']}" if kind == "image" else f" {rec['duration']}s")
        print(f"  [{num}] OK {rec['file']}{tail}")
        return rec
    return None


def contact_sheet(root: Path, rows: list[dict]) -> None:
    """一屏看完全部图：标着文件名的那一眼就能定哪格要补。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print("  未装 Pillow，跳过联络表（可自己翻 images/ 目检）")
        return
    files = [root / r["file"] for r in rows
             if r.get("kind") == "image" and (root / r["file"]).is_file()]
    if not files:
        return
    tw, th, lab, pad, cols = 320, 210, 18, 6, 5
    n_rows = (len(files) + cols - 1) // cols
    canvas = Image.new("RGB", (cols * (tw + pad) + pad, n_rows * (th + lab + pad) + pad),
                       (26, 26, 30))
    draw = ImageDraw.Draw(canvas)
    for i, f in enumerate(files):
        try:
            im = Image.open(f).convert("RGB")
        except Exception:  # noqa: BLE001
            continue
        im.thumbnail((tw, th), Image.LANCZOS)
        x = pad + (i % cols) * (tw + pad)
        y = pad + (i // cols) * (th + lab + pad)
        canvas.paste(im, (x, y))
        draw.text((x, y + th + 2), f.name[:40], fill=(235, 235, 235))
    out = root / "contact_sheet.jpg"
    canvas.save(out, quality=86)
    print(f"  联络表：{out}（{len(files)} 张图）目检后只补不合格那几格")


def main() -> int:
    ap = argparse.ArgumentParser(description="按槽位表并发抓取素材")
    ap.add_argument("root", help="素材包目录（内含 slots.json）")
    ap.add_argument("--only", default="", help="只补这些编号，逗号分隔：14,25")
    ap.add_argument("--report", action="store_true", help="只打印填写状态")
    ap.add_argument("--workers", type=int, default=0, help="并发数，默认图 12 / 视频 4")
    ap.add_argument("--pexels-key", default=os.environ.get("PEXELS_API_KEY", "").strip())
    a = ap.parse_args()

    root = Path(a.root).resolve()
    sf = root / "slots.json"
    if not sf.is_file():
        print(f"缺槽位表：{sf}")
        return 2
    slots = json.loads(sf.read_text(encoding="utf-8")).get("assets", [])
    for s in slots:
        s["num"] = str(s["num"]).zfill(2)
    reg = root / "registry.json"
    done = {r["num"]: r for r in (json.loads(reg.read_text(encoding="utf-8"))
                                  if reg.is_file() else [])}
    only = {s.strip().zfill(2) for s in a.only.split(",") if s.strip()}

    if a.report:
        miss = [s["num"] for s in slots if s["num"] not in done]
        print(f"槽位 {len(slots)}，已填 {len(done)}，缺 {len(miss)}：{' '.join(miss) or '无'}")
        return 0

    todo = [s for s in slots if not only or s["num"] in only]
    imgs = [s for s in todo if s.get("kind", "image") == "image"]
    vids = [s for s in todo if s.get("kind") == "video"]
    seen_md5 = {r["md5"] for r in done.values() if r.get("md5")}
    if not a.pexels_key:
        print("未提供 PEXELS_API_KEY：pexels 槽位会退回搜索引擎抓取（限速更严、质量更差）")
    print(f"开抓：图 {len(imgs)} 格（并发 {a.workers or 12}）"
          f" + 视频 {len(vids)} 格（并发 {a.workers or 4}）")

    def save() -> None:
        rows_now = [done[k] for k in sorted(done)]
        reg.write_text(json.dumps(rows_now, ensure_ascii=False, indent=1),
                       encoding="utf-8")

    for group, default_w in ((imgs, 12), (vids, 4)):
        if not group:
            continue
        with ThreadPoolExecutor(max_workers=a.workers or default_w) as ex:
            futs = {ex.submit(fill_one, root, s, a.pexels_key, seen_md5): s for s in group}
            for fut, slot in futs.items():
                num = slot["num"]
                try:
                    rec = fut.result()
                except Exception as e:  # noqa: BLE001
                    print(f"  [{num}] 崩了 {type(e).__name__} {e}")
                    continue
                if rec:
                    done[rec["num"]] = rec
                    save()          # 每格落盘即写回执：进程中途被打断也不用重下整包
                else:
                    hint = ("og 格要填有配图的新闻页/产品页地址，或直接填那张图的地址"
                            if slot.get("site") == "og" else
                            "换一个更具体的名词（抽象词会被搜成字面物）")
                    print(f"  [{num}] !! 这格没填上：{hint}；改完再 --only {num}")

    save()
    rows = [done[k] for k in sorted(done)]
    missing = [s["num"] for s in slots if s["num"] not in done]
    print(f"\n已填 {len(rows)}/{len(slots)} 格 → registry.json")
    if missing:
        print(f"还缺：{' '.join(missing)}  补法：--only {','.join(missing[:6])}")
    contact_sheet(root, rows)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.exit(main())
