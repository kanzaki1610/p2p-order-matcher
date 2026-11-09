# QR thanh toán lệnh Mua USDT · chỉ kanzaki

Luồng đọc riêng cho **lệnh Mua USDT/VND đang chờ bạn thanh toán** trên OKX
và quảng cáo Mua maker MEXC của tài khoản gốc. Không thay đổi đối chiếu tiền
vào, mở khóa USDT, bám giá hoặc cấu hình kayhap. Không chuyển tiền và không
gọi API đánh dấu đã thanh toán. Không có nút xác nhận thanh toán trong kênh QR.

## Cấu hình

```dotenv
BUY_PAYMENT_QR_ENABLED=true
DISCORD_BUY_PAYMENT_CHANNEL_ID=<private channel in the primary guild>
BUY_PAYMENT_QR_SECONDS=15
```

Sử dụng `DISCORD_BOT_TOKEN`, `DISCORD_APPLICATION_ID`, `DISCORD_GUILD_ID`,
`DISCORD_ALLOWED_USER_IDS` và API đọc OKX/MEXC hiện có. Không tạo webhook mới.
Không thêm tiền tố `KAYHAP_`: feature có cả kiểm tra package và schema để chỉ
chạy trong tài khoản gốc. Mặc định tắt. Kênh phải riêng tư, `@everyone` bị
chặn View Channel, các quyền cho xem trực tiếp chỉ dành cho operator hoặc bot.
Bot cần View Channel, Send Messages, Embed Links, Attach Files và Read Message
History. Quản trị viên Discord vẫn có quyền quản trị theo cơ chế của Discord.

## Cách dùng

1. Có người bán vào quảng cáo Mua của bạn: bot đọc thông tin lệnh từ sàn.
2. Kênh riêng hiển thị sàn, mã lệnh, ngân hàng, số tài khoản giữ nguyên số 0
   đầu, tên người nhận, số tiền, mã lệnh gợi ý làm nội dung và ảnh QR PNG.
3. Mở lệnh trên sàn, đối chiếu thông tin và nội dung người bán yêu cầu, sau đó
   tự quét trong ứng dụng ngân hàng. Tên hiển thị trên bot không thay thế bước
   xác minh người thụ hưởng trong ứng dụng ngân hàng.
4. Sau khi thanh toán thực tế, tự thao tác trên sàn. Bot không bấm thay bạn.

Một thông báo cho mỗi lệnh; dữ liệu thay đổi cập nhật cùng tin nhắn. Khi
hủy/hoàn tất/đã báo thanh toán/hết hạn, bot bỏ ảnh QR và thay bằng trạng thái.
Lỗi đọc hoặc dữ liệu mâu thuẫn cũng bỏ QR cũ khi Discord nhận được bản cập
nhật. Có thời gian đọc API trên thông báo; luôn mở lệnh trên sàn trước khi
thanh toán, đặc biệt nếu bot/mạng/Discord đang gián đoạn.

## Quy tắc kiểm tra

- OKX: chỉ `buy`, USDT/VND, `new`, `unpaid`, không frozen/tranh chấp. Lấy
  tài khoản thu của **đối tác** qua `counterpartyDetail.takerPaymentMethod`
  hoặc GET `/api/v5/p2p/order/counterparty-user-info`. Kiểm tra cùng user ID,
  phương thức BANK/VND không bị vô hiệu. Không lấy tài khoản maker của mình.
- MEXC: dùng danh sách maker; chiều API đối ứng cấu hình luồng bán hiện tại.
  GET chi tiết đúng ID và GET quảng cáo của chính mình phải có chiều BUY.
  Chỉ `NOT_PAID`, không tranh chấp và còn hạn thanh toán. Lấy `paymentInfo`
  hoặc `confirmPaymentInfo` khớp đúng phương thức trong danh sách.
- Nhiều tài khoản chưa được chọn, tài khoản bị che, tên chủ tài khoản không
  khớp tên người bán, thiếu dữ liệu hoặc ngân hàng chưa xác minh BIN: báo cần
  kiểm tra, không tự chọn tài khoản đầu tiên và không đoán số tài khoản.
- Chỉ khám phá lệnh được tạo trong 24 giờ, kiểm tra lại lệnh đã theo dõi kể cả
  khi nó biến mất khỏi danh sách pending. Các lệnh quá 24 giờ bỏ QR.
- Đọc lại chi tiết ngay trước khi gửi QR mới/đã đổi. Tài khoản/tiền/trạng thái
  thay đổi giữa hai lần đọc sẽ chặn gửi QR.

## QR và dữ liệu

VietQR/NAPAS IBFT (`A000000727`, `QRIBFTTA`, VND 704, CRC16/CCITT-FALSE)
được tạo cục bộ bằng `qrcode[pil]`. Không gửi số tài khoản/tiền/tên tới dịch vụ
ảnh QR và không đặt chúng vào URL công khai. Chỉ tải ảnh vào kênh Discord đã
được cấp quyền. Danh sách 23 ngân hàng được đánh dấu hỗ trợ trong dữ liệu công
khai SePay/VietQR ngày 09/11/2026 được đóng gói tại `app/vietqr_banks.json`.
Ngân hàng ngoài danh sách cần bổ sung/xác minh BIN trước khi tạo QR.

Bảng `buy_payment_qr_notifications` chỉ lưu mã lệnh, kênh/message ID, trạng
thái và fingerprint; không lưu tài khoản người nhận hay ảnh QR trong bảng này.
Nếu mất kết quả gửi Discord, bot đối chiếu nonce của tin nhắn, không gửi lặp
mù. Nếu không tìm lại được trong 100 tin gần nhất, cần kiểm tra vận hành.
Không ghi khóa/API URL có chữ ký hay dữ liệu tài khoản vào log.

## Kiểm thử

```shell
python -m pytest tests/test_buy_payment_qr.py -q
```

Đã kiểm tra giải mã lại ảnh QR PNG về đúng payload (bằng zxing-cpp trong môi
trường kiểm thử), dữ liệu không hợp lệ, nhầm chiều/người nhận, nhiều tài khoản,
thay đổi/hủy lệnh, tách tài khoản, quyền kênh và mất kết quả gửi Discord.
Mẫu gửi thử dùng toàn số 0 cho tài khoản và ghi rõ **MÔ PHỎNG — KHÔNG THANH TOÁN**.
Kết quả mô phỏng không chứng minh có lệnh thật hoặc chuyển tiền đã thành công.

Nguồn: [SePay VietQR](https://developer.sepay.vn/vi/tien-ich-khac/tao-qr-code),
[bank directory](https://vietqr.app/banks.json),
[MEXC order detail](https://www.mexc.com/api-docs/p2p/account-order-info/get-order-detail),
tài liệu OKX P2P “API Specs - External (1).pdf” do chủ tài khoản cung cấp,
[Discord messages](https://docs.discord.com/developers/resources/message).
