# Muse2API — gói chạy macOS

Gói mã nguồn dùng Python trên máy, dành cho Mac Apple Silicon và Intel. Máy cần Python 3.10 trở lên và Google Chrome hoặc Microsoft Edge. Đây không phải ứng dụng `.app` chứa sẵn Python.

1. Giải nén `muse2api-macos.zip` vào thư mục có quyền ghi, ví dụ `~/Applications/muse2api`.
2. Bấm đúp `START.command`. Lần đầu cần mạng để cài thư viện.
3. Nếu không mở được bằng cách bấm đúp, mở Terminal tại thư mục vừa giải nén rồi chạy `bash START.command`.
4. Trang quản trị tự mở khi máy chủ sẵn sàng. Giữ cửa sổ Terminal và trang quản trị mở khi chạy hàng loạt. Nhấn Ctrl+C để dừng.
5. Nhập tài khoản Muse qua tiện ích trong thư mục `extension`: mở `chrome://extensions` hoặc `edge://extensions`, bật chế độ nhà phát triển, chọn tải tiện ích đã giải nén, rồi đăng nhập Muse và nhập cookie.

Gói tự tạo `.env` với API key ngẫu nhiên và địa chỉ localhost. Base URL của tiện ích là `http://localhost:18610`; Base URL cho ứng dụng OpenAI-compatible là `http://localhost:18610/v1`.

Nếu trình duyệt không được tìm thấy, đặt đường dẫn trong `.env`, ví dụ:

```dotenv
MUSE2API_CHROMIUM=/Applications/Google Chrome.app/Contents/MacOS/Google Chrome
```

Chọn thư mục lưu video trong trang quản trị bằng Chrome hoặc Edge. Sau khi mở lại trình duyệt, nếu cần cấp lại quyền, bấm nút tên thư mục hoặc “Cho phép mở video đã lưu”.

Khi cập nhật, dừng phiên đang chạy và giữ riêng `.env`, `data`, `browser-profiles`. Gói phát hành không chứa API key, tài khoản, cookie hoặc video cá nhân.
