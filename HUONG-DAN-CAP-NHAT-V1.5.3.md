# Cập nhật MEXC P2P v1.5.3

## Lỗi đã sửa

- Dùng endpoint Maker View `/api/v3/fiat/merchant/order/paginationV2` để đọc các lệnh phát sinh từ quảng cáo của chính tài khoản.
- Không gửi `coinId` và `side` trong truy vấn danh sách để tránh MEXC trả danh sách rỗng; app lọc USDT/VND/SELL sau khi nhận dữ liệu.
- Nếu Maker View không có dữ liệu, app thử endpoint danh sách tất cả lệnh làm phương án dự phòng.
- Giữ chức năng tự đối soát lại giao dịch SePay `UNMATCHED` khi lệnh MEXC đến trễ.
- Chỉ đọc và cảnh báo; không tự release USDT.

## Cách kiểm tra

1. Chép đè toàn bộ mã nguồn, commit/push và deploy lại Render.
2. Mở `/docs`, xác nhận phiên bản **1.5.3**.
3. Khi đang có một lệnh MEXC, mở Dashboard → **Nâng cao → Kết nối MEXC P2P**.
4. Kết quả phải có `nhận 1 lệnh` hoặc lớn hơn. Trường `source_endpoint` phải là `MAKER_VIEW`; nếu Maker View rỗng thì sẽ là `ALL_ORDERS_FALLBACK`.

Sau khi kiểm tra thành công, bot tự quét theo `MEXC_P2P_SYNC_SECONDS`, không cần nhấn thủ công.
