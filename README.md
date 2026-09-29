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

SQLite lưu task, user, bản nháp và lịch sử. DB mặc định nằm cạnh `server.py`, kể cả khi chạy lệnh từ thư mục khác; đường dẫn DB thực tế được in lúc khởi động. Mỗi khi một annotation được **nộp**, mở lại, import/giao lại hoặc sửa email làm thay đổi kết quả cuối, server ghi lại file `rcr-data-final.jsonl` ở cạnh DB bằng thay thế nguyên tử; tên file thay đổi theo tên DB, ví dụ `--db /data/review.sqlite3` → `/data/review-final.jsonl`. Khi khởi động, server tạo lại JSONL từ DB. File chỉ gồm các mẫu thỏa đồng thời `tasks.status = SUBMITTED` và `annotations.submitted = 1`, theo schema output bên dưới. Gợi ý LLM chưa áp dụng và bản nháp chưa nộp không có trong file này. Giữ và backup cả DB nếu cần khôi phục user, bản nháp và lịch sử.

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

Trong giao diện admin: chọn file → **Kiểm tra file** → xem lỗi/trùng → **Import hợp lệ**. `sample_id` đã có được bỏ qua; import có lỗi sẽ không ghi một phần. File import là JSONL (mỗi dòng một task) hoặc mảng JSON, dưới 30 MB; hỗ trợ UTF-8 BOM. Nếu row có `subjects`, `final_desc`, `final_change` thì nhãn được kiểm tra và nhập thành bài đã nộp. Có thể dùng `imported_annotation` theo mẫu cũ. Nếu một annotation cũ có `annotator_email` hợp lệ ở top level, email provenance đó được giữ nguyên; email không đi kèm annotation sẽ bị từ chối. Không dùng file export làm source task mới vì export không chứa `target_image_ids` và candidate metadata cần cho annotation UI. `example-task.jsonl` là ví dụ cấu trúc; ảnh ví dụ không kèm theo.

Task tối thiểu gồm `sample_id`, `query_image_id`, `target_image_id`, `target_image_ids` (có thể có nhiều positive), `query_image_path`, `target_image_path`, `query_boxes`, `target_boxes`. Các box dùng tọa độ chuẩn hóa `[0,1]` và `identity_id` giống nhau ở cả hai ảnh. `case_type` thuộc `INDIVIDUAL / GROUP / DUAL / RELATIONAL`; có thể đổi khi gán nhãn.

## Quy trình

Admin có một bảng task chung và một trang Users: thêm user, đặt/cập nhật email, import, lọc, chọn task, giao/thu hồi và export. Khi tạo từng annotator, nhập riêng username, email và password. Tạo hàng loạt ưu tiên mỗi dòng `username,email,password`; dạng cũ `username,password` vẫn được chấp nhận để database/quy trình cũ không bị hỏng, nhưng email sẽ để trống cho tới khi admin cập nhật. Nếu mật khẩu chứa dấu phẩy, đặt mật khẩu trong dấu ngoặc kép theo CSV. Tối đa 200 người mỗi lần; sai một dòng hoặc trùng username/email thì cả lô không được tạo. Mật khẩu tối thiểu 4 ký tự. Username không bao giờ được suy luận thành email.

Bảng thống kê Users tính trên toàn bộ task, không phụ thuộc bộ lọc/trang của danh sách task: **Đã giao** là số task hiện giao cho user (kể cả đã nộp); **Chưa bắt đầu** là `ASSIGNED`; **Đang làm** là `IN_PROGRESS`; **Đã nộp hiện tại** là các annotation cuối ở trạng thái `SUBMITTED` do chính user đó nộp. **Đã từng hoàn thành** là số `sample_id` khác nhau user đã nộp, gồm cả kết quả đã được lưu vào `annotation_history` khi giao lại; một task nộp lại nhiều lần chỉ tính một lần cho cùng user. Tiến độ = Đã nộp hiện tại / Đã giao (0% nếu chưa được giao). Annotation import cũ không có `author_id` không được cộng cho bất kỳ user nào. Giao lại task đang làm/đã nộp đòi xác nhận rõ ràng; bản cũ được giữ trong bảng `annotation_history` của DB. Nên backup DB định kỳ, đặc biệt trước khi giao lại số lượng lớn.

Annotator có một hàng đợi cá nhân. Màn hình làm bài gồm hai ảnh (zoom/pan, box đồng bộ theo identity), chọn case và Subject, một ô SELECT cho `INDIVIDUAL/GROUP` hoặc hai ô cho `DUAL/RELATIONAL`, **một ô TARGET** cho mọi case, bản xem trước chỉ đọc, lưu nháp tự động và nút nộp. `GROUP` tự xác định khi S1 có ít nhất hai identity; `DUAL/RELATIONAL` vẫn cho phép một Subject là nhóm. Task `SUBMITTED` hiển thị nội dung hiện hành ở chế độ chỉ đọc và có nút **Sửa lại**. Nút này đưa task về `IN_PROGRESS`, giữ nguyên nội dung/assignee/`started_at`, xóa `submitted_at`, bỏ task khỏi final snapshot và cho phép lưu nháp, Fix with LLM rồi nộp lại. Sau khi nộp lại, UI giữ nguyên task vừa sửa thay vì tự chuyển sang task tiếp theo. Mọi lần lưu/nộp cập nhật cùng một row trong `annotations`; chỉ admin force reassign mới đưa bản cũ vào `annotation_history`.

Khi export, ứng dụng tự tạo:

```text
final_desc        = Identify Subject 1 as … [and Subject 2 as …]
final_change      = then retrieve target images where …
final_instruction = final_desc + "; " + final_change + "."
```

Output JSONL có đúng các field theo thứ tự: `sample_id, split, annotator_email, query_image_id, query_image_path, query_boxes, target_image_id, target_image_path, target_boxes, case_type, subjects, final_desc, final_change, final_instruction`. `target_image_id` là số ít; boxes và subjects vẫn là array/object JSON, không stringify. Export chỉ lấy task `SUBMITTED` có annotation `submitted = 1`; bộ lọc case/split/annotator/search của bảng được áp dụng, nhưng bộ lọc trạng thái không thể xuất draft như kết quả cuối. `annotator_email` lấy từ email riêng của author; annotation import không có author dùng provenance đã import. Nếu dữ liệu cũ thực sự không có email thì field này là `null`, không thay bằng username. `final_change` **có** tiền tố “then retrieve…” giống dữ liệu hiện hành; annotator chỉ viết phần thân trong UI.

Validation kiểm tra số Subject, số identity, identity không bị dùng hai lần, ô SELECT/TARGET không trống và nhắc đúng S1/S2. Hướng quan hệ và độ đúng hình ảnh **vẫn do annotator xác minh**; validator/LLM không chứng minh được hai điều này.

## LLM tùy chọn

Thiết lập `RCR_GEMINI_API_KEY` và `RCR_GEMINI_MODEL` trong môi trường chạy server (không lưu key trong file import hoặc mã nguồn). `RCR_GEMINI_MODEL` là tên model Gemini, không có tiền tố `models/`. Chọn identity cho từng Subject và **phải tự gạch ý trước cho mọi ô SELECT và TARGET** rồi mới dùng **Fix with LLM**. Bản nháp có thể là câu ngắn, fragment, tiếng Việt, tiếng Anh hoặc trộn cả hai.

Gemini được dùng như **language editor**, không phải annotation generator hay visual verifier. Request LLM **chỉ gửi text** gồm case type, các bản nháp SELECT/TARGET và ghi chú tùy chọn; **không gửi QUERY/TARGET image, bounding box, đường dẫn ảnh hay identity ID**. Gemini được yêu cầu dịch nội dung tiếng Việt sang **tiếng Anh**, sửa grammar/phrasing và chuẩn hóa format RCR, nhưng phải giữ nguyên ý, Subject role, hướng quan hệ, hành động, vật thể, thuộc tính và mức độ chi tiết của bản nháp; không được tự suy đoán hay thêm visual fact mới. `thinkingLevel` dùng `low` cho Gemini 3; model có tên bắt đầu bằng `gemini-2.5-` dùng `thinkingBudget: 1024` vì API 2.5 không nhận `thinkingLevel`. Cần chọn model hỗ trợ generateContent + JSON response schema. Xem [tài liệu Google](https://ai.google.dev/gemini-api/docs/generate-content/thinking).

Máy chủ từ chối gọi LLM nếu thiếu bất kỳ SELECT/TARGET nào, đồng thời kiểm tra cấu trúc và quy tắc case, không cho output làm hỏng contract Subject. Gợi ý chỉ hiển thị tạm thời; tải lại trang sẽ mất. Annotator phải tự đối chiếu QUERY/TARGET, sau đó bấm **Áp dụng và lưu nháp** nếu đồng ý; chỉ khi bấm **Nộp** mới có dòng trong file JSONL cuối. `RCR_IMAGE_ROOT` vẫn cần cho UI hiển thị ảnh, nhưng **không còn là dependency của Fix with LLM**. Nếu không cấu hình LLM, annotator vẫn làm thủ công bình thường. Mỗi lần bấm nút có thể phát sinh chi phí API.

## Kiểm thử và bảo trì

```bash
python3 -m unittest discover -s tests -v
node --check static/app.js   # nếu có Node
node --test tests/test_ui.cjs
```

SQLite lưu các bảng `users`, `sessions`, `tasks`, `annotations`, `annotation_history`, `task_completions`. Bảng `task_completions` chỉ ghi nhận cặp task/người đã từng nộp, giúp thống kê không bị giảm khi mở lại bài; annotation hiện hành vẫn chỉ có một row. Cột `tasks.reopened` giữ chế độ Nộp lại qua các lần lưu nháp, tải lại trang hoặc đổi trình duyệt. Giao lại task sẽ xóa chế độ này. Khi khởi động, migration cộng thêm `users.email` và trường provenance `annotator_email` trong annotation hiện hành/lịch sử nếu DB cũ chưa có; user hiện hữu vẫn đăng nhập được và admin có thể bổ sung email sau. Ứng dụng này không tự di chuyển dữ liệu của PostgreSQL/Prisma cũ; muốn chuyển kết quả lịch sử khác ngoài `samples.jsonl` cần export và chuyển sang định dạng import trước. Để backup đơn giản, dừng server rồi sao chép file SQLite (nếu server vẫn chạy thì dùng SQLite backup API để có bản nhất quán). Không xóa DB cũ trước khi so sánh số lượng và export.


## Cập nhật bản audit 2026-09-29

Gói `rcr-server-audit-patch.zip` chỉ chứa file code thay đổi, test và tài liệu.
Xem `DEPLOY.md` để cập nhật và cấu hình reverse proxy/systemd trên Linux.
Dừng server, backup DB hiện hành, chép đè các file trong thư mục `rcr-simple/`
của gói vào thư mục ứng dụng tương ứng rồi khởi động lại bằng đúng `--db` cũ.
Migration tự chạy khi `serve`; không cần và không được khởi tạo DB mới thay cho DB đang dùng.
Tải lại các tab sau khi cập nhật. ZIP không chứa SQLite, WAL, JSONL kết quả, ảnh hoặc API key.

Chi tiết lỗi và phạm vi kiểm tra nằm trong `AUDIT.md`.

## Khôi phục nháp và xử lý lỗi

- Khi gõ, nội dung chưa lưu được giữ dự phòng trên trình duyệt theo tài khoản/task/từng tab. Một tab lưu xong không xóa nháp chưa lưu của tab khác. Các bản nháp của phiên bản cũ vẫn có thể khôi phục; nếu có nhiều bản, giao diện lần lượt hiện bản mới nhất còn khác server.
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

`check` chỉ đọc DB và kiểm tra integrity, foreign keys cùng invariant status/annotation. `backup` dùng SQLite backup API,
lấy cả dữ liệu đã commit trong WAL khi server đang chạy; từ chối ghi đè file backup có sẵn.
Nên backup mỗi ngày có gán nhãn và trước khi giao lại nhiều task. Lưu thêm bản backup
trên ổ/máy khác; JSONL chỉ chứa kết quả đã nộp, không chứa tài khoản và nháp.

Khi phục hồi: dừng server, giữ một bản sao thư mục dữ liệu hiện tại, rồi dùng DB backup
ở **tên file mới** qua `--db`. Không chép riêng DB đè lên một bộ DB/WAL đang chạy.

## Phạm vi kiểm tra bản này

- 29 kiểm thử Python: HTTP, đăng nhập/phân quyền/CSRF, migration, import,
  draft/submit/reopen, revision, thống kê, lịch sử, snapshot/export, Gemini giả lập và backup.
- 18 kiểm thử JavaScript: mất mạng, hết phiên, đổi task, tạo user, nháp nhiều tab,
  khôi phục nháp cũ, response chậm, nộp lại trên trình duyệt mới và export.
- Bộ test Python có mô phỏng 30 bài nộp qua 6 kết nối đồng thời, snapshot trùng export.
- Đã kiểm tra 4.689 task và 2 annotation trong DB đính kèm; không phát hiện lỗi cấu trúc.
  Migration hai lần trên bản sao giữ nguyên toàn bộ field dữ liệu cũ, integrity OK,
  không có lỗi foreign key. Database gốc không bị chỉnh sửa.

JavaScript được kiểm tra bằng DOM giả lập, chưa chạy trình duyệt đồ họa thật.
Chưa có ảnh PIPA và API key nên ảnh được thử qua HTTP bằng ảnh PNG tạm,
Gemini được giả lập; chưa xác minh ảnh thật, lời gọi Gemini thật hoặc HTTPS của server triển khai.
Xem các bước kiểm tra sau triển khai trong `DEPLOY.md`.
