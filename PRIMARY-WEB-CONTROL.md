# Dashboard điều khiển chính cho kanzaki

URL: https://p2p-order-matcher.onrender.com/dashboard

Thêm biến môi trường gốc `PRIMARY_WEB_CONTROL_ENABLED=true` sau khi triển khai
mã. Không thêm biến này cho KAYHAP. Dùng `DASHBOARD_ADMIN_KEY` hiện có để đăng
nhập; không cần API key, webhook hay bí mật mới. Flag chỉ đổi giao diện và bật
API điều khiển web; không tự bật Auto mở khóa hoặc Auto bám giá.

## Web

### Công tắc quảng cáo

Mục **Quảng cáo trên sàn** liệt kê quảng cáo USDT/VND của sàn và chiều đang chọn,
kể cả quảng cáo ẩn để có thể bật lại. Đèn xanh chỉ khi API xác minh đang hiện và
còn USDT khả dụng đủ Min giao dịch. Hết USDT / dưới Min / CLOSE / LOW_STOCK / ẩn: đèn tối. Thiếu dữ liệu hoặc
lỗi API: viền vàng, không cho gạt. Danh sách đọc lại mỗi 30 giây khi đang mở trang;
worker kiểm tra quảng cáo của slot trong chu kỳ bám giá 5 giây hiện có.
MEXC phân trang 10 bản ghi của sàn/lần, không quét toàn bộ lịch sử mỗi chu kỳ;
dùng Trang trước/Trang tiếp để xem quảng cáo cũ. Mỗi trang lọc đúng chiều Mua/Bán.

Gạt công tắc mở xác nhận hiện/ẩn thật, giữ nguyên giá, Min/Max và số USDT. Sau xác
nhận, chỉ đổi đèn khi đọc lại được sàn. Không tự bổ sung USDT hay mở lại quảng cáo
đã hết số dư. Thao tác hiện/ẩn luôn tắt Auto của các slot gắn với quảng cáo; bật
quảng cáo không bật Auto. Quảng cáo ẩn/hết USDT được worker tắt Auto bám giá, kể cả
quảng cáo trước đó do bot ẩn vì giá vốn. Chủ tài khoản tự bật lại khi cần.

`ad_visibility_guards` là bảng mới do create_all tạo, không sửa dữ liệu cũ. Xác
nhận có chữ ký, hết hạn 120 giây, gắn ID/chiều/snapshot/revision để chặn dùng lại.
Mọi writer dùng cùng khóa guard và hồ sơ slot. Ghi UNKNOWN trước POST duy nhất;
timeout/chưa khớp chỉ cho **Kiểm tra hiển thị**, không tự gửi lại. Nút kiểm tra chỉ
GET sàn và cập nhật hồ sơ xác minh. Không đụng kayhap; không cần secret/env mới.

- Tổng quan: công tắc mở khóa, công tắc bám giá OKX/MEXC, từng slot và trạng thái khóa.
- OKX/MEXC riêng, Mua/Bán riêng, slot 1/2; chọn quảng cáo thuộc đúng tài khoản.
- Bám vị trí hoặc biệt danh, chênh giá có dấu, sàn Bán/trần Mua, giá vốn thủ công riêng.
- Min bán và Max tự tính; Mua giữ quy tắc ngân sách hiện có. Không nhập lại tiền đã chi.
- Đọc trực tiếp giá, tổng USDT, khả dụng, đang giữ, Min/Max và hiển thị của quảng cáo;
  tách rõ giá thực tế với kế hoạch bám giá. Thời điểm đọc được ghi trên màn hình.
- Đặt giá/số lượng/Min/Max thủ công phải xem trước và xác nhận; hồ sơ một lần và hết hạn
  như Discord. Số lượng thủ công hiện là **tổng USDT quảng cáo**, không phải khả dụng.
  Để trống số lượng nếu chỉ đổi giá. Xem bản xác nhận đầy đủ trước khi gửi sàn.
- Kiểm tra/khôi phục và nhận cấu hình MEXC dùng cùng hồ sơ xác minh với Discord.
  Nhận cấu hình giữ Auto tắt; chủ tài khoản tự bật khi sẵn sàng.

Trạng thái được làm mới từ database mỗi 5 giây; không gọi sàn ở mỗi lần poll.
Đọc sàn khi chọn slot hoặc bấm Đọc lại từ sàn. Lưu cấu hình không khẳng định
đã đổi giá trên sàn. Kế hoạch bám giá tiếp tục phải qua các kiểm tra của worker.

## Discord của kanzaki

Các bảng điều khiển chính được rút gọn thành Mở dashboard, Dừng toàn bộ Auto,
Trạng thái. Thông báo giao dịch, manual-review theo sàn, cảnh báo giá và QR giữ
nguyên. Quyền và cấu hình kayhap giữ nguyên; các bảng nút của kayhap vẫn hoạt động.

**Dừng toàn bộ Auto** chặn yêu cầu mở khóa mới và tạm dừng bám giá cả hai sàn
trong cùng tài khoản. Không hủy lệnh hoặc đóng quảng cáo; các yêu cầu đã gửi có
thể vẫn hoàn tất. Auto từng slot được giữ để chủ tài khoản tự tiếp tục sau.

## Cơ chế chung và kiểm tra

API `/dashboard/api/control/*` kiểm tra khóa quản trị, schema và package gốc.
Web gọi chính các controller cũ của Discord; không có bộ máy gửi lệnh riêng.
Các bản xem trước web có owner riêng, không dùng được qua lệnh của chủ thể khác.
Phản hồi không chứa API key, chữ ký, webhook URL hay hồ sơ ngân hàng. JSON không cache.
Chỉ public AutoP2P được dùng cho bảng đối thủ; dữ liệu ngoài được hiển thị bằng text.

Bật Auto/tiếp tục bám giá yêu cầu xác nhận rõ ràng. Web khóa công tắc mở khóa và
kiểm tra phiên bản dừng trước các yêu cầu bật, để yêu cầu bật cũ không vượt qua
một lần dừng mới. Web và Discord dùng cùng thứ tự snowflake/epoch cho công tắc.

```powershell
python -m pytest -q
```

Kiểm thử HTTP dùng database tạm và mock sàn; UI mô phỏng không chạy worker hoặc
giao dịch thật. Khi triển khai, kiểm tra GET state/live/market và nhập cấu hình
trên chính tài khoản đã đăng nhập; không thử POST giao dịch thật để kiểm tra UI.

Đặt flag gốc về `false` để trở về dashboard cũ. API điều khiển mới đóng khi flag
tắt; bảng nút Discord đầy đủ có thể mở lại bằng `/gia bang`. Không đổi các flag
Auto khi chỉ chuyển giao diện.
