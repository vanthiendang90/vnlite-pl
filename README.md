# vnlite-pl

**VI:** Danh sách kênh truyền hình Ba Lan miễn phí, công khai, tổng hợp từ [iptv-org/iptv](https://github.com/iptv-org/iptv) và [Free-TV/IPTV](https://github.com/Free-TV/IPTV). Chỉ chứa đường link, không lưu trữ nội dung nào. Khả năng phát được kiểm tra tự động mỗi ngày (GitHub Actions).

**EN:** Free, public Polish TV channels gathered from [iptv-org/iptv](https://github.com/iptv-org/iptv) and [Free-TV/IPTV](https://github.com/Free-TV/IPTV). Links only, nothing is hosted here. Playability is checked daily by GitHub Actions.

Playlist: `https://raw.githubusercontent.com/vanthiendang90/vnlite-pl/main/pl.m3u` · report: `pl-report.json`

**Kiểm tra / Check:** each link must deliver a full live segment of real video (MPEG-TS with video PID, or fMP4) at ≥1.3× real time, and the live playlist must advance. `pl-local-ok.json` = result from a PC in Poland (`scripts/pl_scrape_local.ps1 -Publish`, valid 14 days): links failing from Poland are dropped even if the runner can play them; links failing only from the runner are kept with `#VNL-GEO:PL`. Max 2 sources per channel (HD and faster first).
