# Hai tài khoản: OKX + MEXC, SePay và Discord

Bản chuẩn bị 09/10/2026 dựa trên main 724e21c, giữ toàn bộ sửa lỗi MEXC PR7.
Dùng cùng một service Render và PostgreSQL hiện tại. Tài khoản gốc giữ bảng
public; kayhap dùng schema kayhap và role p2p_kayhap với cấu hình, vòng đọc,
đối chiếu và mở khóa riêng. Không dùng chung API giữa hai người.

## Quy tắc

Giống tài khoản gốc: số tiền chính xác, nội dung chứa mã lệnh hoặc đầy đủ họ tên
(cho phép đảo thứ tự), đúng ngân hàng, chỉ một lệnh phù hợp. Hỗ trợ ACB, OCB,
MBBank. Thiếu dữ liệu, không khớp hoặc tranh chấp gửi kênh manual-review.
MEXC chờ trạng thái đã thanh toán và ngân hàng đã chọn; không gửi lặp yêu cầu
mở khóa khi chưa rõ kết quả. Các lỗi đã sửa của PR7 áp dụng cho cả hai người.

## Cấu hình chờ API

Tên biến nằm trong `.env.kayhap.example`. Thêm vào Environment service hiện tại.
Mọi biến bạn bạn bắt đầu KAYHAP_; không kế thừa secret tài khoản gốc.
OKX: API key, secret, passphrase. MEXC: API key, secret.
Nhập trực tiếp trên Render, không gửi qua chat hoặc lưu GitHub.
Chưa có API: giữ reader và auto release kayhap ở false.

Năm webhook riêng: KAYHAP_DISCORD_WEBHOOK_ORDERS → p2p-orders;
PAYMENT_DETECTED → payment-detected; REVIEW_REQUIRED → manual-review;
CONFIRMED → confirmed; SYSTEM_ALERTS → system-alerts. Các tên sau cũng có
prefix KAYHAP_DISCORD_WEBHOOK_.

## Chuẩn bị database và triển khai

1. Sao lưu database; kiểm tra gói/hạn sử dụng thực tế trên Render.
2. Chạy scripts/provision_shared_account.py để preview. --apply dùng DATABASE_URL
   và KAYHAP_DATABASE_PASSWORD trong môi trường, tạo role/schema hạn chế trong
   cùng database. Script không sửa bảng gốc; dừng nếu role/schema đã tồn tại.
3. KAYHAP_DATABASE_URL trỏ cùng database nhưng role p2p_kayhap, mật khẩu riêng.
   Xác minh role không đọc/ghi public và current_schema() là kayhap.
4. SePay, ingest, dashboard dùng ba khóa khác nhau, tối thiểu 32 ký tự, riêng
   với người gốc. Cần đủ năm webhook trước khi MULTI_ACCOUNT_ENABLED=true.
5. Start command: uvicorn app.shared:app --host 0.0.0.0 --port $PORT.
   Chỉ một worker. MULTI_ACCOUNT_ENABLED=false giữ ứng dụng gốc hoạt động.
6. Khi có API: kiểm tra health, Discord, SePay và đọc lệnh. Chỉ bật auto riêng
   sau khi kiểm tra kết nối, đối chiếu và quyền API thực tế.

## URL kayhap

- /accounts/kayhap/dashboard
- /accounts/kayhap/webhooks/sepay
- /accounts/kayhap/health
- /accounts/kayhap/discord/test-notification

Không trỏ SePay kayhap vào /webhooks/sepay gốc. Endpoint dùng khóa riêng;
prefix không thay thế xác thực.

## Trạng thái

207 kiểm thử mô phỏng đạt: gồm cả bộ hiện tại, cùng mã lệnh/giao dịch trong hai
namespace, từ chối khóa chéo, thông báo riêng, client và coordinator của hai sàn
dùng cấu hình/model/connection riêng. Chưa triển khai bản hai tài khoản, chưa
xác minh role PostgreSQL thực tế và chưa có API/giao dịch thật của kayhap.
Rollback: MULTI_ACCOUNT_ENABLED=false rồi deploy; giữ schema/role kayhap.
