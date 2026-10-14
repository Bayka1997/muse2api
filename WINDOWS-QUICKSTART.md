# Muse2API — gói chạy Windows

1. Giải nén vào ổ còn nhiều dung lượng, ví dụ `F:\Muse2API`.
2. Máy cần Python 3.10 trở lên và Google Chrome hoặc Microsoft Edge.
3. Bấm đúp `START.bat`. Lần đầu cần mạng để cài thư viện; các lần sau dùng lại môi trường đã cài.
4. Trang quản trị tự mở khi máy chủ sẵn sàng. Giữ cửa sổ terminal mở khi sử dụng.
5. Nhập tài khoản Muse: mở `chrome://extensions` (hoặc `edge://extensions`), bật chế độ nhà phát triển, tải tiện ích từ thư mục `extension`, đăng nhập Muse rồi nhập cookie qua tiện ích.

Gói tự tạo `.env` với khóa ngẫu nhiên, địa chỉ localhost và 2 tác vụ đồng thời. Xem khóa trong `.env` khi cấu hình tiện ích. Base URL của tiện ích là `http://localhost:18610`; Base URL cho ứng dụng OpenAI-compatible là `http://localhost:18610/v1`.

Nhấn Ctrl+C để dừng. Bấm lại `START.bat` để chạy lại. Nếu cổng 18610 đang được dùng, dừng phiên cũ trước hoặc sửa `MUSE2API_PORT` trong `.env`.

Mã nguồn, môi trường `.venv`, cache cài thư viện `.pip-cache`, dữ liệu `data` và profile `browser-profiles` nằm cạnh file khởi động. Python và trình duyệt vẫn sử dụng một phần ổ hệ thống; ổ C cần có dung lượng trống.

Gói không chứa tài khoản, cookie, API key hay video của máy tạo gói. Nhập tài khoản một lần sau khi cài. Giữ riêng `.env`, `data` và `browser-profiles` khi cập nhật; không chia sẻ các thư mục này.

Đây là gói mã nguồn có trình khởi động, không phải EXE chứa sẵn Python. Docker và WSL không cần thiết.
