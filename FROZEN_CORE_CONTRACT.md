# Contract lõi tải và xử lý sample

Tài liệu này khóa ranh giới giữa lõi đã được xác nhận hoạt động và lớp giao kết quả. Mọi thay đổi sau Chùm 0 phải bắt đầu ở phía delivery của ranh giới này.

## Điểm giao duy nhất

Điểm giao hiện tại là lời gọi sau trong `AudioBot.handle_url`:

```python
await self._send_processed_files(update, results, site)
```

Lõi chịu trách nhiệm tạo xong `results` và lưu file vào thư viện trước lời gọi này. Delivery chỉ được đọc kết quả; lỗi delivery không được xóa, di chuyển, đổi tên hoặc sửa file trong thư viện và kho raw.

## Contract đầu ra

`results` là danh sách giữ nguyên thứ tự các file đã tải được. Mỗi phần tử là một mapping có các trường theo trạng thái:

| Trường | Ý nghĩa | Trạng thái áp dụng |
| --- | --- | --- |
| `status` | `passed`, `duplicate`, `rejected` hoặc `error` | Luôn có |
| `file` | Đường dẫn file nguồn đã được đưa vào xử lý | Luôn có |
| `output` | Đường dẫn tuyệt đối tới WAV đã sắp xếp trong thư viện | `passed`; `duplicate` khi SQLite có bản ghi hiện hữu |
| `source_hash` | SHA-256 của file nguồn | Có sau khi bước hash thành công |
| `analysis` | Metadata phân tích chất lượng, loại sample, BPM, key và thể loại | `passed`, `rejected` |
| `raw` | Đường dẫn nguồn đã lưu trong kho raw | Được `handle_url` bổ sung khi lưu raw thành công |
| `issues` | Danh sách lý do loại | `rejected` |
| `error` | Mô tả lỗi xử lý nội bộ | `error` |

`site` là nguồn catalogue đã chuẩn hóa từ URL người dùng và được truyền riêng bên cạnh `results`. Contract hiện tại không đưa URL catalogue gốc vào từng phần tử kết quả.

Delivery chỉ được coi `passed` và `duplicate` có `output` tồn tại là file có thể giao. `rejected` và `error` không phải file tải về.

## Phạm vi đóng băng

Các file production sau được khóa toàn bộ:

- `crawler.py`: khám phá URL và tải file gốc.
- `quality_gate.py`: kiểm tra chất lượng, phân loại và phân tích audio.
- `processor.py`: chuẩn hóa WAV và ghi tag.
- `organizer.py`: đặt tên, chống trùng, sắp xếp thư viện và ghi SQLite.
- `organize.py`: điều phối `process_file` và contract kết quả.

Trong `bot.py`, toàn bộ symbol `AudioBot.handle_url` được khóa. Vùng delivery được phép phát triển từ `AudioBot._send_processed_files` và các helper/module delivery mà hàm này gọi.

`tests/test_frozen_core_contract.py` lưu dấu vân tay nội dung (đã chuẩn hóa xuống dòng) cho các file/symbol bị khóa. Test thất bại khi vùng lõi thay đổi là hành vi có chủ đích; chỉ cập nhật dấu vân tay khi có phê duyệt rõ ràng để mở khóa lõi.

## Mở khóa an toàn có chủ đích

Ngày 2026-07-16, `crawler.py` và `quality_gate.py` được mở khóa giới hạn để kiểm tra redirect trước khi kết nối, dừng crawl Splice đúng timeout và không trả kết quả catalogue bị cắt. Thay đổi này không đổi contract `results`, thứ tự pipeline, file thư viện/raw hoặc điểm giao delivery. `AudioBot.handle_url` vẫn giữ nguyên fingerprint.

## Kiểm tra trước mỗi chùm tiếp theo

1. Xem diff và xác nhận không file/symbol đóng băng nào bị chạm.
2. Chạy toàn bộ test, Ruff và type check cho module mới.
3. Chạy test thủ công riêng của chùm.
4. Báo cáo file đã đổi và dừng chờ xác nhận.
