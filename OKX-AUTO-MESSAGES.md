# Tin nhắn tự động cho lệnh OKX

Tại `/dashboard#messages`, tab **Tin nhắn OKX** có hai cấu hình độc lập:

- **Mua USDT**: tài khoản của bạn chuyển VND cho người bán USDT.
- **Bán USDT**: khách chuyển VND cho bạn để mua USDT.

Nhập văn bản, chọn công tắc rồi bấm Lưu cho từng chiều. Tối đa 1.000 ký tự,
giữ xuống dòng và gửi nguyên văn; không có mã mẫu tự thay thế. Mặc định cả hai
chiều tắt và nội dung trống. Không cần thêm secret hay biến môi trường; dùng
API OKX và DASHBOARD_ADMIN_KEY hiện có. OKX vẫn phải cấp quyền gửi chat phù hợp.

Chỉ lệnh USDT/VND tạo sau thời điểm bật công tắc được nhận tin. Sửa nội dung
không gửi lại cho lệnh đã có hồ sơ; tắt rồi bật lại không nhắn bù lệnh cũ.
Mỗi lệnh tối đa một lần bắt đầu gửi từ tính năng này. Tin nhắn thủ công hoặc
tin tự động từ công cụ khác không thuộc bộ chống trùng này.

## Độ tin cậy và phạm vi

Worker chạy riêng với đối chiếu/mở khóa và bám giá. Phát hiện lệnh từ receipt
OKX đã đồng bộ gần đây, rồi đọc lại chi tiết xác thực trước khi gửi. Kiểm tra
đúng ID, chiều, USDT/VND, số tiền, lượng USDT và trạng thái lệnh. Lệnh đóng/hủy
hoặc cấu hình đã tắt/đổi được bỏ qua. Lỗi GET có thể đọc lại khi chưa gửi.

Bảng `okx_auto_message_config` lưu công tắc, nội dung và phiên bản từng chiều;
`okx_auto_message_deliveries` có khóa duy nhất theo order ID. Trạng thái bắt đầu
gửi được commit trước POST, dùng cập nhật có điều kiện để tránh hai worker gửi
trùng. Giãn ít nhất 2 giây giữa các lần bắt đầu gửi. Sau timeout, mất kết nối,
phản hồi không rõ hoặc process dừng giữa lần gửi, không tự gửi lại POST.

Nhật ký riêng hiển thị 50 tin gần nhất: chờ gửi, đang gửi, đã gửi, chưa rõ,
chưa gửi được hoặc bỏ qua. `SUBMITTING` quá 120 giây được hiển thị là chưa rõ;
người vận hành mở chat OKX kiểm tra. Không có nút xóa hồ sơ/gửi lại để bỏ qua
khóa chống trùng. Tắt công tắc không thu hồi được yêu cầu đã bắt đầu gửi.

Nút **Dừng toàn bộ Auto** cũng tắt hai công tắc tin nhắn, giữ nguyên nội dung.
Tính năng không xác nhận thanh toán, mở khóa USDT, tạo lệnh, đổi giá hoặc gửi
tin trên MEXC. Chỉ bật cho ứng dụng `app` của tài khoản chính; kayhap bị chặn
ở API và worker. Hai công tắc mặc định vẫn tắt sau triển khai.

## Kiểm thử

```shell
python -m pytest tests/test_okx_auto_messages.py tests/test_web_control.py -q
```

Kiểm thử dùng database riêng và HTTP mock, gồm chữ ký API, đúng nội dung từng
chiều, chống trùng sau restart/cancellation, lỗi GET/POST, thay cấu hình giữa
lần đọc, lệnh cũ/đã đóng, giới hạn tốc độ, xác thực admin và tách tài khoản.
Giao diện được kiểm tra bằng database bộ nhớ, không có worker gọi sàn.

Nguồn giao thức: tài liệu OKX P2P “API Specs - External (1).pdf”, mục 9.2:
POST `/api/v5/p2p/chat/send-message`, `orderId`, `content`, `contentType=101`;
chỉ ghi Đã gửi khi phản hồi `isSentSuccessfully` là boolean true.
