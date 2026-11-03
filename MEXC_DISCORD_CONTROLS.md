# Điều khiển MEXC qua Discord

Mỗi tài khoản có bảng riêng trong `#bam-gia-mexc`. Cảnh báo gửi vào
`#canh-bao-gia-mexc`; bảng OKX và giá vốn OKX không bị thay đổi khi dùng nút MEXC.

## Các nút

- **Đặt giá Mua/Bán**: nhập slot, giá, tùy chọn số lượng USDT và Min/Max VND.
  Xem trước rồi xác nhận một lần. Khi gửi, Auto của slot tắt; trạng thái
  hiển thị quảng cáo giữ nguyên. Bán phải đạt giá vốn MEXC +10đ.
- **Vị trí Mua/Bán**: đổi thứ hạng đối thủ cần bám, từ 1 đến 50. Giữ nguyên
  công tắc Auto. Nhập đúng biệt danh để loại quảng cáo của chính mình.
- **Chọn quảng cáo Mua/Bán**: xác minh ID bằng API của đúng tài khoản,
  không dùng một ID cho hai slot. Đổi ID sẽ tắt Auto slot.
- **Auto Mua/Bán**: bật/tắt riêng slot 1 hoặc 2. Chỉ bật khi quảng cáo đang
  OPEN và không có yêu cầu chưa rõ kết quả. Bot không mở lại quảng cáo chủ tự tắt.
- **Trạng thái**, **Quảng cáo mua/bán**: xem cấu hình và giá quảng cáo thực tế.
- **Tạm dừng/Tiếp tục bám giá**: công tắc riêng của MEXC, giữ lựa chọn Auto
  từng slot; không thay công tắc mở khóa USDT hoặc OKX. Yêu cầu đã gửi vẫn có
  thể hoàn tất.
- **Nhập giá vốn MEXC**: nhập thủ công, độc lập với OKX; không lấy lệnh mua gần nhất.
- **Min bán · Max tự động**: lưu Min, tắt Auto slot để chủ kiểm tra rồi bật lại.
  Max bán = USDT còn khả dụng × giá mới − 1 VND, tối đa bằng giới hạn sàn.
  Không dùng số lượng ban đầu hoặc USDT đang bị giữ cho lệnh khác để tính Max.
- **Kiểm tra/khôi phục Mua/Bán**: tắt Auto slot, đọc lại API sau tối thiểu
  120 giây và so với hồ sơ yêu cầu. Chỉ gỡ khóa khi xác minh đủ giá, tổng
  số lượng, Min/Max và trạng thái hiển thị. Không gửi lại POST. Yêu cầu cũ
  thiếu hồ sơ xác minh vẫn giữ khóa; không có nút bỏ qua bảo vệ.
  Ngoại lệ hẹp: yêu cầu PRICE cũ bị MEXC từ chối chữ ký rõ ràng bằng HTTP 400,
  mã 700002, có thể đối chiếu lại quảng cáo đúng chủ rồi gỡ khóa với Auto tắt.
  Không áp dụng cho timeout, HTTP 429 hoặc lỗi không xác định.

## Ngân sách mua

BUY giữ ngân sách VND còn lại. Khi khởi tạo sổ ngân sách cho một quảng cáo,
bot lấy Max đang cấu hình làm ngân sách ban đầu và trừ các lệnh DONE của đúng
quảng cáo từ lịch sử API. Ví dụ ngân sách 158.532.000 VND trừ lệnh đã mua
132.475.000 VND còn 26.057.000 VND. Đổi giá không nạp lại ngân sách.

Nếu quảng cáo đã giao dịch trước khi bật tính năng, cần kiểm tra ngân sách
hiển thị; bot sẽ trừ toàn bộ lịch sử đã hoàn tất của quảng cáo đó, thay vì
đoán Max hiện tại đã được chủ trừ hay chưa. Để đặt ngân sách còn lại mới,
dùng **Đặt giá Mua**, nhập Max mong muốn và xác nhận rõ trong bản xem trước.
Sửa số lượng hoặc Max thủ công được coi là thiết lập lại ngân sách còn mua;
chỉ sửa giá không thiết lập lại ngân sách.

Bot đọc hết lịch sử bằng cursor `lastCreateTime` và `lastId`, lưu ID lệnh để
không trừ hai lần sau khởi động lại. Lệnh hủy không trừ tiền; lệnh đang chờ,
số dư bị giữ, lịch sử thiếu hoặc bất thường sẽ chặn sửa giá. Bot điều chỉnh
số lượng còn mua theo giá mới để đủ ngân sách VND và giữ Min gốc.

## Vận hành và cấu hình

- `MEXC_AD_PRICING_ENABLED`: cho phép tính năng chỉnh giá, không tự bật Auto slot.
- `MEXC_AD_PRICING_SECONDS=5`: chu kỳ kiểm tra tối thiểu 5 giây.
- `MEXC_AD_MIN_UPDATE_SECONDS=30`: tối thiểu 30 giây giữa lần sửa giá, dùng chung
  giữa các slot trong cùng tài khoản. Thao tác ẩn bảo vệ có thể thực hiện sớm hơn.
- `DISCORD_MEXC_PRICING_CHANNEL_ID`: kênh dùng nút MEXC.
- `DISCORD_WEBHOOK_MEXC_PRICING`: webhook cảnh báo MEXC riêng.
- `DISCORD_PRICING_COMMANDS_ENABLED`, `DISCORD_APPLICATION_ID`,
  `DISCORD_PUBLIC_KEY`, `DISCORD_GUILD_ID`, `DISCORD_ALLOWED_USER_IDS`:
  xác minh chữ ký và giới hạn operator đúng server/kênh.

Kayhap dùng các biến cùng tên có tiền tố `KAYHAP_`, API của kayhap, role/schema
Postgres riêng. Chữ ký dùng chung ứng dụng Discord, nhưng server, operator,
kênh và webhook phải tách biệt. Operator kayhap phải đã được cấu hình làm
operator Auto của chính kayhap; không cho phép role rộng. Các kiểm tra này
chạy lúc khởi động. Lệnh xác nhận mở khóa chung vẫn tắt cho kayhap.

Bản cập nhật thêm bảng mới qua `Base.metadata.create_all`, không sửa/xóa bảng
cũ. Khóa kết quả chưa rõ lưu bền trước POST. Không tự thử lại yêu cầu ghi khi
timeout hoặc chưa khớp dữ liệu đọc lại. Không cần thêm bot, database hoặc gói
Render để dùng các nút mới.

## API chính thức đã đối chiếu

- [Cập nhật quảng cáo](https://www.mexc.com/api-docs/p2p/advertisement-management/save-or-update-ad)
- [Giới hạn quảng cáo](https://www.mexc.com/api-docs/p2p/account-order-info/get-ads-limit)
- [Lịch sử lệnh thương nhân](https://www.mexc.com/api-docs/p2p/account-order-info/get-merchant-order-pagination-v2)

MEXC cập nhật cùng ID quảng cáo bằng query parameters; `supplyQuantity` và
`reduceQuantity` là lượng cộng/giảm. Giữ phương thức thanh toán và điều kiện
quảng cáo gốc; không sao chép payload OKX sang MEXC.

Chữ ký dùng đúng chuỗi query mã hóa RFC3986 gửi đi, khoảng trắng là `%20`,
không dùng `+`. Đã xác minh bằng GET trên môi trường thật với ký tự Unicode,
khoảng trắng, dấu phẩy, `&`, `+` và `%`; không cần gửi POST để kiểm tra chữ ký.
