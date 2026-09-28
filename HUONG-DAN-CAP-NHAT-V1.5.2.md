# Cập nhật MEXC P2P v1.5.2

## Lỗi đã sửa

- Không còn gửi chuỗi trạng thái `NOT_PAID,PAID` khiến MEXC trả về 0 lệnh.
- Bot đọc riêng các trạng thái: `NOT_PAID`, `PAID`, `WAIT_PROCESS`, `PROCESSING`, `DONE`.
- Lệnh `DONE` vẫn được giữ để SePay có thể đối chiếu giao dịch ngân hàng đến trước hoặc lệnh được đọc muộn.
- Khi lệnh MEXC đến sau, bot tự kiểm tra lại giao dịch SePay `UNMATCHED` gần đây có cùng số tiền.
- Đây vẫn là chế độ chỉ đọc, không tự release USDT.

## Cách cập nhật Render

1. Giải nén gói v1.5.2 và chép đè toàn bộ mã nguồn lên repository đang dùng.
2. Commit và push lên GitHub, sau đó chọn **Manual Deploy → Deploy latest commit** trên Render.
3. Mở `/docs`, kiểm tra phiên bản hiển thị **1.5.2**.
4. Mở Dashboard → **Nâng cao → Kết nối MEXC P2P** và nhấn một lần.
5. Kết quả đúng với lệnh hiện tại phải là `MEXC OK · nhận 1 lệnh · mới 1` (hoặc `cập nhật 1` nếu lệnh đã tồn tại).

Sau lần kiểm tra này, tiến trình nền sẽ tự quét theo `MEXC_P2P_SYNC_SECONDS`; không cần nhấn nút thủ công nữa.
