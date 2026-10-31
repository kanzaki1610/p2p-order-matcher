# Hạn mức khi bám giá OKX

Trong kênh điều khiển OKX, dùng `/gia bang` để lấy bảng nút mới.
Chiều mua USDT không cần nhập Min/Max thủ công. Bot giữ Min đang có trên OKX,
lưu Max ban đầu làm ngân sách VND và trừ số tiền của các lệnh mua đã hoàn tất.
Khi bám giá, bot gửi giá, lượng USDT tính lại từ ngân sách còn lại và Min/Max
cùng nhau để quảng cáo thay thế không khôi phục lượng mua ban đầu.
Nút hạn mức mua cũ chỉ hiển thị hướng dẫn, không lưu cấu hình hay tắt Auto.

Với chiều bán, chọn **Min bán · Max tự động**, nhập slot và Min VND cho mỗi
giao dịch. Không cần nhập Max. Nếu chưa lưu Min riêng cho quảng cáo hiện tại,
bot giữ Min đang có trên OKX.

Lưu cấu hình tắt Auto của slot, không gửi lệnh sửa quảng cáo ngay.
Kiểm tra giá vốn, vị trí và quảng cáo, sau đó bật Auto của slot khi sẵn sàng.
Yêu cầu cũ UNKNOWN vẫn phải được xác minh riêng trước khi bật Auto.

Khi chạy chiều bán, bot gửi `unitPrice`, `minOrderLimit`, `maxOrderLimit` cùng một
yêu cầu cập nhật. Với SELL, Max = `floor(availableAmount × unitPrice) - 1` VND.
Ví dụ 3000 USDT × 26500 → Max 79499999; giá giảm về 26100 → Max 78299999.
Max tăng/giảm theo giá mới và số USDT còn khả dụng, không lấy số lượng ban đầu
hoặc Max cũ làm trần. Các Max SELL từng lưu trước đây không dùng làm trần giá trị SELL mới. Nếu Max tính được dưới Min hoặc không xác minh được
lượng USDT khả dụng, bot báo lỗi và không sửa quảng cáo.
Hạn mức chỉ gắn với đúng quảng cáo của slot và được chuyển sang ID mới
sau khi xác minh thành công việc thay quảng cáo của OKX.

Bot lưu hạn mức đã gửi để kiểm tra lại sau khởi động. Chỉ báo thành công
khi giá, chiều, trạng thái và Min/Max trả về đều đúng; không tự gửi lại
yêu cầu có kết quả chưa rõ. Không thay đổi quy tắc mở khóa USDT hay MEXC.

Không cần biến môi trường mới. Hai bảng mới `okx_auto_limits` và
`okx_pending_limits` được tạo khi ứng dụng khởi động.

## Kiểm tra và khôi phục slot

Trong bảng nút OKX, bấm **Kiểm tra/khôi phục Mua** hoặc **Kiểm tra/khôi phục Bán**,
rồi chọn slot. Bot tắt Auto của slot trước khi đọc lại quảng cáo bằng API GET.
Bot không sửa/ẩn quảng cáo, không tự gửi lại yêu cầu và không tự bật Auto.

Nếu đã có ID kết quả, bot kiểm tra đúng chủ sở hữu, USDT/VND, chiều, giá,
trạng thái và Min/Max đã gửi trước khi ghi nhận hoàn tất. Với yêu cầu tự bám giá
cũ bị từ chối HTTP 400/mã 55723 và không có ID kết quả, chỉ gỡ khóa nếu quảng cáo
cũ vẫn hoạt động, giữ đúng giá trước yêu cầu (khác giá đã yêu cầu) và trạng thái
hiển thị đúng. Yêu cầu phải đã qua ít nhất 120 giây; không xử lý SUBMITTING.

Timeout, lỗi kết nối, 429, lỗi khác hoặc dữ liệu không khớp vẫn giữ khóa.
Kết quả và toàn bộ thông tin yêu cầu cũ được lưu vào nhật ký trước khi chuyển
slot về IDLE; giữ Min đã nhập và thời điểm gửi cũ để không bỏ qua giới hạn tần suất.
Nếu cấu hình/yêu cầu đổi trong lúc đọc OKX, bot không ghi đè. Sau khi khôi phục,
operator tự kiểm tra Min/Max, giá vốn, vị trí rồi bật Auto khi sẵn sàng.


## BUY: persistent remaining VND budget

Auto BUY requires no manual Min/Max entry. It preserves exchange Min, records the initial Max as a VND budget, and subtracts authenticated completed BUY order fiat amounts across all replacement ad IDs. It also sends remaining cryptoAmount so an OKX replacement cannot restore the original quantity. Pending purchases block repricing until resolved; cancelled purchases do not consume budget.

Example: budget 158,532,000 VND, completed purchase 132,475,000 VND (5,000 USDT): remaining budget 26,057,000 VND with USDT quantity recalculated from that remaining VND budget at each new price. Max is the remaining VND budget. Crypto stock is rounded up to the authenticated USDT/VND cryptoScale (currently 2 decimals) from (remaining VND + 1) / price so that Max fits below its value; this does not increase the VND spending limit. Changing price never replenishes spent VND. If below Min or exhausted, hide the ad; never reopen a manually hidden ad.

Tables okx_buy_budgets / okx_buy_budget_ads / okx_buy_budget_orders persist budget, ID lineage and deduplicated orders. okx_pending_buy_budgets verifies replacement quantity before confirmation or operator recovery. Initial bootstrap traces confirmed automatic BUY replacements in the audit journal and reads paginated authenticated history. Missing/inconsistent history blocks a write. New unrelated/manual replacement ads start a separate budget. No credentials or private order records belong in this repository.


Quantity precision is read from authenticated GET /api/v5/p2p/currency/currency-pair-list?fiatCurrency=VND. Missing, duplicated or invalid USDT/VND precision blocks the write. Round upward at cryptoScale before checking the fiat capacity; never send an eight-decimal amount that becomes too small after OKX truncates to two decimals. Example: budget 190,000,000 VND at 26,222 requires 7,245.83 USDT, not 7,245.82415529 or 7,245.82. The VND Max remains 190,000,000.
