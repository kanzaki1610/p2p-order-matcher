# Một service, một PostgreSQL, hai tài khoản

## Chi phí và nguyên tắc

Giữ service p2p-order-matcher $7/tháng, database hiện tại nâng lên gói $6/tháng
với 1GB storage $0.30/tháng: tổng dự kiến $13.30/tháng trước thuế/vượt hạn mức.
Không tạo service hoặc database thứ hai. Workspace P2P kayhap chỉ là workspace
trống đã tạo, không cần thuê dịch vụ trong đó.

## Cách tách dữ liệu

- Tài khoản gốc dùng bảng public hiện tại; không sao chép hoặc xóa lịch sử.
- Kayhap dùng schema kayhap và role PostgreSQL p2p_kayhap không được đọc/ghi
  bảng public. Hai connection pool, bộ model, cấu hình và vòng đọc OKX riêng
  nằm trong một ứng dụng Uvicorn. Không có thao tác thay cấu hình global theo request.
- Khóa OKX, SePay, ingest, dashboard và năm webhook Discord riêng cho kayhap.
  Biến của kayhap bắt đầu KAYHAP_; không kế thừa secret hoặc file .env của người gốc.
- Cùng mã lệnh/mã ngân hàng được phép tồn tại ở cả hai schema; chỉ khớp trong
  schema của tài khoản nhận webhook. Release và notification dùng cấu hình của
  chính tài khoản đó.
- Khởi động bị từ chối nếu schema không có, dùng chung role, chung khóa xác thực,
  thiếu webhook hoặc role kayhap được quyền đọc/ghi bảng của tài khoản gốc.

## URL

| Chức năng | Tài khoản gốc | Kayhap |
|---|---|---|
| SePay | /webhooks/sepay | /accounts/kayhap/webhooks/sepay |
| Dashboard | /dashboard | /accounts/kayhap/dashboard |
| Health | /health | /accounts/kayhap/health |
| Test Discord | /discord/test-notification | /accounts/kayhap/discord/test-notification |

Prefix không phải khóa bảo mật. Mỗi endpoint xác thực bằng khóa riêng của tài khoản.
Không cấu hình SePay kayhap vào URL /webhooks/sepay gốc.

## Chuẩn bị và triển khai

1. Sao lưu database gốc và tạm dừng giao dịch thật trước khi chuyển cấu hình.
2. Nâng cấp database gốc trước ngày hết hạn Free 20/10/2026. Không tạo database khác.
3. Chạy scripts/provision_shared_account.py để preview, rồi --apply với
   DATABASE_URL và KAYHAP_DATABASE_PASSWORD đã đặt an toàn trong môi trường shell.
   Script tạo role/schema trong một transaction, không sửa bảng gốc. Render phải
   cho role quản trị hiện tại tạo role mới; nếu thiếu quyền thì dừng và báo rõ.
4. Đặt KAYHAP_DATABASE_URL trỏ cùng host/database như DATABASE_URL, nhưng user
   p2p_kayhap/password riêng. Không lưu mật khẩu trong GitHub.
5. Render Environment thêm:

```
MULTI_ACCOUNT_ENABLED=true
KAYHAP_DATABASE_SCHEMA=kayhap
KAYHAP_DATABASE_URL=<cùng database, role p2p_kayhap>
KAYHAP_INGEST_API_KEY=<khóa riêng ngẫu nhiên >=32 ký tự>
KAYHAP_SEPAY_WEBHOOK_SECRET=<khóa khác >=32 ký tự>
KAYHAP_DASHBOARD_ADMIN_KEY=<khóa khác >=32 ký tự>
KAYHAP_NOTIFICATION_PROVIDER=discord
KAYHAP_TELEGRAM_FALLBACK_ENABLED=false
KAYHAP_TELEGRAM_COMMANDS_ENABLED=false
KAYHAP_DISCORD_COMMANDS_ENABLED=false
KAYHAP_BITGET_P2P_ENABLED=false
KAYHAP_MEXC_P2P_ENABLED=false
KAYHAP_OKX_P2P_ENABLED=true
KAYHAP_OKX_AUTO_RELEASE_ENABLED=false
KAYHAP_OKX_API_KEY=<kayhap>
KAYHAP_OKX_API_SECRET=<kayhap>
KAYHAP_OKX_API_PASSPHRASE=<kayhap>
KAYHAP_DISCORD_WEBHOOK_ORDERS=<p2p-orders>
KAYHAP_DISCORD_WEBHOOK_PAYMENT_DETECTED=<payment-detected>
KAYHAP_DISCORD_WEBHOOK_REVIEW_REQUIRED=<manual-review>
KAYHAP_DISCORD_WEBHOOK_CONFIRMED=<confirmed>
KAYHAP_DISCORD_WEBHOOK_SYSTEM_ALERTS=<system-alerts>
```

6. Start command: uvicorn app.shared:app --host 0.0.0.0 --port $PORT.
   Chỉ một Uvicorn worker. Để MULTI_ACCOUNT_ENABLED=false khi triển khai code
   lần đầu: địa chỉ gốc, cấu hình và luồng gốc vẫn hoạt động như trước.
7. Kiểm tra thực tế PostgreSQL: role kayhap không SELECT được public.p2p_orders,
   current_schema() đúng kayhap và có bảng release/receipt riêng. Kiểm tra cả hai
   health, Discord test notification, đọc đơn OKX và SePay (không release thật).
8. Chủ tài khoản bật auto riêng cho từng người sau khi kiểm tra.

## Rollback

Đặt MULTI_ACCOUNT_ENABLED=false, deploy lại. Không xóa schema kayhap hoặc role;
giữ dữ liệu để phục hồi. Dữ liệu gốc vẫn ở public và dùng URL cũ.

## Kiểm thử

142 kiểm thử mô phỏng đạt, bao gồm API thật trên hai namespace SQLite giả lập,
cùng mã lệnh/giao dịch/số tiền/họ tên, xác thực chéo bị từ chối, thông báo đúng
tài khoản và startup/shutdown cả hai vòng đọc. Chưa xác minh PostgreSQL Render,
khóa OKX kayhap hoặc nâng cấp database thật. Các bước kiểm tra thực tế trên là
bắt buộc trước khi bật multi-account trên production.
