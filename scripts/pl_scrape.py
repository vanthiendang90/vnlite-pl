#!/usr/bin/env python3
"""Build pl.m3u: Polish channels that actually play (stdlib only).

Sources: iptv-org (countries/pl, languages/pol), Free-TV/IPTV Poland.
Each source URL is probed: HLS master -> variant -> media playlist -> ~200 KB of
first segment (must be MPEG-TS / fMP4 / ADTS); DASH: MPD + init segment.
Geo-blocked / 403 / 451 sources are kept with marker '#VNL-GEO:PL' so the
device Health scan decides (--strict-geo drops them, for runs inside Poland).
Output: pl.m3u + pl-report.json.
"""
import argparse, json, re, sys, time, unicodedata, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

SOURCES = [
    ("iptv-org/pl", "https://iptv-org.github.io/iptv/countries/pl.m3u"),
    ("iptv-org/pol", "https://iptv-org.github.io/iptv/languages/pol.m3u"),
    ("free-tv/pl", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_poland.m3u8"),
]
GROUP = "Kênh Ba Lan"
DEFAULT_UA = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
TIMEOUT = 10
SEG_BYTES = 200 * 1024
# main channels first (prefix match on normalized name)
PRIORITY = ["tvp1", "tvp2", "tvpinfo", "tvpsport", "tvpworld", "tvppolonia", "tvp",
            "polsatnews", "polsatsport", "polsat", "tvn24", "tvn", "canalplussport",
            "eleven", "sport"]


def norm(name):
    s = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", name)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("+", "plus")
    s = re.sub(r"\b(hd|fhd|uhd|sd|4k|1080p|720p|576p|480p|360p)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def fetch(url, opts, limit=None):
    h = {"User-Agent": opts.get("ua") or DEFAULT_UA}
    if opts.get("ref"):
        h["Referer"] = opts["ref"]
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.geturl(), r.read(limit or 4 * 1024 * 1024)


def parse_m3u(text, src):
    out, cur, opts = [], None, {}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line))
            name = line.rsplit(",", 1)[-1].strip()
            cur = {"name": name, "attrs": attrs, "src": src,
                   "geo": "geo-blocked" in name.lower()}
            opts = {}
        elif line.startswith("#EXTVLCOPT:"):
            k, _, v = line[11:].partition("=")
            k = k.strip().lower()
            if k == "http-user-agent":
                opts["ua"] = v.strip()
            elif k in ("http-referrer", "http-referer"):
                opts["ref"] = v.strip()
        elif line and not line.startswith("#") and cur:
            url, _, pipe = line.partition("|")
            for kv in pipe.split("&") if pipe else []:
                k, _, v = kv.partition("=")
                if k.lower() == "user-agent":
                    opts["ua"] = v
                if k.lower() in ("referer", "referrer"):
                    opts["ref"] = v
            cur["url"], cur["opts"] = url.strip(), dict(opts)
            out.append(cur)
            cur = None
    return out


def media_kind(b):
    if len(b) >= 189 and b[0] == 0x47 and b[188] == 0x47:
        return "ts"
    if b[4:8] in (b"ftyp", b"styp", b"moof", b"sidx", b"moov"):
        return "mp4"
    if b[:3] == b"ID3" or (len(b) > 1 and b[0] == 0xFF and (b[1] & 0xF6) == 0xF0):
        return "aac"
    return None


def probe_hls(url, opts, body, depth=0):
    text = body.decode("utf-8", "ignore")
    if "#EXTM3U" not in text:
        return False, "not m3u8"
    lines = [l.strip() for l in text.splitlines()]
    if "#EXT-X-STREAM-INF" in text and depth < 2:
        var = [lines[i + 1] for i, l in enumerate(lines[:-1]) if l.startswith("#EXT-X-STREAM-INF")]
        why = "no variants"
        for v in var[:2]:
            u2, b2 = fetch(urljoin(url, v), opts)
            ok, why = probe_hls(u2, opts, b2, depth + 1)
            if ok:
                return ok, why
        return False, "variant: " + why
    segs = [l for l in lines if l and not l.startswith("#")]
    if not segs:
        return False, "no segments"
    if "METHOD=AES-128" in text or "METHOD=SAMPLE-AES" in text:
        _, b = fetch(urljoin(url, segs[0]), opts, SEG_BYTES)
        return len(b) > 1000, "encrypted seg"
    m = re.search(r'#EXT-X-MAP:.*URI="([^"]+)"', text)
    if m:
        _, b = fetch(urljoin(url, m.group(1)), opts, SEG_BYTES)
        if media_kind(b) != "mp4":
            return False, "bad init"
    _, b = fetch(urljoin(url, segs[0]), opts, SEG_BYTES)
    k = media_kind(b)
    return k is not None, k or "bad segment"


def probe_dash(url, opts, body):
    root = ET.fromstring(body)
    ns = root.tag[1:].split("}")[0] if root.tag.startswith("{") else ""
    q = (lambda t: "{%s}%s" % (ns, t)) if ns else (lambda t: t)
    base = url
    bu = root.find(q("BaseURL"))
    if bu is not None and bu.text:
        base = urljoin(url, bu.text.strip())
    rep = next(root.iter(q("Representation")), None)
    rid = rep.get("id", "") if rep is not None else ""
    bw = rep.get("bandwidth", "") if rep is not None else ""
    for st in root.iter(q("SegmentTemplate")):
        init = st.get("initialization")
        if not init:
            continue
        init = init.replace("$RepresentationID$", rid).replace("$Bandwidth$", bw)
        _, b = fetch(urljoin(base, init), opts, SEG_BYTES)
        return media_kind(b) == "mp4", "dash init"
    return False, "mpd without SegmentTemplate init"


def probe(item):
    url, opts = item["url"], item["opts"]
    t = time.time()
    try:
        final, body = fetch(url, opts, 1024 * 1024)
        head = body[:2000].lstrip()
        if head.startswith(b"#EXTM3U"):
            ok, why = probe_hls(final, opts, body)
        elif b"<MPD" in head:
            ok, why = probe_dash(final, opts, body)
        else:
            k = media_kind(body)
            ok, why = k is not None, k or "unknown body"
        return {"status": "ok" if ok else "bad", "why": why, "ms": int((time.time() - t) * 1000)}
    except urllib.error.HTTPError as e:
        return {"status": "geo" if e.code in (403, 451) else "bad", "why": "HTTP %d" % e.code, "ms": 0}
    except Exception as e:
        return {"status": "bad", "why": "%s: %s" % (type(e).__name__, str(e)[:80]), "ms": 0}


def rank(key):
    for i, p in enumerate(PRIORITY):
        if key.startswith(p):
            return i
    return len(PRIORITY)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--strict-geo", action="store_true",
                    help="run inside Poland: drop failing sources instead of marking #VNL-GEO:PL")
    a = ap.parse_args()

    items, per_source = [], {}
    for sid, u in SOURCES:
        try:
            _, b = fetch(u, {})
            got = parse_m3u(b.decode("utf-8", "ignore"), sid)
            per_source[sid] = len(got)
            items += got
        except Exception as e:
            per_source[sid] = "error: %s" % e
            print("source fail", sid, e, file=sys.stderr)
    if not items:
        sys.exit("no source playlists loaded")

    seen, uniq = set(), []
    for it in items:
        if it["url"] not in seen:
            seen.add(it["url"])
            uniq.append(it)
    with ThreadPoolExecutor(a.workers) as ex:
        res = list(ex.map(probe, uniq))

    chans = {}
    for it, r in zip(uniq, res):
        it["probe"] = r
        if r["status"] == "ok":
            it["mark"] = None
        elif not a.strict_geo and (r["status"] == "geo" or it["geo"]):
            it["mark"] = "geo"
        else:
            continue
        key = norm(it["name"]) or norm(it["attrs"].get("tvg-id", ""))
        if key:
            chans.setdefault(key, []).append(it)

    lines, nsrc, ngeo = ["#EXTM3U"], 0, 0
    for k in sorted(chans, key=lambda k: (rank(k), k)):
        srcs = sorted(chans[k], key=lambda s: (s["mark"] is not None, s["probe"]["ms"]))
        name = re.sub(r"\s*(\[[^\]]*\]|\((\d{3,4}[pi]|[^)]*geo[^)]*)\))", "", srcs[0]["name"]).strip()
        tid = next((s["attrs"]["tvg-id"] for s in srcs if s["attrs"].get("tvg-id")), "")
        logo = next((s["attrs"]["tvg-logo"] for s in srcs if s["attrs"].get("tvg-logo")), "")
        for s in srcs:
            lines.append('#EXTINF:-1 tvg-id="%s" tvg-logo="%s" group-title="%s",%s' % (tid, logo, GROUP, name))
            if s["mark"]:
                lines.append("#VNL-GEO:PL")
                ngeo += 1
            if s["opts"].get("ref"):
                lines.append("#EXTVLCOPT:http-referrer=" + s["opts"]["ref"])
            if s["opts"].get("ua"):
                lines.append("#EXTVLCOPT:http-user-agent=" + s["opts"]["ua"])
            lines.append(s["url"])
            nsrc += 1
    with open(a.out + "/pl.m3u", "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    stat = {}
    for r in res:
        stat[r["status"]] = stat.get(r["status"], 0) + 1
    rep = {"sources": per_source, "entries": len(items), "unique_urls": len(uniq),
           "probe": stat, "channels": len(chans),
           "channels_verified": sum(1 for v in chans.values() if any(s["mark"] is None for s in v)),
           "sources_written": nsrc, "geo_marked": ngeo, "strict_geo": a.strict_geo,
           "failed": [{"name": it["name"], "url": it["url"], "why": r["why"]}
                      for it, r in zip(uniq, res) if r["status"] != "ok"]}
    with open(a.out + "/pl-report.json", "w", encoding="utf-8", newline="\n") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in rep.items() if k != "failed"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
