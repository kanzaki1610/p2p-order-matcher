# MEXC P2P chỉ đọc — v1.5.0

## 1. Quyền API MEXC

Chỉ giữ quyền:

```text
P2P
☑ Đọc thông tin tài khoản & lệnh
☐ Đặt lệnh
☐ Nhà quảng cáo
```

Không bật quyền rút tiền.

## 2. Biến Environment trên Render

Thêm các biến sau vào Web Service `p2p-order-matcher`:

```text
MEXC_P2P_ENABLED=true
MEXC_P2P_API_KEY=<Access Key MEXC>
MEXC_P2P_API_SECRET=<Secret Key MEXC>
MEXC_P2P_BASE_URL=https://api.mexc.com
MEXC_P2P_SYNC_SECONDS=10
MEXC_P2P_ORDER_LIMIT=50
MEXC_P2P_LOOKBACK_MINUTES=1440
MEXC_P2P_INCOMING_SIDE=SELL
MEXC_P2P_LIVE_WRITES=false
```

Không nhập khóa vào GitHub, Dashboard hoặc Chrome Extension.

## 3. Cập nhật mã nguồn

Chép toàn bộ file trong gói vào đúng thư mục repository, commit rồi chọn **Render → Manual Deploy → Deploy latest commit**.

Mở `/docs`, phiên bản phải là `1.5.0`. Sau đó vào **Dashboard → Nâng cao → Kết nối MEXC P2P** và bấm **Kiểm tra và đọc lệnh MEXC**.

## 4. Luồng hoạt động

App đọc đơn SELL USDT/VND, lấy số tiền và họ tên thật từ chi tiết đơn. Khi SePay báo có tiền vào, bot đối chiếu:

1. Số tiền có chính xác không.
2. Họ tên trên lệnh có xuất hiện trong nội dung chuyển khoản hoặc tên người chuyển không.

App không đặt lệnh, không quản lý quảng cáo và không release USDT.
