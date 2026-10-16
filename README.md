# CS232 Lab 1 — Thu thập bài viết và bình luận

Ứng dụng thu thập bài viết, ảnh, số lượt thích, số bình luận và bình luận từ trang có quyền truy cập. Dữ liệu được lưu trong SQLite; giao diện web hiển thị bài viết, thao tác thu thập và nhật ký theo thời gian thực.

## Chạy ứng dụng

Yêu cầu Python 3.11 trở lên. Tại thư mục dự án:

```powershell
python -m pip install -e .
python -m playwright install chromium
python app.py ui --port 8787
```

Mở `http://127.0.0.1:8787`, nhập URL, đăng nhập trong Chromium nếu cần rồi bấm **Bắt đầu crawl**. Có thể chọn collection, tìm bài, xem hoặc lấy bình luận trên giao diện. Dữ liệu mặc định lưu tại `data/crawler.db`; profile trình duyệt lưu tại `data/profile/`.

Các lệnh terminal khác:

```powershell
python app.py run --url "https://www.facebook.com/groups/ten-nhom"
python app.py comments --collection "mặc định" --min-comments 2
python app.py report --url "https://www.facebook.com/groups/ten-nhom"
```

## Dữ liệu EDA

Tạo lại báo cáo từ database đã thu thập:

```powershell
python eda.py --db ..\data\crawler.db --out eda
```

Nếu chạy ứng dụng tại thư mục dự án và dùng đường dẫn mặc định, lệnh ngắn gọn là `python eda.py`. Thư mục `eda/` chứa `summary.json` (thống kê tổng quan), `posts_features.csv` (đặc trưng từng bài), `comments_features.csv` (đặc trưng từng bình luận) và `posts_by_month.csv` (số bài theo tháng đăng). Hai bảng đặc trưng dùng khóa băm để nối bài viết với bình luận, không xuất tên tác giả hay nội dung văn bản.

| Chỉ tiêu từ lần xuất hiện tại | Số lượng |
| --- | ---: |
| Bài viết | 10.168 |
| Bình luận đã lưu | 35.423 |
| Bài có nội dung văn bản | 9.904 |
| Bài có ít nhất một ảnh | 3.648 |
| Bài có bình luận theo số liệu trên trang | 6.048 |
| Bài đã hoàn tất lấy bình luận | 4.412 |
| Bình luận trả lời (độ sâu trên 0) | 14.584 |

`summary.json` còn có phân bố lượt thích, độ sâu bình luận, độ dài văn bản, số ảnh mỗi bài và trạng thái các lượt thu thập. Dữ liệu `created_at` được gom theo tháng; bài thiếu ngày đăng được xếp vào `unknown`. Số bình luận trang hiển thị và số bình luận lưu được là hai chỉ tiêu khác nhau.
