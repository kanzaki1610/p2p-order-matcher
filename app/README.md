# Bot khớp đơn P2P – SePay/MB/VIB/VPBank (V2)

V2 nhận đơn P2P nhập thủ công, nhận trực tiếp webhook SePay từ MB/VIB/VPBank, chấm điểm khớp và báo Telegram. Bot **không tự release USDT**.

## Luồng xử lý

1. Nhân viên tạo đơn qua `POST /orders`.
2. SePay đẩy giao dịch đến `POST /webhooks/sepay`; endpoint chuẩn hóa cũ vẫn có tại `/bank-transactions/{bank}`.
3. Bot chỉ xét đơn `WAITING_PAYMENT`, cùng số tiền và trong cửa sổ thời gian.
4. Điểm ưu tiên mã đơn trong nội dung (40) + số tiền (50); tên người chuyển chỉ dùng khi ngân hàng cung cấp.
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
3. Điền `DATABASE_URL`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`; Render tự sinh hai khóa còn lại nếu dùng Blueprint.
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
