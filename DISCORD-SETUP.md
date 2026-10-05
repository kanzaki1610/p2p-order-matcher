# Chuyển P2P order matcher sang Discord

## Trạng thái bản bàn giao

Bản này được ghép vào repo GitHub kanzaki1610/p2p-order-matcher, dựa trên commit c2bf1a0 ngày 05/10/2026. Giữ các sửa lỗi MEXC hiện có, API incoming side và bảng mexc_notifications chống gửi trùng. Thêm ORDER_CREATED cho thông báo đơn MEXC mới; receipt cũ tiếp tục được dùng.

Server Discord: kanzaki (368802651904802818). Đã tạo category P2P Matcher và năm kênh. Webhook/application và triển khai production cần được cấu hình trước khi gửi thật. Không sao chép lịch sử chat Telegram. Tiếp tục dùng DATABASE_URL hiện có để giữ đơn và audit.

## Những nơi đã kiểm tra và thay đổi

| Nơi | Trước | Sau |
|---|---|---|
| app/main.py: POST /bank-transactions/{bank} | notify_match từ telegram | notification router |
| app/main.py: POST /webhooks/sepay | notify_match từ telegram | notification router |
| app/mexc_sync.py: đối chiếu giao dịch khi đơn MEXC đến trễ | notify_match từ telegram | notification router |
| app/main.py: /webhooks/telegram | trả lời các lệnh Telegram | giữ sau flag; phát thêm event CONFIRMED/ORDER_CREATED/REJECTED |
| app/telegram_commands.py | /xacnhan và các lệnh quản lý đơn | tái sử dụng trên Discord; /confirm ánh xạ /xacnhan |
| app/bitget_sync.py, app/mexc_sync.py | bỏ qua lỗi vòng đồng bộ | SYSTEM_ALERT khi bắt đầu lỗi và khi phục hồi |

Trong nguồn v1.5.3, matcher phát AUTO_MATCHED, REVIEW_REQUIRED, UNMATCHED; AUTO_MATCHED đổi trạng thái đơn sang PAYMENT_DETECTED. Router phát thêm PAYMENT_DETECTED khi chuyển trạng thái này. CONFIRMED phát sau xác nhận thành công. Không phát xác nhận lần nữa nếu đơn đã CONFIRMED. REVIEW_REQUIRED không tự chuyển đơn sang PAYMENT_DETECTED; giữ nguyên quy tắc nghiệp vụ hiện tại.

## Kênh đề xuất trong server hiện có

Tạo category **P2P Matcher**, dùng text channels:

| Kênh | Event | Biến môi trường |
|---|---|---|
| #p2p-orders | AUTO_MATCHED, ORDER_CREATED; dùng /p2p, /confirm | DISCORD_WEBHOOK_ORDERS |
| #payment-detected | PAYMENT_DETECTED | DISCORD_WEBHOOK_PAYMENT_DETECTED |
| #review-required | REVIEW_REQUIRED, UNMATCHED, REJECTED | DISCORD_WEBHOOK_REVIEW_REQUIRED |
| #confirmed | CONFIRMED | DISCORD_WEBHOOK_CONFIRMED |
| #system-alerts | lỗi/phục hồi đồng bộ, notification test | DISCORD_WEBHOOK_SYSTEM_ALERTS |

Chọn server đúng trước khi tạo. Mỗi channel: Edit Channel → Integrations → Webhooks → New Webhook → Copy Webhook URL. Người tạo cần Manage Channels/Manage Webhooks. Dán URL trực tiếp vào môi trường Render hoặc `.env` riêng, không dán vào chat hoặc commit. Nếu Discord yêu cầu mật khẩu/2FA/CAPTCHA, chủ tài khoản tự thực hiện.

Webhook URL là secret cho phép gửi vào kênh. Bot token cũng là secret. Application ID, Guild ID, Channel/User/Role IDs và Public Key là các giá trị cấu hình, không phải bot token. Không cần bot token để gửi outbound bằng webhook.

## Biến môi trường

Xem `.env.example` (không chứa secret thật).

- `NOTIFICATION_PROVIDER=discord`: Discord trước; `telegram` để rollback; `both` gửi cả hai.
- `TELEGRAM_FALLBACK_ENABLED=true`: nếu Discord thất bại, gửi Telegram bằng token/chat ID hiện có. Gửi thành công một phần rồi lỗi có thể tạo thông báo trùng khi fallback; không có persistent outbound queue.
- `TELEGRAM_COMMANDS_ENABLED=true`: giữ endpoint Telegram. Đặt `false` sau khi Discord command đã chạy ổn.
- `DISCORD_WEBHOOK_URL`: webhook mặc định nếu kênh riêng chưa có URL. Có thể để trống nếu đã cấu hình đủ năm URL riêng.
- Năm `DISCORD_WEBHOOK_*` trong bảng trên: webhook từng kênh. Khi chỉ dùng webhook mặc định, mọi event cùng vào một kênh.
- `DISCORD_COMMANDS_ENABLED=false`: outbound dùng được ngay; bật `true` khi đã cấu hình application.
- `DISCORD_APPLICATION_ID`, `DISCORD_PUBLIC_KEY`, `DISCORD_GUILD_ID`: lấy từ application/server.
- `DISCORD_BOT_TOKEN`: chỉ cần cho script đăng ký commands; có thể gỡ khỏi môi trường web sau khi đăng ký.
- `DISCORD_ALLOWED_USER_IDS`, `DISCORD_ALLOWED_ROLE_IDS`: danh sách ID cách nhau bằng dấu phẩy; người dùng phải khớp một trong hai.
- `DISCORD_COMMAND_CHANNEL_IDS`: danh sách channel ID cho phép, cách nhau bằng dấu phẩy. Để trống sẽ từ chối mọi lệnh.

Render blueprint đã thêm các biến này. Với service đang tồn tại, thêm giá trị trong Environment; đừng tạo lại database hoặc ghi đè các secret sàn/SePay hiện có.

## Application commands

1. Tạo/chọn application P2P Matcher tại Discord Developer Portal. Lấy Application ID, Public Key. Tạo bot token riêng nếu cần đăng ký commands; không dùng user token.
2. Cài application vào đúng server bằng scope `applications.commands` (có thể thêm `bot` nếu dùng bot user). HTTP interactions không cần Message Content Intent, Gateway process hoặc quyền Administrator.
3. Deploy bản này và đặt các ID/allowlists; bật `DISCORD_COMMANDS_ENABLED=true`.
4. Trong General Information đặt Interactions Endpoint URL: `https://<service-host>/webhooks/discord`. Endpoint xác thực Ed25519, timestamp và trả PING.
5. Từ repo root, chạy `python -m scripts.register_discord_commands` với token/ID trong môi trường. Script POST upsert hai command, giữ các command khác.
6. Server Settings → Integrations → application → cấp quyền dùng commands cho operator/role được phép. Commands mặc định tắt cho thành viên thường. Backend vẫn kiểm tra guild, channel và allowlist, kể cả admin.

Ví dụ:

```text
/confirm order_code:P2P12345
/p2p command:help
/p2p command:don 260920160719335 | 1000000 | THI HONG THAM DAO | MB
/p2p command:danhsach
/p2p command:baocao
/p2p command:chitiet P2P12345
/p2p command:xacnhan P2P12345
/p2p command:tuchoi P2P12345 | Tên người chuyển không đúng
/p2p command:huy P2P12345
```

Command trả lời riêng cho operator, thông báo kết quả được gửi tới kênh tương ứng. Database giữ schema audit cũ: actor Discord ghi `discord:<user-id>` trong cột `telegram_user_id`, display name có tiền tố Discord; không giả làm tài khoản Telegram. Chỉ thêm bảng `discord_interaction_receipts` để chặn cùng interaction chạy lại qua nhiều worker. Nếu tiến trình dừng sau khi ghi receipt nhưng trước khi xử lý, xem trạng thái đơn rồi gửi lệnh mới; background task không phải durable queue. Không có auto release USDT.

## Test notification Discord

Sau khi thêm webhook #system-alerts (hoặc URL mặc định), gọi endpoint bằng API key hiện có. Endpoint chỉ gửi dữ liệu TEST giả và không thay đổi đơn:

```powershell
$headers = @{ 'X-API-Key' = $env:INGEST_API_KEY }
Invoke-RestMethod -Method Post -Uri "$env:PUBLIC_BASE_URL/discord/test-notification" -Headers $headers
```

Kết quả `{ "sent": true, "channel": "system-alerts" }` và message TEST trong kênh. Nếu dùng URL mặc định thì kiểm tra kênh của URL đó. HTTP 502 nghĩa là chưa gửi được Discord; endpoint test không fallback để tránh nhầm thành công Telegram với Discord.

Test luồng đầy đủ bằng một đơn/giao dịch giả trong database thử nghiệm, không nhập giao dịch giả vào production. Kiểm tra AUTO_MATCHED ở #p2p-orders và PAYMENT_DETECTED ở #payment-detected; /confirm chỉ cho phép đơn PAYMENT_DETECTED; kiểm tra #confirmed và audit actor. Kiểm tra user ngoài allowlist bị từ chối.

```text
pip install -r requirements.txt
python -m pytest -q
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Kiểm thử tự động dùng Discord mock; chưa xác nhận kết nối Discord thật khi chưa có webhook/server. Outbound chia tin dài, giữ toàn bộ nội dung giao dịch, tắt mentions, dùng wait=true và retry HTTP 429 có giới hạn. Lỗi mạng không retry tự động để giảm nguy cơ gửi trùng. Không ghi webhook/token/response body vào log.

Tài liệu chính thức: [Webhooks](https://docs.discord.com/developers/resources/webhook), [HTTP interactions](https://docs.discord.com/developers/interactions/receiving-and-responding), [Application commands](https://docs.discord.com/developers/interactions/application-commands).
