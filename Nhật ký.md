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
