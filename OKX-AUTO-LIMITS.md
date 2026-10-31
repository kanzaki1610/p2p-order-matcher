# Hạn mức khi bám giá OKX

Trong kênh điều khiển OKX, dùng `/gia bang` để lấy bảng nút mới.
Chiều mua USDT chỉ bám giá (`unitPrice`), không gửi hoặc thay đổi Min/Max,
kể cả slot từng lưu hạn mức mua trước đây. Giữ nguyên hạn mức quảng cáo mua trên OKX.
Nút hạn mức mua cũ chỉ hiển thị hướng dẫn, không lưu cấu hình hay tắt Auto.

Với chiều bán, chọn **Min bán · Max tự động**, nhập slot và Min VND cho mỗi
giao dịch. Không cần nhập Max. Nếu chưa lưu Min riêng cho quảng cáo hiện tại,
bot giữ Min đang có trên OKX.

Lưu cấu hình tắt Auto của slot, không gửi lệnh sửa quảng cáo ngay.
Kiểm tra giá vốn, vị trí và quảng cáo, sau đó bật Auto của slot khi sẵn sàng.
Yêu cầu cũ UNKNOWN vẫn phải được xác minh riêng trước khi bật Auto.

Khi chạy chiều bán, bot gửi `unitPrice`, `minOrderLimit`, `maxOrderLimit` cùng một
yêu cầu cập nhật. Với SELL, Max = `floor(availableAmount × unitPrice) - 1` VND.
Ví dụ 3000 USDT × 26500 → Max 79499999; giá giảm về 26100 → Max 78299999.
Max tăng/giảm theo giá mới và số USDT còn khả dụng, không lấy số lượng ban đầu
hoặc Max cũ làm trần. Các Max mua/bán từng lưu trước đây không dùng làm giới hạn
cho luồng bám giá mới. Nếu Max tính được dưới Min hoặc không xác minh được
lượng USDT khả dụng, bot báo lỗi và không sửa quảng cáo.
Hạn mức chỉ gắn với đúng quảng cáo của slot và được chuyển sang ID mới
sau khi xác minh thành công việc thay quảng cáo của OKX.

Bot lưu hạn mức đã gửi để kiểm tra lại sau khởi động. Chỉ báo thành công
khi giá, chiều, trạng thái và Min/Max trả về đều đúng; không tự gửi lại
yêu cầu có kết quả chưa rõ. Không thay đổi quy tắc mở khóa USDT hay MEXC.

Không cần biến môi trường mới. Hai bảng mới `okx_auto_limits` và
`okx_pending_limits` được tạo khi ứng dụng khởi động.

## Kiểm tra và khôi phục slot

Trong bảng nút OKX, bấm **Kiểm tra/khôi phục Mua** hoặc **Kiểm tra/khôi phục Bán**,
rồi chọn slot. Bot tắt Auto của slot trước khi đọc lại quảng cáo bằng API GET.
Bot không sửa/ẩn quảng cáo, không tự gửi lại yêu cầu và không tự bật Auto.

Nếu đã có ID kết quả, bot kiểm tra đúng chủ sở hữu, USDT/VND, chiều, giá,
trạng thái và Min/Max đã gửi trước khi ghi nhận hoàn tất. Với yêu cầu tự bám giá
cũ bị từ chối HTTP 400/mã 55723 và không có ID kết quả, chỉ gỡ khóa nếu quảng cáo
cũ vẫn hoạt động, giữ đúng giá trước yêu cầu (khác giá đã yêu cầu) và trạng thái
hiển thị đúng. Yêu cầu phải đã qua ít nhất 120 giây; không xử lý SUBMITTING.

Timeout, lỗi kết nối, 429, lỗi khác hoặc dữ liệu không khớp vẫn giữ khóa.
Kết quả và toàn bộ thông tin yêu cầu cũ được lưu vào nhật ký trước khi chuyển
slot về IDLE; giữ Min đã nhập và thời điểm gửi cũ để không bỏ qua giới hạn tần suất.
Nếu cấu hình/yêu cầu đổi trong lúc đọc OKX, bot không ghi đè. Sau khi khôi phục,
operator tự kiểm tra Min/Max, giá vốn, vị trí rồi bật Auto khi sẵn sàng.
