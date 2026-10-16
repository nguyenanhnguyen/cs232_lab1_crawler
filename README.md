# CS232 — Lab 1

Ứng dụng thu thập bài viết và bình luận, lưu SQLite và theo dõi tiến độ bằng giao diện web cục bộ.

## Cài đặt và chạy

Yêu cầu Python 3.11 trở lên. Mở terminal tại thư mục `cs232_lab1`:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m playwright install chromium
python app.py
```

Truy cập **http://127.0.0.1:8787**, chọn collection, nhập URL và mở trang. Đăng nhập trong Chromium nếu cần, sau đó bấm bắt đầu crawl trên giao diện. Dừng ứng dụng bằng `Ctrl+C`.

Sau khi cài đặt, có thể dùng `cs232-lab1` hoặc tên lệnh tương thích `pagecrawl` thay cho `python app.py`.

```powershell
python app.py ui --port 9000
python app.py run --url "https://www.facebook.com/groups/ten-nhom" --collection "Lab 1"
python app.py comments --collection "Lab 1" --min-comments 2
python app.py report --url "https://www.facebook.com/groups/ten-nhom"
```

Lệnh `run` cũng mở web để xác nhận bắt đầu sau bước đăng nhập. Lấy bình luận là tác vụ riêng, có thể thao tác trên web hoặc bằng lệnh `comments`. Dùng `--help` sau mỗi lệnh để xem tùy chọn.

## Giao diện ba cột

- Bên trái: URL, chế độ trình duyệt, bắt đầu/dừng, collection và thao tác bình luận.
- Ở giữa: số liệu, tìm kiếm, danh sách bài và bình luận đã lưu.
- Bên phải: nhật ký cập nhật trực tiếp.

Mỗi khung cuộn dọc độc lập. Khi màn hình hẹp hơn 900 px, cuộn ngang để xem đủ ba cột. Dừng lấy bài trước khi lấy bình luận vì hai tác vụ dùng chung profile Chromium. Các nút đổi/xóa collection được khóa khi đang thu thập.

Bài gặp lỗi khi mở hoặc đọc bình luận được giữ để thử lại ở lần chạy sau. Tiến độ bình luận cập nhật trong lúc chạy; danh sách bài được tải lại khi tác vụ kết thúc.

## Các tệp chính

| Tệp | Nội dung |
| --- | --- |
| `app.py` | Lệnh terminal, API, sự kiện tiến độ và quản lý phiên |
| `data.py` | Mô hình bài viết/bình luận, SQLite, JSONL và trạng thái |
| `extract.py` | Đọc payload, chuẩn hóa trường dữ liệu, gộp bài trùng |
| `crawler.py` | Điều khiển trình duyệt, lấy bài và bình luận |
| `dashboard.html` | Giao diện ba cột với bảng chức năng, danh sách bài và nhật ký |

Mặc định dữ liệu được tạo trong thư mục làm việc hiện tại:

- `data/crawler.db`: bài viết, bình luận, collection và lịch sử chạy.
- `data/posts.jsonl`: thêm một dòng khi lưu hoặc cập nhật bài, giữ định dạng xuất cũ.
- `data/profile/`: phiên đăng nhập Chromium.

Có thể dùng `--db` và `--profile` để chọn đường dẫn khác. Muốn tiếp tục dữ liệu cũ, trỏ các tùy chọn này đến dữ liệu của bản gốc; đóng phiên dùng cùng profile trước khi chạy.

Không kèm trang giả, lệnh `mock` hay bộ test. Định dạng dữ liệu xuất và quy tắc trích xuất vẫn tương thích với bản gốc; nhánh xử lý lỗi bình luận và khóa tác vụ đã được sửa. Kết quả crawl trực tiếp còn phụ thuộc dữ liệu trang và phiên đăng nhập tại thời điểm chạy.
