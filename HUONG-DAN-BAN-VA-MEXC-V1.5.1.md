# Bản vá v1.5.1 — ACB và MEXC P2P

Bản này bao gồm đầy đủ phần MEXC P2P chỉ đọc của v1.5.0 và bổ sung các file xử lý ACB cho Telegram, SePay và API tạo đơn.

## Lệnh Telegram ACB

```text
/don TESTACB22345 | 2001 | DUONG DUC HUY | ACB
```

Bot phải tạo đơn thành công và không còn báo chỉ hỗ trợ MB, VIB hoặc VPBANK.

## Cập nhật

1. Giải nén gói v1.5.1.
2. Upload toàn bộ nội dung vào thư mục gốc repository GitHub.
3. Cho phép ghi đè các file cũ, commit thay đổi.
4. Trên Render chọn **Manual Deploy → Deploy latest commit**.
5. Mở `/docs`, kiểm tra phiên bản `1.5.1`.
6. Gửi lại lệnh Telegram ACB ở trên.

Không cần thay đổi `SEPAY_WEBHOOK_SECRET` hoặc cấu hình Telegram hiện tại.
