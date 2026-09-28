# P2P 4-Sàn Control + Bot đối soát SePay/Telegram (V13)

V10 đọc quảng cáo P2P công khai đang hiển thị trên OKX, Binance, MEXC và Bitget, sau đó tự xếp hạng cơ hội mua USDT thấp/bán USDT cao theo giới hạn VND và USDT. Dashboard vẫn chạy bắt buộc ở chế độ READ ONLY; hệ thống **không tự release USDT**, không click và không đặt lệnh thật.

V11 bổ sung **Giá đã chốt**: sau khi người dùng ghi nhận một giao dịch BUY hoặc SELL đã thực hiện, hệ thống so sánh chiều đối ứng trên các sàn khác và tính chênh lệch gộp/ước tính sau phí.

V12 bổ sung **Bitget P2P API chính thức ở chế độ chỉ đọc**. Ứng dụng tự đồng bộ quảng cáo USDT/VND, kiểm tra thị trường VND và đưa dữ liệu Bitget vào phần so sánh 4 sàn. Mọi endpoint ghi, xác nhận thanh toán và release USDT vẫn bị khóa.

V13 bổ sung **MEXC P2P API chính thức ở chế độ chỉ đọc**. Ứng dụng đọc đơn SELL USDT/VND, lấy số tiền và `userInfo.realName`, sau đó lưu thành đơn chờ để SePay đối chiếu. App không gọi endpoint đặt lệnh, đánh dấu đã trả tiền, quản lý quảng cáo hoặc release coin.

## MEXC P2P API đọc đơn

Chỉ bật quyền `P2P → Đọc thông tin tài khoản & lệnh`. Bỏ quyền `Đặt lệnh`, `Nhà quảng cáo` và mọi quyền rút tiền.

```text
MEXC_P2P_ENABLED=true
MEXC_P2P_API_KEY=<Access Key>
MEXC_P2P_API_SECRET=<Secret Key>
MEXC_P2P_BASE_URL=https://api.mexc.com
MEXC_P2P_SYNC_SECONDS=10
MEXC_P2P_ORDER_LIMIT=50
MEXC_P2P_LOOKBACK_MINUTES=1440
MEXC_P2P_INCOMING_SIDE=SELL
MEXC_P2P_LIVE_WRITES=false
```

- API key và secret chỉ đặt trong Environment của Render.
- `SELL` nghĩa là app theo dõi đơn bạn bán USDT và nhận VND.
- Danh sách lệnh: `GET /api/v3/fiat/market/order/paginationV2`.
- Chi tiết lệnh: `GET /api/v3/fiat/order/detail`.
- App dùng `amount` và `userInfo.realName` làm hai điều kiện chính để đối chiếu SePay.
- Nút kiểm tra nằm trong Dashboard → Nâng cao → Kết nối MEXC P2P.

## Bitget P2P API chỉ đọc

Các biến môi trường cần thêm trên Render:

```text
BITGET_P2P_ENABLED=true
BITGET_P2P_API_KEY=<Khóa API>
BITGET_P2P_API_SECRET=<Secret key>
BITGET_P2P_API_PASSPHRASE=<API token/cụm mật khẩu đã đặt khi tạo khóa>
BITGET_P2P_BASE_URL=https://api.bitget.com
BITGET_P2P_SYNC_SECONDS=30
BITGET_P2P_AD_LIMIT=10
BITGET_P2P_LIVE_WRITES=false
```

- Dùng khóa Bitget chỉ có quyền P2P Search/Read; không cấp quyền rút tiền.
- `BITGET_P2P_API_PASSPHRASE` không phải Secret key.
- Không nhập khóa vào Dashboard, GitHub, `.env.example` hoặc Extension.
- Nút **Kiểm tra và đồng bộ Bitget** nằm trong Dashboard → Nâng cao.
- Background reader tự thử đồng bộ lại theo `BITGET_P2P_SYNC_SECONDS`, tối thiểu 15 giây.
- `BUY` trên Dashboard là bạn mua USDT nên API đọc quảng cáo `sell`; `SELL` là bạn bán USDT nên API đọc quảng cáo `buy`.
- Bản này không gọi `ad-create`, `ad-update`, `ad-operate`, `order-pay` hoặc `order-release`.

## So sánh 4 sàn

- `BUY` là trang nơi bạn mua USDT: hệ thống ưu tiên giá thấp.
- `SELL` là trang nơi bạn bán USDT: hệ thống ưu tiên giá cao.
- Cấu hình biên tối thiểu theo VND/USDT và phần trăm.
- Cấu hình số tiền giao dịch tối thiểu, giới hạn tối đa theo VND và USDT.
- Auto pick chỉ chọn và xếp hạng cơ hội; không thực hiện giao dịch.
- Lợi nhuận là ước tính gộp, chưa trừ phí chuyển coin, trượt giá và thời gian xử lý.

## Dashboard OKX

Mở `https://<service>.onrender.com/dashboard` và đăng nhập bằng `DASHBOARD_ADMIN_KEY`.

- 2 BUY slots và 2 SELL slots.
- Giá trần BUY/giá sàn SELL riêng theo slot, chu kỳ quét và bước giá.
- Hạn mức min/max của quảng cáo mình tách biệt với khoảng hạn mức đối thủ cần theo dõi.
- Bộ lọc tuổi tài khoản, số lệnh hoàn tất và tổng số lệnh.
- Blacklist bỏ qua hoàn toàn; Friendly theo cùng giá.
- Tin nhắn tự động chỉ được lưu làm cấu hình.
- Lịch sử cấu hình và chạy mô phỏng.
- API key/secret/passphrase không lưu trong PostgreSQL.
- LIVE bị khóa ở cấp mã nguồn cho tới khi có quyền và tài liệu OKX P2P merchant.

## Chrome Extension 4 sàn chỉ đọc

Extension nằm trong thư mục `okx-p2p-extension` (giữ tên cũ để cập nhật thuận tiện) và chỉ gửi tên người đăng quảng cáo, giá, hạn mức cùng thống kê công khai nhìn thấy trên trang P2P. Extension không gửi cookie, session token, mật khẩu, API key, số dư hay dữ liệu lệnh.

1. Tạo biến Render `OKX_BROWSER_BRIDGE_SECRET` bằng một chuỗi ngẫu nhiên dài và khác `DASHBOARD_ADMIN_KEY`.
2. Mở `chrome://extensions`, bật **Developer mode**.
3. Chọn **Load unpacked**, rồi chọn thư mục `okx-p2p-extension`.
4. Mở Extension, nhập URL Render và đúng Bridge Secret.
5. Mở trang P2P của OKX/Binance/MEXC/Bitget, chọn đúng BUY hoặc SELL rồi nhấn **Quét ngay**.
6. Quét cả hai chiều cần dùng. Dashboard **So sánh 4 sàn** sẽ hiển thị cơ hội phù hợp.

Chrome và tab sàn phải đang mở. Đây là bộ nhận diện giao diện thử nghiệm, không phải API P2P chính thức; nếu sàn đổi giao diện thì có thể cần cập nhật selector.

## API tài khoản của các sàn

- Binance: `https://www.binance.com/en/my/settings/api-management`
- MEXC: `https://www.mexc.com/user/openapi`
- Bitget: `https://www.bitget.com/account/newapi`

Các API key thông thường dùng cho các sản phẩm được sàn cấp quyền như Spot/Futures/Wallet. Không nhập các key này vào Extension. Chỉ tích hợp thao tác P2P LIVE sau khi tài khoản được sàn cấp tài liệu và quyền P2P merchant chính thức. Khi thử API, chỉ bật quyền đọc và whitelist IP; không bật quyền rút tiền.

### Ý nghĩa các trường đề giá

- `Min/Max giao dịch của quảng cáo mình`: hạn mức khách được giao dịch trên quảng cáo của mình.
- `Target BUY/SELL min/max`: khoảng hạn mức của quảng cáo đối thủ cần đưa vào phép tính; hai khoảng chỉ cần giao nhau.
- `Giá mua tối đa`: giá trần của BUY, mô phỏng không được đề xuất cao hơn.
- `Giá bán tối thiểu`: giá sàn của SELL, mô phỏng không được đề xuất thấp hơn.

## Luồng xử lý

1. Nhân viên tạo đơn qua `POST /orders`.
2. SePay đẩy giao dịch đến `POST /webhooks/sepay`; endpoint chuẩn hóa cũ vẫn có tại `/bank-transactions/{bank}`.
3. Bot chỉ xét đơn `WAITING_PAYMENT`, cùng số tiền và trong cửa sổ thời gian.
4. Điểm ưu tiên số tiền, tên người thanh toán trong nội dung và 5 số cuối ID lệnh.
5. Từ 90 điểm và không mơ hồ: `AUTO_MATCHED`; từ 60: `REVIEW_REQUIRED`; còn lại: `UNMATCHED`.
6. Telegram luôn nhắc người vận hành kiểm tra thủ công.

## Chạy thử

Yêu cầu Python 3.11+.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Mở `http://127.0.0.1:8000/docs` để nhập đơn và thử giao dịch mà không cần viết lệnh.

## Telegram

1. Tạo bot với `@BotFather`, lấy token.
2. Nhắn một tin cho bot, lấy `chat_id` bằng API `getUpdates` của Telegram.
3. Điền `TELEGRAM_BOT_TOKEN` và `TELEGRAM_CHAT_ID` trong `.env`.

Không gửi token/khóa ngân hàng qua chat. Chỉ đặt chúng trong biến môi trường trên máy chủ.

## Triển khai Render

1. Tạo PostgreSQL trên Render và sao chép **Internal Database URL**.
2. Tạo Web Service từ kho GitHub này hoặc dùng `render.yaml`.
3. Điền `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`; đồng thời tạo `OKX_BROWSER_BRIDGE_SECRET` nếu dùng Extension. Render tự sinh các khóa khi dùng Blueprint.
4. Kiểm tra `https://<service>.onrender.com/health`.
5. Trong SePay, tạo Webhook sự kiện **Có tiền vào** đến `https://<service>.onrender.com/webhooks/sepay`.
6. Cấu hình header `X-API-Key` có giá trị đúng bằng `SEPAY_WEBHOOK_SECRET` trên Render.

Không dùng gói máy chủ tự ngủ cho đối soát giao dịch thật.

## Ví dụ nhập đơn

Header: `X-API-Key: <INGEST_API_KEY>`

```json
{
  "order_code": "OKX-ABC123",
  "side": "SELL",
  "fiat_amount": 52360000,
  "crypto_amount": 2000,
  "counterparty_name": "Nguyễn Văn An",
  "expected_bank": "VIB",
  "payment_note": "ABC123"
}
```

## Ví dụ giao dịch VIB/VPBank chuẩn hóa

```json
{
  "transaction_id": "VIB-20260920-0001",
  "amount": 52360000,
  "direction": "CREDIT",
  "sender_name": "NGUYEN VAN AN",
  "sender_account": "***1234",
  "description": "Thanh toan OKX-ABC123",
  "occurred_at": "2026-09-20T15:10:00+07:00"
}
```

## Nối API ngân hàng thật

Phần khớp đơn đã độc lập với ngân hàng. Để nối thật cần lấy từ từng ngân hàng: tài liệu API doanh nghiệp/đối tác, môi trường thử nghiệm, client ID/secret hoặc chứng thư ký, danh sách IP được phép, endpoint lịch sử giao dịch hoặc webhook và mẫu payload. Sau đó chỉ cần chuyển payload ngân hàng về đúng cấu trúc `BankTransactionIn` ở trên.

Tuyệt đối không tự động hóa đăng nhập Internet Banking, đọc OTP hoặc lưu mật khẩu ngân hàng trong mã nguồn.

## Kiểm thử

```bash
PYTHONPATH=. pytest -q
```
