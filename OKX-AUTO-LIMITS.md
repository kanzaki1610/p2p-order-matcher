# Hạn mức khi bám giá OKX

Trong kênh điều khiển OKX, dùng `/gia bang` để lấy bảng nút mới.
Chọn **Hạn mức bán Min/Max** hoặc **Hạn mức mua Min/Max**, nhập slot, Min
và Max VND cho mỗi giao dịch. Đây là hạn mức giao dịch, không phải giá USDT.
Ví dụ Min `20000000`, Max `180000000`.

Lưu cấu hình tắt Auto của slot, không gửi lệnh sửa quảng cáo ngay.
Kiểm tra giá vốn, vị trí và quảng cáo, sau đó bật Auto của slot khi sẵn sàng.
Yêu cầu cũ UNKNOWN vẫn phải được xác minh riêng trước khi bật Auto.

Khi chạy, bot gửi `unitPrice`, `minOrderLimit`, `maxOrderLimit` cùng một
yêu cầu cập nhật. Với SELL, Max thực tế là số nhỏ hơn giữa Max đã nhập và
`floor(availableAmount × unitPrice)`. Nếu số tiền này dưới Min, bot báo lỗi
và không sửa quảng cáo. Max đã nhập được giữ làm trần cho các lần sau.
Hạn mức chỉ gắn với đúng quảng cáo của slot và được chuyển sang ID mới
sau khi xác minh thành công việc thay quảng cáo của OKX.

Bot lưu hạn mức đã gửi để kiểm tra lại sau khởi động. Chỉ báo thành công
khi giá, chiều, trạng thái và Min/Max trả về đều đúng; không tự gửi lại
yêu cầu có kết quả chưa rõ. Không thay đổi quy tắc mở khóa USDT hay MEXC.

Không cần biến môi trường mới. Hai bảng mới `okx_auto_limits` và
`okx_pending_limits` được tạo khi ứng dụng khởi động.
