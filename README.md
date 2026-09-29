# RCR Annotation — bản tối giản

Ứng dụng độc lập, không thay đổi mã nguồn hay PostgreSQL của web Annotate cũ. Chỉ dùng Python 3.11+ (thư viện chuẩn), SQLite và trình duyệt; không cần `npm install`.

## Chạy lần đầu

```bash
cd rcr-simple
python3 server.py init-admin --username admin
# Nhập mật khẩu tối thiểu 4 ký tự khi chương trình hỏi.

# Trỏ tới thư mục *images* có các thư mục con train/val/test/…
export RCR_IMAGE_ROOT=/duong/dan/Amazing/Datasets/PIPA/images
python3 server.py serve --host 127.0.0.1 --port 8090
```

Mở `http://127.0.0.1:8090`. Có thể đặt DB riêng bằng `--db /duong/dan/rcr-data.sqlite3` ở **cả hai** lệnh. Nếu ảnh không hiện, kiểm tra `RCR_IMAGE_ROOT` và đường dẫn ảnh tương đối trong file import. Bộ mã và gói bàn giao không chứa ảnh PIPA.

SQLite lưu task, user, bản nháp và lịch sử. DB mặc định nằm cạnh `server.py`, kể cả khi chạy lệnh từ thư mục khác; đường dẫn DB thực tế được in lúc khởi động. Mỗi khi một annotation được **nộp** (hoặc import/giao lại làm thay đổi những annotation đã nộp), server ghi lại file `rcr-data-final.jsonl` ở cạnh DB bằng thay thế nguyên tử; tên file thay đổi theo tên DB, ví dụ `--db /data/review.sqlite3` → `/data/review-final.jsonl`. Khi khởi động, server tạo lại JSONL từ DB. File chỉ gồm các mẫu đang ở trạng thái `SUBMITTED`, mỗi dòng đúng 8 trường của output bên dưới. Gợi ý LLM chưa áp dụng và bản nháp chưa nộp không có trong file này. Giữ và backup cả DB nếu cần khôi phục user, bản nháp và lịch sử.

Để cho nhiều người truy cập: đặt server sau reverse proxy HTTPS, bật `RCR_COOKIE_SECURE=1` và cấu hình xác thực, backup, giới hạn truy cập của máy chủ phù hợp. Không phơi cổng HTTP trực tiếp ra Internet.

## Import dữ liệu của bạn

File `samples.jsonl` chứa 4.311 mẫu đã viết nhưng thiếu đường dẫn ảnh/box; `pair_data.json` chứa đường dẫn ảnh và box. Tạo file import đúng schema bằng:

```bash
python3 convert_dataset.py \
  --pair-data /duong/dan/pair_data.json \
  --samples /duong/dan/samples.jsonl \
  --all-pairs \
  --out /duong/dan/rcr-import.jsonl
```

Số lượng đầu ra phụ thuộc hai file đầu vào; xem thống kê `written` và `skipped` do converter in ra. Annotation cũ hợp lệ được nhập ở trạng thái `SUBMITTED` (không gán user). Dùng `--existing-mode unassigned` nếu **chủ ý** muốn giao lại các mẫu đã có câu; tùy chọn này bỏ trạng thái đã nộp trong bản import mới. Bỏ `--all-pairs` nếu chỉ muốn nhập các mẫu có trong `samples.jsonl`.

Trong giao diện admin: chọn file → **Kiểm tra file** → xem lỗi/trùng → **Import hợp lệ**. `sample_id` đã có được bỏ qua; import có lỗi sẽ không ghi một phần. File import là JSONL (mỗi dòng một task) hoặc mảng JSON, dưới 30 MB; hỗ trợ UTF-8 BOM. Nếu row có `subjects`, `final_desc`, `final_change` thì nhãn được kiểm tra và nhập thành bài đã nộp. Có thể dùng `imported_annotation` theo mẫu cũ. Không nhập file export 8 trường trực tiếp vì file đó không có đường dẫn ảnh và box. `example-task.jsonl` là ví dụ cấu trúc; ảnh ví dụ không kèm theo.

Task tối thiểu gồm `sample_id`, `query_image_id`, `target_image_id`, `target_image_ids` (có thể có nhiều positive), `query_image_path`, `target_image_path`, `query_boxes`, `target_boxes`. Các box dùng tọa độ chuẩn hóa `[0,1]` và `identity_id` giống nhau ở cả hai ảnh. `case_type` thuộc `INDIVIDUAL / GROUP / DUAL / RELATIONAL`; có thể đổi khi gán nhãn.

## Quy trình

Admin có một bảng task chung và một trang Users: thêm user, import, lọc, chọn task, giao/thu hồi, export. Trang Users hỗ trợ tạo từng người hoặc tạo nhiều người một lần: nhập mỗi dòng `username,password`, ví dụ `annotator01,my-long-password-01`. Nếu mật khẩu chứa dấu phẩy, đặt mật khẩu trong dấu ngoặc kép theo CSV. Tối đa 200 người mỗi lần; sai một dòng hoặc trùng tên đã tồn tại thì cả lô không được tạo. Mật khẩu tối thiểu 4 ký tự.

Bảng thống kê Users tính trên toàn bộ task, không phụ thuộc bộ lọc/trang của danh sách task: **Đã giao** là số task hiện giao cho user (kể cả đã nộp); **Chưa bắt đầu** là `ASSIGNED`; **Đang làm** là `IN_PROGRESS`; **Đã nộp hiện tại** là các annotation cuối ở trạng thái `SUBMITTED` do chính user đó nộp. **Đã từng hoàn thành** là số `sample_id` khác nhau user đã nộp, gồm cả kết quả đã được lưu vào `annotation_history` khi giao lại; một task nộp lại nhiều lần chỉ tính một lần cho cùng user. Tiến độ = Đã nộp hiện tại / Đã giao (0% nếu chưa được giao). Annotation import cũ không có `author_id` không được cộng cho bất kỳ user nào. Giao lại task đang làm/đã nộp đòi xác nhận rõ ràng; bản cũ được giữ trong bảng `annotation_history` của DB. Nên backup DB định kỳ, đặc biệt trước khi giao lại số lượng lớn.

Annotator có một hàng đợi cá nhân. Màn hình làm bài gồm hai ảnh (zoom/pan, box đồng bộ theo identity), chọn case và Subject, một ô SELECT cho `INDIVIDUAL/GROUP` hoặc hai ô cho `DUAL/RELATIONAL`, **một ô TARGET** cho mọi case, bản xem trước chỉ đọc, lưu nháp tự động và nút nộp. `GROUP` tự xác định khi S1 có ít nhất hai identity; `DUAL/RELATIONAL` vẫn cho phép một Subject là nhóm.

Khi export, ứng dụng tự tạo:

```text
final_desc        = Identify Subject 1 as … [and Subject 2 as …]
final_change      = then retrieve target images where …
final_instruction = final_desc + "; " + final_change + "."
```

Output JSONL chỉ gồm `sample_id, case_type, query_image_id, target_image_ids, subjects, final_desc, final_change, final_instruction`. Export chỉ lấy task `SUBMITTED`; bộ lọc case/split/annotator/search của bảng được áp dụng, nhưng bộ lọc trạng thái không thể xuất draft như kết quả cuối. `final_change` **có** tiền tố “then retrieve…” giống cả 4.311 mẫu hiện hành; annotator chỉ viết phần thân trong UI.

Validation kiểm tra số Subject, số identity, identity không bị dùng hai lần, ô SELECT/TARGET không trống và nhắc đúng S1/S2. Hướng quan hệ và độ đúng hình ảnh **vẫn do annotator xác minh**; validator/LLM không chứng minh được hai điều này.

## LLM tùy chọn

Thiết lập `RCR_GEMINI_API_KEY` và `RCR_GEMINI_MODEL` trong môi trường chạy server (không lưu key trong file import hoặc mã nguồn). `RCR_GEMINI_MODEL` là tên model Gemini, không có tiền tố `models/`. Chọn identity cho từng Subject, rồi bấm **Fix with LLM**. Nút gọi Gemini `generateContent` với hai ảnh, case, box, Subject và nội dung đang viết; Gemini gợi ý cả SELECT lẫn TARGET. Máy chủ kiểm tra cấu trúc và quy tắc case, không cho LLM đổi Subject/identity. Gợi ý chỉ hiển thị tạm thời; tải lại trang sẽ mất. Bấm **Áp dụng và lưu nháp** sau khi kiểm tra hai ảnh để đưa text vào bản nháp SQLite; chỉ khi bấm **Nộp** mới có dòng trong file JSONL cuối. Nếu không cấu hình LLM, annotator vẫn làm thủ công bình thường. Mỗi lần bấm nút có thể phát sinh chi phí API; hãy kiểm tra quyền sử dụng và chính sách dữ liệu ảnh trước khi bật.

## Kiểm thử và bảo trì

```bash
python3 -m unittest discover -s tests -v
node --check static/app.js   # nếu có Node
node --test tests/test_ui.cjs
```

SQLite lưu các bảng `users`, `sessions`, `tasks`, `annotations`, `annotation_history`. Ứng dụng này không tự di chuyển dữ liệu của PostgreSQL/Prisma cũ; muốn chuyển kết quả lịch sử khác ngoài `samples.jsonl` cần export và chuyển sang định dạng import trước. Để backup đơn giản, dừng server rồi sao chép file SQLite (nếu server vẫn chạy thì dùng SQLite backup API để có bản nhất quán). Không xóa DB cũ trước khi so sánh số lượng và export.


## Cập nhật bản ổn định 2026-09-28

Dùng `rcr-simple-update.zip` nếu server hiện tại đã có dữ liệu mới hơn file ZIP bạn gửi.
Dừng server trước, giải nén gói cập nhật sang thư mục riêng rồi chạy:

```bash
python3 /duong/dan/goi-cap-nhat/apply_update.py /duong/dan/rcr-simple
```

Nếu dùng DB riêng, thêm `--db /duong/dan/review.sqlite3`.
Chương trình sao lưu các file code bị thay thế và toàn bộ SQLite trước khi cập nhật;
không chép đè DB hay JSONL kết quả. Đường dẫn backup được in khi hoàn tất.
Sau đó chạy lại lệnh `serve` bình thường và tải lại trang trên các trình duyệt.

Bản `rcr-simple.zip` đầy đủ chứa dữ liệu tại thời điểm bạn gửi để mở độc lập.
Không chép DB trong bản đầy đủ lên server đang có bài làm mới hơn.

## Khôi phục nháp và xử lý lỗi

- Khi gõ, nội dung chưa lưu được giữ dự phòng trên trình duyệt theo tài khoản/task.
  Khi có bản dự phòng, tải lại task sẽ hiện lựa chọn tải nháp, khôi phục hoặc giữ bản server.
  Gợi ý Gemini chưa áp dụng không được lưu vào bản dự phòng.
- Đăng xuất và chuyển task đợi lưu xong. Mất mạng thì giữ nguyên nội dung để thử lại.
- Nếu báo xung đột, bấm **Tải nháp** nếu cần giữ một bản riêng, rồi **Tải lại task** để
  đối chiếu bản server. Việc khôi phục nội dung cũ là lựa chọn của người làm.
- Nếu server đã lưu nhưng trình duyệt mất phản hồi, gửi lại đúng nội dung/revision cũ
  được chấp nhận và không tạo thêm bài nộp.
- **Tải nháp** là file phục hồi để đọc/đối chiếu, không phải file import task.
- Bản dự phòng trình duyệt không thay thế backup SQLite. Nếu trình duyệt chặn lưu trữ,
  UI báo rõ; hãy giữ tab mở cho tới khi thấy **Đã lưu nháp**.
- Nếu JSONL cuối không ghi được, phản hồi báo dữ liệu đã lưu trong SQLite nhưng xuất
  file thất bại; server ghi lỗi vào stderr. Sửa dung lượng/quyền ghi rồi khởi động lại
  để tạo lại JSONL, hoặc dùng Export để tải kết quả từ DB.

## Kiểm tra và backup

```bash
python3 server.py check
python3 server.py backup --out backups/rcr-2026-09-28.sqlite3
# DB riêng:
python3 server.py backup --db /data/review.sqlite3 --out /data/backups/review-2026-09-28.sqlite3
```

`check` chỉ đọc DB và kiểm tra integrity/foreign keys. `backup` dùng SQLite backup API,
lấy cả dữ liệu đã commit trong WAL khi server đang chạy; từ chối ghi đè file backup có sẵn.
Nên backup mỗi ngày có gán nhãn và trước khi giao lại nhiều task. Lưu thêm bản backup
trên ổ/máy khác; JSONL chỉ chứa kết quả đã nộp, không chứa tài khoản và nháp.

Khi phục hồi: dừng server, giữ một bản sao thư mục dữ liệu hiện tại, rồi dùng DB backup
ở **tên file mới** qua `--db`. Không chép riêng DB đè lên một bộ DB/WAL đang chạy.

## Phạm vi kiểm tra bản này

- 18 kiểm thử Python: HTTP, đăng nhập/phân quyền/CSRF, import, draft/submit, revision,
  thống kê, lịch sử, xuất file, Gemini giả lập và backup.
- 10 kiểm thử JavaScript về trạng thái và bất đồng bộ: mất mạng, hết phiên, đổi task,
  tạo user, khôi phục nháp, cập nhật trong lúc đang lưu, response cũ và lỗi server.
- Mô phỏng 30 bài nộp qua 6 kết nối đồng thời; snapshot trùng export và không mất bài.
- Kiểm tra toàn bộ 4.689 task và 4 annotation trong DB bàn giao: không có lỗi cấu trúc;
  integrity OK, không có lỗi foreign key.

Chưa kiểm tra bằng trình duyệt đồ họa đầy đủ trong môi trường kiểm tra này; các kiểm thử
JavaScript dùng DOM giả lập, không xác minh bố cục/zoom/pan bằng ảnh chụp màn hình.
Không có ảnh PIPA hoặc API key trong gói gửi, nên chưa xác minh đường dẫn ảnh thực tế
và một lần gọi Gemini thật trên máy triển khai. Nhãn đúng ảnh và hướng quan hệ vẫn
cần annotator kiểm tra.
# rcr-simple
