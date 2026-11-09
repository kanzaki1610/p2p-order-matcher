# Tự khôi phục bám giá MEXC trên Discord

1. Bấm **Kiểm tra/khôi phục Mua** hoặc **Kiểm tra/khôi phục Bán**, chọn slot.
2. Nếu yêu cầu cũ khớp dữ liệu sàn, bot xác minh và bỏ khóa, giữ Auto tắt.
   Nếu quảng cáo đã đổi, bot đọc hai lần cấu hình hiện tại và mở bản xem trước
   để nhận lại cấu hình khi hồ sơ cũ hợp lệ. Có thể mở trực tiếp từ **Nhận cấu hình**.
3. Kiểm tra ID, giá, USDT khả dụng, tổng USDT, Min/Max và hiển thị; bấm
   **Xác nhận nhận cấu hình**. Hồ sơ cũ được lưu vào nhật ký; không khẳng định
   yêu cầu cũ đã thành công. Bot không gửi lại yêu cầu và không sửa quảng cáo.
4. Sau xác nhận, nếu quảng cáo đang OPEN, bot hiện **Bật Auto … slot …**.
   Chủ tài khoản tự bấm khi sẵn sàng. Nếu đang CLOSE/LOW_STOCK, bot chỉ rõ cần
   xử lý/mở quảng cáo trên MEXC trước, không tự mở quảng cáo bị đóng thủ công.

Khóa cũ do PRICE, SHOW hoặc HIDE đều được xem xét; riêng SHOW/HIDE phải có
hồ sơ khớp đúng quảng cáo/chiều/lần gửi, không kèm thay đổi lượng/giá/hạn mức,
và đã qua ít nhất 10 phút. Không nhận lại khi yêu cầu còn SUBMITTING, hồ sơ
thiếu/mâu thuẫn, đang có USDT bị giữ, ngân sách Mua chưa xác minh được hoặc
quảng cáo thay đổi giữa các lần đọc. Nút trả lý do và có **Kiểm tra lại**.

Xác nhận chỉ dùng một lần, đúng chủ tài khoản/schema, hết hạn sau 5 phút và
bị vô hiệu khi cấu hình, dữ liệu sàn, yêu cầu cũ hoặc sổ ngân sách thay đổi.
Sổ tiền đã chi của Mua được giữ nguyên. Khôi phục không chuyển tiền, mở khóa
USDT, tự bật Auto, sửa giá hoặc thử lại POST cũ.

Không cần biến môi trường hoặc quyền Discord mới. Bản sửa áp dụng trong
module MEXC theo schema tài khoản hiện có; không thay đổi luồng OKX.
