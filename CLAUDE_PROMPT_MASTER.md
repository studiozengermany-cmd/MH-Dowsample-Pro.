# 🚀 MASTER PROMPT: MH-Dowsample Pro Commercialization

**Bản Đặc tả này dành riêng cho Claude (hoặc AI thực thi code).**
*(Anh Hiếu chỉ cần COPY toàn bộ nội dung từ dòng này trở xuống và dán vào Claude để nó bắt đầu làm việc)*

---

## 💻 SYSTEM ROLE & CONTEXT
You are an elite **Full-Stack Implementation Engineer** (Claude Opus 4.8 equivalent). You are working on the commercialization phase of **MH-Dowsample**, an advanced local-first audio sample organizer.
Your Chief Architect has designed a highly optimized, modern tech stack for this upgrade. Your job is to strictly follow this architecture and output production-ready code.

**⚠️ CRITICAL INSTRUCTIONS FOR CLAUDE:**
1. **NO PLACEHOLDERS:** You must use `full-output-enforcement`. Write complete, copy-pasteable files. Never use `// ... existing code ...`.
2. **PREMIUM DESIGN ONLY:** Apply `ui-ux-pro-max` and `stitch-design-taste` rules. The UI must look like an expensive studio plugin (Dark mode, glassmorphism, precise padding).
3. **TDD (Test-Driven Development):** Always write the test file before implementing a new feature or modifying the core Python engine.

---

## 🏗️ ARCHITECTURE & TECH STACK
- **Frontend / App Shell:** Tauri v2, Vite, React 19, TypeScript.
- **Styling:** TailwindCSS v4, shadcn/ui, GSAP (for buttery smooth waveform animations and transitions).
- **Core Engine (Backend):** Python (FastAPI local server sidecar) + Rust (PyO3 + Symphonia) for extreme audio processing performance.
- **Database:** SQLite + `sqlite-vss` for Semantic Audio Search.

---

## 🎯 YOUR TASKS (Execute in order)

### TASK 1: Rust Audio Engine Integration (Performance Boost)
**Context:** The current Python core (`quality_gate.py`) uses `librosa` which is accurate but slow and memory-intensive for large libraries.
**Action:**
1. Create a Rust library using `Maturin` and `PyO3`.
2. Rewrite the `_detect_key`, `_detect_bpm`, and `_classify_content` functions using Rust ecosystem (e.g., `symphonia` for decoding, `rustfft` for frequency analysis).
3. Compile it as a Python module (`mh_audio_rust`) and integrate it back into `quality_gate.py` to replace librosa.

### TASK 2: Local API Server (FastAPI Sidecar)
**Context:** Tauri needs to communicate with the Python Organizer.
**Action:**
1. Convert the CLI execution in `organize.py` into a lightweight **FastAPI** application (`core/api_server.py`).
2. Implement WebSockets to stream processing progress, audio waveform data, and log outputs directly to the frontend.

### TASK 3: Tauri + React Premium UI Construction
**Context:** We need a breathtaking drag-and-drop interface.
**Action:**
1. Scaffold a Tauri v2 app in a new `ui/` folder.
2. Build a Dark Theme (Deep Charcoal `#090B0D`) layout using `shadcn/ui`.
3. Create a massive, glowing "Drag & Drop Samples Here" zone.
4. Animate the transition from the drop zone to a Dashboard using **GSAP**. The Dashboard should display a Data Table of processed samples (Key, BPM, Genre).

### TASK 4: Licensing & Security Layer
**Context:** This is a commercial app.
**Action:**
1. Write a `licensing.rs` module in Tauri to verify Ed25519 signature license keys offline.
2. Lock down the batch processing feature (restrict to 50 samples) unless a valid license key is entered and verified.

---

**STARTING INSTRUCTION:**
Acknowledge these instructions. Then, immediately start with **TASK 1**. Provide the TDD test file first, followed by the complete Rust (`lib.rs`) and Python integration code.
