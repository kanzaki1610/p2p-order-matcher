# OKX P2P Read-only Bridge

Extension này chỉ đọc các trường công khai đang hiển thị trên trang OKX P2P: nickname, giá và hạn mức. Extension không đọc cookie, mật khẩu, API key, số dư hoặc thông tin lệnh; không click và không giao dịch.

## Cài đặt

1. Mở `chrome://extensions`.
2. Bật **Developer mode**.
3. Chọn **Load unpacked** và chọn thư mục `okx-p2p-extension`.
4. Trên Render, tạo biến `OKX_BROWSER_BRIDGE_SECRET` với một chuỗi ngẫu nhiên dài.
5. Mở Extension, nhập URL Render và đúng Bridge Secret.
6. Mở trang OKX P2P, chọn BUY hoặc SELL trong Extension và nhấn **Quét ngay**.
7. Kiểm tra dashboard; snapshot mới phải hiển thị trong phần **Dữ liệu OKX Extension**.

Nếu Extension báo không nhận diện được quảng cáo, hãy chụp toàn bộ trang P2P và phần lỗi Extension. Không gửi ảnh có cookie, API key, mật khẩu hoặc thông tin đăng nhập.

## Giới hạn

- Chrome và tab OKX P2P phải đang mở.
- Bộ nhận diện có thể cần cập nhật nếu OKX đổi giao diện hoặc ngôn ngữ.
- Đây không phải kết nối P2P API chính thức.
- Mọi giá chỉ là đề xuất DRY RUN; người dùng tự kiểm tra và thay đổi quảng cáo thủ công.
