# Bản Đặc Tả Kiến Trúc & Kế Hoạch Thực Thi (Dành cho Claude Opus 4.8)

**Vai trò của tôi (Antigravity):** Giám sát Kiến trúc (Chief Architect)
**Người thực thi:** Claude Opus 4.8 (Full Output / Implementation Engineer)
**Dự án:** MH-Dowsample Pro (Phiên bản Thương mại Quốc tế)

---

## 1. Phân Tích Công Nghệ Mới Nhất (State-of-the-Art Tech Stack)

Dựa trên xu hướng công nghệ năm nay, để biến dự án từ một Python Script thành một siêu phẩm phần mềm âm nhạc có hiệu năng vô đối và giao diện đắt tiền, Claude phải áp dụng đúng Stack công nghệ sau:

### A. Tầng Frontend (Giao diện người dùng)
- **Tauri v2 (Rust-based Desktop):** Thay thế hoàn toàn Electron. Tauri giúp app nhẹ hơn 80%, không ăn RAM, kết nối trực tiếp với backend bằng Rust hoặc Python (thông qua sidecar).
- **Vite + React (TypeScript):** Tốc độ build siêu nhanh, strict type-checking.
- **UI/UX Framework:** Sử dụng **shadcn/ui** kết hợp **TailwindCSS**. Bắt buộc áp dụng triết lý "Premium Design" (Dark mode chuẩn phòng thu, Glassmorphism, Micro-interactions).
- **Motion & Animation:** Bắt buộc dùng **GSAP (GreenSock)** để làm các hiệu ứng mượt mà khi người dùng kéo thả file audio, render waveform theo thời gian thực.

### B. Tầng Backend & Xử Lý Âm Thanh (Core Engine)
- **Maturin & PyO3 (Rust + Python):** Giữ lại core Python hiện tại, nhưng yêu cầu Claude viết lại các hàm nghẽn cổ chai (nhận diện BPM, Key) bằng Rust, sau đó biên dịch thành thư viện Python. Tốc độ sẽ tăng gấp 10-50 lần so với dùng pure `librosa`.
- **Symphonia (Rust Audio Decoder):** Thay thế sự phụ thuộc vào `ffmpeg` cồng kềnh. Symphonia giải mã audio trực tiếp trong RAM với tốc độ chóng mặt.
- **Database (Semantic Search):** Nâng cấp SQLite hiện tại bằng cách tích hợp extension **`sqlite-vss`** (Vector Search). Cho phép người dùng tìm kiếm sample bằng mô tả (ví dụ: "punchy kick for techno").

---

## 2. Các "Skill" Bắt Buộc Claude Phải Load Trước Khi Code

Khi anh giao việc cho Claude, anh hãy đính kèm yêu cầu bắt buộc Claude phải sử dụng các **Superpowers Skills** sau (các kỹ năng này đã được định nghĩa chuẩn trong hệ thống):

1. **`full-output-enforcement` (Output Skill):** Bắt buộc Claude viết code hoàn chỉnh, không được dùng các đoạn placeholder như `// ... existing code ...` hoặc `// TODO`. Phải nhả full output.
2. **`ui-ux-pro-max` & `stitch-design-taste`:** Ép Claude thiết kế UI theo chuẩn agency cao cấp, cấm dùng các thiết kế rẻ tiền, nhàm chán. Yêu cầu font chữ đậm chất editorial, shadow sâu, padding rộng rãi.
3. **`test-driven-development`:** Trước khi Claude sửa `quality_gate.py` hoặc tạo API, Claude phải viết file Test trước.
4. **`brainstorming` & `writing-plans`:** Claude phải bám sát bản kế hoạch này và không được tự ý đi chệch hướng.

---

## 3. Kế Hoạch Giao Việc (Phân rã Task cho Claude)

Dưới đây là các Task anh sẽ copy và giao cho Claude làm từng bước một. (Đừng giao hết 1 lần, hãy giao từng Phase).

### Phase 1: Nâng cấp Core Engine bằng Rust (Performance Boost)
**Lệnh giao cho Claude:**
> "Claude, hãy đọc file `quality_gate.py`. Tao muốn mày dùng PyO3 và Maturin để viết lại hàm `_detect_key` và `_detect_bpm` sang Rust nhằm tăng tốc độ xử lý lên 50 lần. Không dùng librosa nữa mà tìm thư viện Rust tương đương (như `rustfft`). Đảm bảo full output, không viết tắt."

### Phase 2: Dựng Kiến Trúc Tauri (Desktop App Skeleton)
**Lệnh giao cho Claude:**
> "Claude, khởi tạo một project Tauri v2 bằng Vite + React + TypeScript. Cấu hình Backend sidecar gọi đến API của Python. Giao diện bắt buộc dùng shadcn/ui. Bật Dark Mode mặc định, tông màu Deep Charcoal. Thiết kế một màn hình Home có khu vực Drag-and-Drop siêu mượt bằng GSAP."

### Phase 3: Tích hợp Vector Database & Tìm kiếm thông minh
**Lệnh giao cho Claude:**
> "Nâng cấp hàm `organize.py` để ghi dữ liệu vào SQLite có kích hợp Vector Search. Thiết kế một hệ thống indexing để khi tao gõ 'dark synth loop', nó sẽ truy xuất ra đúng file audio nhanh nhất."

### Phase 4: Đóng gói và Cấp phép thương mại
**Lệnh giao cho Claude:**
> "Viết một module `licensing.rs` chạy ngầm trong Tauri để verify mã kích hoạt qua Ed25519 (Offline). Nếu pass, thì app mới mở khóa chức năng hàng loạt. Sau đó viết file CI/CD Github Actions để build ra file `.exe` cho Windows."

---

## 4. Open Questions (Phần Giám Sát Của Em)

> [!IMPORTANT]
> Em với tư cách là Giám đốc Kiến trúc, cần anh chốt:
> 1. Bộ công nghệ em vừa chọn (Tauri + Rust + React + GSAP) anh thấy đã đủ "đỉnh" và "bén" cho dự án âm nhạc này chưa?
> 2. Anh muốn em viết hẳn một file `.md` chứa toàn bộ Prompt chuẩn (đóng gói sẵn) để anh chỉ việc copy ném qua cho Claude chạy không? Đảm bảo Claude đọc phát là code ngay.
