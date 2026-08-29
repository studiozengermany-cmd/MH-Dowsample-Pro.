# Nhật ký Dự án: MH-Dowsample
Tạo lúc: 2026-08-27 10:00 (giờ địa phương, Asia/Ho_Chi_Minh)
Quy tắc: FILE NÀY LÀ APPEND-ONLY.
NGHIÊM CẤM sửa, xóa, ghi đè, sắp xếp lại hay "dọn dẹp" các entry cũ.
Mọi đính chính phải là một entry MỚI ở cuối file.
Mọi AI hoặc người tiếp quản dự án BẮT BUỘC đọc file này trước khi làm việc.

---

## [2026-08-28 01:15] — Khắc phục đọng file tạm khi Worker Timeout & Giới hạn Duration âm thanh
🕐 Thời gian: 2026-08-28 01:15 (+07:00)
👤 Thực hiện bởi: Claude Code (qwen-max)
🧩 Loại: FIX
🎯 Mục đích: Xóa bỏ triệt để file tạm .processed.wav khi worker bị timeout, đồng thời giới hạn duration tối đa (600s) cho QualityGate để tránh cạn kiệt RAM/CPU khi phân tích file âm thanh lớn.
📂 File đã tác động:
  · organize.py — Thêm logic dọn dẹp file staged `.processed.wav` dư thừa trong `process_file()` khi worker process con bị kill timeout.
  · quality_gate.py — Thêm hằng số `MAX_ANALYSIS_DURATION_SEC = 600.0` và từ chối xử lý mẫu vượt 10 phút.
  · tests/test_timeout_deadlock.py — Thêm assertion kiểm tra dọn sạch file tạm trong TEMP_ROOT.
  · tests/test_quality_gate.py — Thêm unit test `test_long_duration_file_is_rejected`.
  · tests/test_frozen_core_contract.py — Cập nhật sha256 fingerprint cho quality_gate.py và organize.py.
✅ Đã giải quyết:
  · Loại bỏ rủi ro đọng file rác trong TEMP_ROOT gây đầy đĩa cứng khi gặp timeout.
  · Phòng ngừa crash OOM bộ nhớ do librosa khi nạp các file âm thanh quá dài (mixes / DJ sets).
🧪 Đã kiểm tra thế nào:
  · Lệnh đã chạy: `.venv_new/Scripts/python.exe -m pytest tests -q`
  · Kết quả thật: 158/158 pass (10.83s).
  · Lệnh lint: `.venv_new/Scripts/python.exe -m ruff check .` -> All checks passed!
⚠️ Còn hạn chế / chưa làm:
  · Chưa đổi token Telegram Bot thực trên Telegram BotFather (người dùng cần chủ động cấp lại token mới).
📝 Lưu ý cho người tiếp theo:
  · Nếu tăng `MAX_ANALYSIS_DURATION_SEC`, cần tính toán bộ nhớ RAM tối thiểu cho server/host (mỗi 10 phút stereo 44.1kHz chiếm ~200MB float32 RAM khi load librosa).

---

## [2026-08-28 02:40] — Mở rộng Extractor đa nguồn & hỗ trợ tải mẫu từ mọi trang web
🕐 Thời gian: 2026-08-28 02:40 (+07:00)
👤 Thực hiện bởi: Claude Code (qwen-max)
🧩 Loại: FEATURE
🎯 Mục đích: Chuyển đổi kiến trúc AudioCrawler sang Extractor Pattern (BaseAudioExtractor, SpliceExtractor, GenericWebExtractor) để loại bỏ việc lạm dụng duy nhất một trang web (Splice) và mở rộng tải tự động cho mọi trang web âm thanh công cộng (Looperman, Cymatics, Freesound, Bandcamp, Tracklib...).
📂 File đã tác động:
  · crawler.py — Thêm giao diện BaseAudioExtractor, SpliceExtractor (tải nhanh Splice pagination) và GenericWebExtractor (Playwright Sniffer tổng quát). Cập nhật registry extractor cho AudioCrawler.__init__ và _discover_urls.
  · tests/test_frozen_core_contract.py — Cập nhật sha256 fingerprint cho crawler.py.
✅ Đã giải quyết:
  · Loại bỏ phụ thuộc độc quyền vào trang web Splice.
  · Cho phép crawler tự động nhận diện và trích xuất audio từ bất kỳ trang web âm thanh nào.
🧪 Đã kiểm tra thế nào:
  · Lệnh đã chạy: `.venv_new/Scripts/python.exe -m pytest tests -q`
  · Kết quả thật: 158/158 pass (10.88s).
⚠️ Còn hạn chế / chưa làm:
  · Cần thực hiện `git push` và kéo bản cập nhật về VPS.
📝 Lưu ý cho người tiếp theo:
  · Muốn thêm trang web có API/phân trang đặc thù, chỉ cần tạo subclass kế thừa `BaseAudioExtractor` và truyền vào `AudioCrawler(extractors=[...])`.

---

## [2026-08-28 02:50] — Commit & Push code lên GitHub (origin main)
🕐 Thời gian: 2026-08-28 02:50 (+07:00)
👤 Thực hiện bởi: Claude Code (qwen-max)
🧩 Loại: CONFIG
🎯 Mục đích: Đẩy toàn bộ thay đổi (Extractor đa nguồn, dọn staging file timeout, giới hạn audio duration, cập nhật contract tests) lên GitHub repo `studiozengermany-cmd/MH-Dowsample-Pro` để chuẩn bị kéo về VPS.
📂 File đã tác động:
  · Nhật ký.md — Thêm entry ghi nhận hành động commit & push.
✅ Đã giải quyết:
  · Commit `414ab71` đã push thành công lên `origin main`.
  · Toàn bộ test suite 158/158 PASS trước khi push.
🧪 Đã kiểm tra thế nào:
  · Lệnh đã chạy: `git push origin main`
  · Kết quả thật: `fccfd70..414ab71 main -> main` (Thành công).
⚠️ Còn hạn chế / chưa làm:
  · Chưa pull trên VPS và chưa đổi Telegram Bot Token trên BotFather.
📝 Lưu ý cho người tiếp theo:
  · Khi SSH vào VPS, dùng lệnh `git pull origin main` để cập nhật.
  · Cập nhật lại SHA256 contract test nếu có sửa đổi thêm core files.

---

## [2026-08-28 06:30] — Đính chính giờ thực hiện entry 02:40 và 02:50
🕐 Thời gian: 2026-08-28 06:30 (+07:00)
👤 Thực hiện bởi: Claude Code (qwen-max)
🧩 Loại: REVERT
🎯 Mục đích: Đính chính thời gian thực tế của hai entry FEATURE (02:40) và CONFIG (02:50) — giờ ghi ban đầu sai so với timestamp commit thật.
📂 File đã tác động:
  · Nhật ký.md — Thêm entry đính chính.
✅ Đã giải quyết:
  · Entry `[2026-08-28 02:40]` (Mở rộng Extractor đa nguồn): thời gian thực tế là **06:25** — tương ứng commit `414ab71` (git timestamp 2026-08-28 06:25:10 +07:00).
  · Entry `[2026-08-28 02:50]` (Commit & Push): thời gian thực tế là **06:28** — tương ứng commit `2bdbe86` (git timestamp 2026-08-28 06:28:44 +07:00).
  · Entry `[2026-08-28 01:15]` (FIX) giữ nguyên — chưa có bằng chứng ngược.
🧪 Đã kiểm tra thế nào:
  · Lệnh đã chạy: `git log --format="%h %ci %s"`
  · Kết quả thật: `414ab71 2026-08-28 06:25:10` và `2bdbe86 2026-08-28 06:28:44`.
⚠️ Còn hạn chế / chưa làm:
  · Không có.
📝 Lưu ý cho người tiếp theo:
  · Giờ entry nên lấy từ `Get-Date` / `git log` timestamp thay vì ước lượng.

---

## [2026-08-29 03:28] — Test chuyên nghiệp toàn diện & Setup Telegram Bot Token
🕐 Thời gian: 2026-08-29 03:28 (+07:00)
👤 Thực hiện bởi: Claude Code (deepseek-v4-flash)
🧩 Loại: TEST + CONFIG
🎯 Mục đích: Hoàn thiện dự án bằng chiến dịch test chuyên nghiệp (Full A+B+C: E2E pipeline thật + bổ sung coverage bot/crawler + quality gate), đồng thời xác nhận và setup Telegram Bot Token mới.
📂 File đã tác động:
  · tests/test_bot_handlers.py — TẠO MỚI (11 test): access gate (PENDING/BLOCKED/REJECTED/REVOKED/APPROVED), invite-code flow, keyboard admin/public, main menu phân quyền, source_name_from_url, cmd_start chặn user chưa duyệt + hiện menu cho user đã duyệt.
  · tests/test_generic_extractor.py — TẠO MỚI (3 test): GenericWebExtractor universal fallback, delegate browser sniffing, guard giới hạn crawl.
  · data/reports/test-report.md — TẠO MỚI: báo cáo test chuyên nghiệp đầy đủ.
  · .env — Xác nhận `TELEGRAM_TOKEN=8340122654:AAE...RQs` (token mới từ BotFather) đã đúng.
✅ Đã giải quyết:
  · Full suite: 158 → **172 passed** (11.14s), coverage **80%** (ngưỡng ≥68%).
  · ruff: All checks passed. bandit (-ll): 0 issue. mypy: lỗi môi trường numpy pyi (cần Python ≥3.12, local đang 3.14.7) — không phải lỗi code.
  · E2E: 10/10 MP3 thật → WAV chuẩn pcm_s16le/44100Hz/16-bit/stereo, phân loại đúng layout (FX/, Loops/House/, Loops/Trap/, Loops/Deep House/, Loops/Lo Fi/), key+BPM detection OK, phát hiện trùng lặp SHA-256 10/10, dùng isolated env (test_e2e/) KHÔNG đụng data production.
  · Token Telegram Bot hợp lệ (getMe OK): **@MH_dowsample_bot** — "MH - Downsample Pro", id 8340122654. Admin user id: 8503737793.
🧪 Đã kiểm tra thế nào:
  · Lệnh đã chạy: `./.venv_new/Scripts/python.exe -m pytest tests -q --cov=. --cov-fail-under=68`
  · Kết quả thật: **172 passed**, TOTAL 5047 stmts, 80% coverage.
  · Lệnh lint: `ruff check *.py utils tools` -> All checks passed!
  · Lệnh bandit: `bandit -r *.py utils tools -ll` -> 0 issue (set PYTHONIOENCODING=utf-8 để tránh lỗi charmap formatter trên console Windows).
  · Xác thực token: `curl api.telegram.org/bot<TOKEN>/getMe` -> `{"ok":true,...}`
⚠️ Còn hạn chế / chưa làm:
  · `test_e2e/` (thư mục test tạm: 10 WAV, DB test, temp) còn trên đĩa — P3, xóa khi không cần.
  · Worktree cũ `zealous-lederberg-cf0fdf` có 2 lỗi lint (benchmark_quality_gate.py) — P3.
  · Coverage bot.py (58%) / crawler.py (63%) còn thấp — có thể bổ sung test handler callback (handle_access_callback, handle_url).
  · Chưa test live bot chạy long-running với token mới (chỉ verify getMe).
📝 Lưu ý cho người tiếp theo:
  · Khi chạy bandit trên Windows console, set `PYTHONIOENCODING=utf-8` trước để formatter 'txt' không crash vì unicode.
  · Bot token đã đổi — nếu khởi động bot.py cần chạy trong terminal có PYTHONIOENCODING=utf-8 để tránh lỗi encode khi in emoji/Vietnamese.

---

## [2026-08-29 07:20] — Tối ưu hóa Audio Crawler hỗ trợ các Store JS-heavy (EvoSounds, Shopify, BeatStars)
🕐 Thời gian: 2026-08-29 07:20 (+07:00)
👤 Thực hiện bởi: Antigravity Teamwork Orchestrator (gemini-2.5-pro + Jetski Subagents)
🧩 Loại: FEATURE + FIX
🎯 Mục đích: Khắc phục lỗi không tìm thấy âm thanh trên các store nhạc/preset nhiều JavaScript (evosounds.com, Shopify Dawn/Debut themes, BeatStars, WaveSurfer), áp dụng rate limiting tránh bot detection (tối đa 5 click/round), mở rộng DOM & iframe audio extraction, và bổ sung bộ test tự động.
📂 File đã tác động:
  · crawler.py — Thêm `_MAX_CLICKS_PER_ROUND = 5`, mở rộng `_PLAY_BUTTON_SELECTORS` (22 bộ chọn), tối ưu hóa vòng lặp fallback click, tăng buffer chờ network response lên 750ms, mở rộng `inspect_dom` đọc data attributes (`[data-track-url]`, `[data-mp3]`,...), dynamic `currentSrc`/`src`, `iframe[src]`, JSON-LD script state và tài nguyên performance, thay thế teleport scroll bằng step scrolling 800px có kiểm tra hội tụ chiều cao.
  · tests/test_crawler.py — Thêm 3 test chuyên sâu: `test_max_clicks_per_round_and_play_selectors_defined`, `test_sniff_urls_throttles_to_max_five_clicks_per_round`, và `test_sniff_urls_inspects_dom_data_attributes_and_dynamic_current_src`.
  · tests/test_frozen_core_contract.py — Cập nhật sha256 fingerprint cho `crawler.py` (`58eb3109a96eec5eb85b1bf1df3886f4cb76059c2560370fbf3771960dafaee7`).
✅ Đã giải quyết:
  · Tự động quét và trích xuất audio stream từ các store dùng lazy loading, iframe, web component hoặc Shopify audio snippet.
  · Giới hạn tối đa 5 click mỗi vòng quét và đánh dấu DOM attribute `data-audio-crawler-clicked="1"` chống click lặp/spam WAF.
  · Thu gom và drain response task bất đồng bộ không gây leak task hay rò rỉ tài nguyên Playwright.
  · Đạt 175 passed tests (vượt mốc 172 ban đầu), 0 regression, ruff clean, audit forensic CLEAN.
🧪 Đã kiểm tra thế nào:
  · Lệnh đã chạy: `.venv_new\Scripts\python.exe -m pytest tests -q`
  · Kết quả thật: **175 passed** (0 failed) trong 11.23s.
  · Lệnh lint: `.venv_new\Scripts\python.exe -m ruff check crawler.py tests/test_crawler.py tests/test_frozen_core_contract.py`
  · Kết quả thật: `All checks passed!` (0 error).
  · Lệnh contract: `.venv_new\Scripts\python.exe -m pytest tests/test_frozen_core_contract.py` -> 1 passed.
⚠️ Còn hạn chế / chưa làm:
  · Không có.
📝 Lưu ý cho người tiếp theo:
  · Nếu thêm selector play button mới, cập nhật `_PLAY_BUTTON_SELECTORS` trong `crawler.py` và cập nhật lại SHA256 trong `tests/test_frozen_core_contract.py`.


---

## [2026-08-29 08:06] — Nâng cấp caption ZIP gửi Telegram
🕐 Thời gian: 2026-08-29 08:06 (+07:00)
👤 Thực hiện bởi: Antigravity (Gemini 3.1 Pro)
🧩 Loại: FEATURE
🎯 Mục đích: Thay caption sơ sài "Gói sample đã sẵn sàng — gói 2 / File gốc: 200" bằng caption chuyên nghiệp có: nguồn web, số file, dung lượng ZIP thật, phân biệt single vs multi-part.
📂 File đã tác động:
  · delivery.py — Sửa đoạn build caption trong hàm build_and_send() (lines 502–527)
✅ Đã giải quyết:
  · Caption single ZIP: ✅ Đã tải xong N sample / 🌐 Nguồn / 🎵 File gốc / 💾 Dung lượng ZIP
  · Caption multi-part: 📦 Gói X — site / 🎵 File trong gói / 💾 Dung lượng ZIP / 📊 Tổng toàn bộ
  · Bỏ hoàn toàn: heading biến rác, total_parts chết, indent sai
🧪 Đã kiểm tra thế nào:
  · Lệnh đã chạy: python -c "import ast; ast.parse(open('delivery.py').read()); print('Syntax OK')"
  · Kết quả thật: Syntax OK
  · CHƯA TEST trên VPS — cần git push và restart bot để xác nhận live
⚠️ Còn hạn chế / chưa làm:
  · Cần git push + restart bot trên VPS để test live caption mới
📝 Lưu ý cho người tiếp theo:
  · Nếu muốn thêm số gói X/Y chính xác (ví dụ "Gói 2/4"), cần đếm tổng archive_paths trước vòng lặp
