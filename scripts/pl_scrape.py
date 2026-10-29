#!/usr/bin/env python3
"""Build pl.m3u: Polish channels that actually play (stdlib only).

Sources: iptv-org (countries/pl, languages/pol), Free-TV/IPTV Poland.
Strict probe per source URL:
  HLS: master -> best variant (top 2 tried) -> media playlist -> the segment
       next to the live edge downloaded IN FULL (timeout 20 s): must be real
       MPEG-TS with a video stream carrying data (or fMP4 moof+mdat), must
       download >= 1.3x faster than its duration, and a second fetch of the
       media playlist must advance (live; VOD with ENDLIST is exempt).
  DASH: no DRM, init segment is fMP4.
Failing sources are dropped. pl-local-ok.json (written by a run from the PC in
Poland, scripts/pl_scrape_local.ps1, honoured for 14 days) decides geo cases:
a source failing from the GitHub runner (USA) but OK from Poland is kept with
marker '#VNL-GEO:PL'; a source failing from Poland is dropped even if the
runner can play it (the TV is in Poland).
Same channel (normalized name) -> at most MAX_SOURCES sources, best first
(HD first, then download speed).
Output: pl.m3u + pl-report.json (+ pl-local-ok.json with --write-local-ok).
"""
import argparse, json, os, re, sys, time, unicodedata, urllib.request, urllib.error
import threading
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

SOURCES = [
    ("iptv-org/pl", "https://iptv-org.github.io/iptv/countries/pl.m3u"),
    ("iptv-org/pol", "https://iptv-org.github.io/iptv/languages/pol.m3u"),
    ("free-tv/pl", "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_poland.m3u8"),
]
GROUP = "Kênh Ba Lan"
DEFAULT_UA = "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
TIMEOUT = 10        # playlist requests
SEG_TIMEOUT = 20    # whole media segment
SEG_LIMIT = 16 * 1024 * 1024
MIN_SPEED = 1.3     # segment must download >= 1.3x faster than it plays
MAX_SOURCES = 2     # per channel: best + one backup
LOCAL_OK = "pl-local-ok.json"
LOCAL_MAX_AGE_DAYS = 14
# main channels first (prefix match on normalized name)
PRIORITY = ["tvp1", "tvp2", "tvpinfo", "tvpsport", "tvpworld", "tvppolonia", "tvp",
            "polsatnews", "polsatsport", "polsat", "tvn24", "tvn", "canalplussport",
            "eleven", "sport"]
VIDEO_TYPES = {0x01, 0x02, 0x10, 0x1B, 0x24, 0x42, 0xEA}


def norm(name):
    s = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", name)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("+", "plus")
    s = re.sub(r"\b(hd|fhd|uhd|sd|4k|1080p|720p|576p|480p|360p)\b", " ", s)
    return re.sub(r"[^a-z0-9]", "", s)


def fetch(url, opts, limit=None, timeout=None):
    h = {"User-Agent": opts.get("ua") or DEFAULT_UA}
    if opts.get("ref"):
        h["Referer"] = opts["ref"]
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as r:
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


def ts_check(b):
    """Real MPEG-TS: >=90% packets synced, PMT lists a video stream and the
    video PID carries payload (not just PAT/PMT/null padding)."""
    n = len(b) // 188
    if n < 50:
        return False, "ts too short"
    if sum(1 for i in range(n) if b[i * 188] == 0x47) < n * 0.9:
        return False, "ts not synced"
    pmt_pids, streams, counts = set(), set(), {}
    for i in range(n):
        p = b[i * 188:(i + 1) * 188]
        if p[0] != 0x47:
            continue
        pid = ((p[1] & 0x1F) << 8) | p[2]
        pusi = p[1] & 0x40
        afc = (p[3] >> 4) & 3
        off = 4
        if afc in (2, 3):
            off += 1 + p[4]
        if afc in (1, 3) and off < 188:
            counts[pid] = counts.get(pid, 0) + 1
        if not pusi or afc not in (1, 3) or off >= 187:
            continue
        if pid != 0 and pid not in pmt_pids:
            continue
        sec = p[off + 1 + p[off]:]
        if len(sec) < 12:
            continue
        end = min(3 + (((sec[1] & 0x0F) << 8) | sec[2]) - 4, len(sec))
        if pid == 0 and sec[0] == 0:
            for j in range(8, end - 3, 4):
                if (sec[j] << 8) | sec[j + 1]:
                    pmt_pids.add(((sec[j + 2] & 0x1F) << 8) | sec[j + 3])
        elif sec[0] == 2:
            j = 12 + (((sec[10] & 0x0F) << 8) | sec[11])
            while j + 5 <= end:
                streams.add((((sec[j + 1] & 0x1F) << 8) | sec[j + 2], sec[j]))
                j += 5 + (((sec[j + 3] & 0x0F) << 8) | sec[j + 4])
    if not pmt_pids:
        return False, "ts no PAT"
    video = [pid for pid, st in streams if st in VIDEO_TYPES]
    if not video:
        return False, "ts no video stream"
    if max(counts.get(pid, 0) for pid in video) < 30:
        return False, "ts video empty"
    return True, "ts"


def mp4_check(b):
    i, boxes = 0, set()
    while i + 8 <= len(b):
        sz = int.from_bytes(b[i:i + 4], "big")
        boxes.add(b[i + 4:i + 8])
        if sz == 1 and i + 16 <= len(b):
            sz = int.from_bytes(b[i + 8:i + 16], "big")
        if sz < 8:
            break
        i += sz
    if b"mdat" in boxes and (b"moof" in boxes or b"moov" in boxes):
        return True, "mp4"
    return False, "mp4 no media"


def timed_get(url, opts, limit):
    t = time.time()
    _, b = fetch(url, opts, limit, timeout=SEG_TIMEOUT)
    return b, max(time.time() - t, 0.001)


def media_playlist(text):
    segs, durs, dur = [], [], None
    for l in (x.strip() for x in text.splitlines()):
        if l.startswith("#EXTINF:"):
            try:
                dur = float(l[8:].split(",")[0])
            except ValueError:
                dur = None
        elif l and not l.startswith("#"):
            segs.append(l)
            durs.append(dur or 0)
            dur = None
    seq = re.search(r"#EXT-X-MEDIA-SEQUENCE:(\d+)", text)
    td = re.search(r"#EXT-X-TARGETDURATION:(\d+)", text)
    return segs, durs, int(seq.group(1)) if seq else 0, int(td.group(1)) if td else 6


def probe_media(url, opts, text):
    segs, durs, seq, td = media_playlist(text)
    if not segs:
        return False, "no segments", {}
    enc = "METHOD=AES-128" in text or "METHOD=SAMPLE-AES" in text
    m = re.search(r'#EXT-X-MAP:.*URI="([^"]+)"', text)
    init = b""
    if m and not enc:
        init, _ = timed_get(urljoin(url, m.group(1)), opts, SEG_LIMIT)
        if media_kind(init) != "mp4":
            return False, "bad init", {}
    k = max(0, len(segs) - 2)  # next to the live edge, already complete
    d = durs[k] or td
    t0 = time.time()
    b, took = timed_get(urljoin(url, segs[k]), opts, SEG_LIMIT)
    info = {"seg_kb": len(b) // 1024, "seg_s": d, "dl_s": round(took, 2),
            "speed": round(d / took, 2) if d else None}
    if len(b) < 20000:
        return False, "segment too small (%d B)" % len(b), info
    if enc:
        ok, why = True, "encrypted"
    else:
        kind = media_kind(b)
        if kind == "ts":
            ok, why = ts_check(b)
        elif kind == "mp4" or init:
            ok, why = mp4_check(init + b)
        elif kind == "aac":
            ok, why = False, "audio only"
        else:
            ok, why = False, "bad segment"
    if not ok:
        return False, why, info
    if d and d / took < MIN_SPEED:
        return False, "too slow (%.2fx real time)" % (d / took), info
    if "#EXT-X-ENDLIST" in text:
        return True, why + " vod", info
    # live: the playlist must advance (2 tries; CDNs may cache it a few seconds)
    for attempt in range(2):
        time.sleep(max(0.0, min(td, 10) + 0.5 - (time.time() - t0)))
        t0 = time.time()
        _, b2 = fetch(url, opts)
        segs2, _, seq2, _ = media_playlist(b2.decode("utf-8", "ignore"))
        if not segs2:
            return False, "live: playlist empty on refetch", info
        if seq2 > seq or segs2[-1] != segs[-1]:
            return True, why, info
    return False, "live: playlist not advancing", info


def probe_hls(url, opts, body, depth=0):
    text = body.decode("utf-8", "ignore")
    if "#EXTM3U" not in text:
        return False, "not m3u8", {}
    lines = [l.strip() for l in text.splitlines()]
    if "#EXT-X-STREAM-INF" in text and depth < 2:
        var = []
        for i, l in enumerate(lines[:-1]):
            if l.startswith("#EXT-X-STREAM-INF"):
                bw = re.search(r"BANDWIDTH=(\d+)", l)
                res = re.search(r"RESOLUTION=\d+x(\d+)", l)
                var.append((int(res.group(1)) if res else 0, int(bw.group(1)) if bw else 0, lines[i + 1]))
        var.sort(reverse=True)  # best first; one of the top 2 must play
        why, info = "no variants", {}
        for h, _, v in var[:2]:
            u2, b2 = fetch(urljoin(url, v), opts)
            ok, why, info = probe_hls(u2, opts, b2, depth + 1)
            if ok:
                if h:
                    info["height"] = h
                return ok, why, info
        return False, "variant: " + why, info
    return probe_media(url, opts, text)


def probe_dash(url, opts, body):
    root = ET.fromstring(body)
    ns = root.tag[1:].split("}")[0] if root.tag.startswith("{") else ""
    q = (lambda t: "{%s}%s" % (ns, t)) if ns else (lambda t: t)
    if root.find(".//" + q("ContentProtection")) is not None:
        return False, "dash drm", {}
    base = url
    bu = root.find(q("BaseURL"))
    if bu is not None and bu.text:
        base = urljoin(url, bu.text.strip())
    rep = next(root.iter(q("Representation")), None)
    rid = rep.get("id", "") if rep is not None else ""
    bw = rep.get("bandwidth", "") if rep is not None else ""
    h = max([int(r.get("height") or 0) for r in root.iter(q("Representation"))] or [0])
    for st in root.iter(q("SegmentTemplate")):
        init = st.get("initialization")
        if not init:
            continue
        init = init.replace("$RepresentationID$", rid).replace("$Bandwidth$", bw)
        b, _ = timed_get(urljoin(base, init), opts, SEG_LIMIT)
        return media_kind(b) == "mp4", "dash init", {"height": h}
    return False, "mpd without SegmentTemplate init", {}


_host_locks, _hl = {}, threading.Lock()


def probe(item):
    # one probe per host at a time: many channels share one server; probing
    # them in parallel would saturate it and make every one look too slow
    host = urlsplit(item["url"]).hostname or ""
    with _hl:
        lock = _host_locks.setdefault(host, threading.Lock())
    with lock:
        return _probe(item)


def _probe(item):
    url, opts = item["url"], item["opts"]
    t = time.time()
    try:
        final, body = fetch(url, opts, 1024 * 1024)
        head = body[:2000].lstrip()
        if head.startswith(b"#EXTM3U"):
            ok, why, info = probe_hls(final, opts, body)
        elif b"<MPD" in head:
            ok, why, info = probe_dash(final, opts, body)
        elif media_kind(body) == "ts":  # raw MPEG-TS over HTTP (endless stream)
            took = time.time() - t
            ok, why = ts_check(body)
            info = {"raw_ts_kbps": int(len(body) * 8 / 1000 / max(took, 0.001))}
            if ok and len(body) < 1024 * 1024:
                ok, why = False, "raw ts ended early"
        else:
            ok, why, info = False, "not a playlist (%s)" % (media_kind(body) or "unknown"), {}
        r = {"status": "ok" if ok else "bad", "why": why, "ms": int((time.time() - t) * 1000)}
        r.update(info)
        return r
    except urllib.error.HTTPError as e:
        return {"status": "geo" if e.code in (401, 403, 451) else "bad", "why": "HTTP %d" % e.code, "ms": 0}
    except Exception as e:
        return {"status": "bad", "why": "%s: %s" % (type(e).__name__, str(e)[:80]), "ms": 0}


def rank(key):
    for i, p in enumerate(PRIORITY):
        if key.startswith(p):
            return i
    return len(PRIORITY)


def height_of(s):
    h = s["probe"].get("height") or 0
    if not h:
        m = re.search(r"\((\d{3,4})[pi]\)", s["name"])
        h = int(m.group(1)) if m else 0
    if not h and re.search(r"\b(hd|fhd)\b", s["name"].lower()):
        h = 720
    return h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=".")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--write-local-ok", action="store_true",
                    help="run inside Poland: also write pl-local-ok.json (URLs that passed here)")
    ap.add_argument("--local-ok", default=None, help="path of pl-local-ok.json (default: repo root)")
    a = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    lok_path = a.local_ok or os.path.join(root, LOCAL_OK)
    local_ok, local_bad = set(), set()
    if not a.write_local_ok and os.path.exists(lok_path):
        with open(lok_path, encoding="utf-8") as f:
            lj = json.load(f)
        age = (time.time() - time.mktime(time.strptime(lj.get("checked", "2000-01-01"), "%Y-%m-%d"))) / 86400
        if age <= LOCAL_MAX_AGE_DAYS:  # the PC in Poland decides, as long as its result is recent
            local_ok, local_bad = set(lj.get("urls", [])), set(lj.get("bad", []))
        else:
            print("pl-local-ok.json older than %d days: ignored" % LOCAL_MAX_AGE_DAYS, file=sys.stderr)

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
        if it["url"] in local_bad:
            r["status"], r["why"] = "bad", "failed from Poland (pl-local-ok.json): " + r["why"]
            continue
        if r["status"] == "ok":
            it["mark"] = None
        elif it["url"] in local_ok:
            it["mark"] = "geo"  # fails here, verified from Poland
        else:
            continue
        key = norm(it["name"]) or norm(it["attrs"].get("tvg-id", ""))
        if key:
            chans.setdefault(key, []).append(it)

    lines, nsrc, ngeo, dropped_dupes = ["#EXTM3U"], 0, 0, 0
    for k in sorted(chans, key=lambda k: (rank(k), k)):
        srcs = sorted(chans[k], key=lambda s: (s["mark"] is not None, -min(height_of(s), 1080),
                                               -(s["probe"].get("speed") or 0)))
        dropped_dupes += max(0, len(srcs) - MAX_SOURCES)
        srcs = srcs[:MAX_SOURCES]
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
    with open(os.path.join(a.out, "pl.m3u"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    if a.write_local_ok:
        ok_urls = sorted(it["url"] for it, r in zip(uniq, res) if r["status"] == "ok")
        with open(os.path.join(a.out, LOCAL_OK), "w", encoding="utf-8", newline="\n") as f:
            bad_urls = sorted(it["url"] for it, r in zip(uniq, res) if r["status"] != "ok")
            json.dump({"checked": time.strftime("%Y-%m-%d"), "from": "PL", "urls": ok_urls, "bad": bad_urls}, f, indent=1)

    stat = {}
    for r in res:
        stat[r["status"]] = stat.get(r["status"], 0) + 1
    rep = {"sources": per_source, "entries": len(items), "unique_urls": len(uniq),
           "probe": stat, "channels": len(chans),
           "channels_verified": sum(1 for v in chans.values() if any(s["mark"] is None for s in v)),
           "sources_written": nsrc, "geo_marked": ngeo, "duplicates_dropped": dropped_dupes,
           "local_ok_urls": len(local_ok),
           "failed": [{"name": it["name"], "url": it["url"], "why": r["why"],
                       **({"speed": r["speed"]} if r.get("speed") else {})}
                      for it, r in zip(uniq, res) if r["status"] != "ok"]}
    with open(os.path.join(a.out, "pl-report.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    print(json.dumps({k: v for k, v in rep.items() if k != "failed"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
