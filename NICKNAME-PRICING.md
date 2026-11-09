# Bám giá theo biệt danh trên Discord

Trong bảng điều khiển bám giá OKX hoặc MEXC, bấm **Bám biệt danh Mua** hoặc
**Bám biệt danh Bán**, nhập slot 1/2, biệt danh đối thủ và chênh giá VND/USDT.
Để trống chênh giá = 0. Ví dụ đối thủ 26.090, chênh -1 → 26.089;
+1 → 26.091. Cùng một phép cộng áp dụng cho cả Mua và Bán.

Cấu hình riêng theo tài khoản/schema, sàn, chiều và slot. Không cần biến môi
trường hay khóa API mới. Bảng `pricing_nickname_targets` được tạo bằng cơ chế
khởi động hiện có. Lưu mục tiêu giữ nguyên công tắc Auto; nếu Auto đang tắt,
chủ tài khoản cần tự bật. Không gửi yêu cầu sửa quảng cáo khi chỉ lưu biểu mẫu.

Nguồn vẫn là bảng AutoP2P https://autop2p.biz/ đúng sàn và đúng chiều, theo
cơ chế đọc hiện có. So khớp đầy đủ biệt danh (không phân biệt hoa/thường,
bỏ khoảng trắng hai đầu), không khớp gần đúng. Không theo chính chủ.
Nếu không tìm thấy trong bảng trả về, nguồn quá cũ/sai, hoặc một biệt danh có
nhiều quảng cáo khác giá: giữ giá hiện tại và cảnh báo; không tự chuyển sang
người khác/vị trí khác. Nhiều dòng cùng biệt danh và cùng giá được chấp nhận.

Giá sau cộng chênh lệch vẫn qua toàn bộ kiểm tra và giới hạn hiện có:
giá vốn bán +10đ, Min/Max/ngân sách, quyền tài khoản/kênh, Auto, công tắc chung,
giãn cách sửa giá tối thiểu và khóa yêu cầu chưa rõ kết quả. Không bật lại
quảng cáo bị chủ tài khoản đóng thủ công. Worker bỏ kế hoạch cũ nếu cấu hình
biệt danh/chênh giá thay đổi trong lúc chờ đọc sàn.

Để trở lại bám vị trí, lưu **Vị trí Mua/Bán**: OKX xóa mục tiêu biệt danh của
cả hai slot cùng chiều (vì vị trí OKX hiện dùng chung theo chiều); MEXC chỉ
xóa mục tiêu biệt danh của slot được chọn. Trạng thái hiển thị chế độ hiện tại.

Kiểm thử mô phỏng không gửi giao dịch thật:

```powershell
python -m pytest -q
```

Không dùng giá trong tài liệu làm cấu hình cho quảng cáo thật.
