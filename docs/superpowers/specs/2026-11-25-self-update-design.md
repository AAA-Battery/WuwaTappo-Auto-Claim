# Thiết kế: nút "Check for update" và tự cập nhật

Ngày: 2026-11-25 · Trạng thái: chờ duyệt

## Mục đích
User hiện phải tự tải zip trên GitHub, giải nén rồi chạy lại exe. Mục tiêu: trong app có nút kiểm tra bản mới và một nút bấm để cập nhật xong, giữ nguyên thư mục cài, dữ liệu và phiên đăng nhập.

## Mục tiêu / Không phải mục tiêu
- Có: kiểm tra bản mới, tải, kiểm tra toàn vẹn, thay file, khởi động lại; tự quay về bản cũ nếu thay file lỗi.
- Không: cập nhật delta, ký số exe, tự cài không cần bấm, kênh prerelease, hạ phiên bản, cập nhật chính Wuwa Tappo.
- Phản mục tiêu: một bản cập nhật lỗi hoặc bị sửa không được làm app của user hỏng hay chạy mã lạ (app chạy quyền admin và giữ phiên đăng nhập Tappo).

## Các hướng đã cân nhắc
- **A. Tự cập nhật trong app (đã chọn):** tải zip release, so SHA-256, thay file bằng script phụ, mở lại.
- B. Chỉ báo bản mới và mở trang release: an toàn nhưng user vẫn tự giải nén (không giải quyết điều bạn muốn).
- C. File cài đặt `setup.exe`: nhiều việc hơn, vẫn bị cảnh báo vì chưa ký số.

## Thiết kế (các lựa chọn của mình, bạn có thể sửa)
**Nguồn:** `GET https://api.github.com/repos/AAA-Battery/WuwaTappo-Auto-Claim/releases/latest` (không cần token). Tag dạng `vX.Y.Z`, so với `VERSION` bằng `version_key` có sẵn. Chỉ nhận release thường (không draft, không prerelease).

**Tài sản release bắt buộc:** `WuwaTappoAutoClaim-vX.Y.Z.zip` và `WuwaTappoAutoClaim-vX.Y.Z.zip.sha256` (chuỗi hex). Thiếu file hash thì không cho cập nhật. Giới hạn: hash cùng nằm trên GitHub nên chống được tải lỗi hoặc bị can thiệp trên đường tải, nhưng không chống được tài khoản GitHub bị chiếm. Ký số là việc riêng, ngoài phạm vi.

**Tải về:** chỉ chấp nhận URL tải từ `github.com/AAA-Battery/WuwaTappo-Auto-Claim/releases/download/`, chuyển hướng chỉ được tới `github.com` hoặc `*.githubusercontent.com`; trần kích thước 300 MB; lưu vào `%LOCALAPPDATA%\WuwaTappoAutoClaim\update\`.

**Giải nén an toàn:** từ chối đường dẫn tuyệt đối hoặc chứa `..`; zip phải chứa `WuwaTappoAutoClaim.exe` và `_internal`; giải vào thư mục tạm rồi mới kiểm tra.

**Giao diện:** thêm một dòng trong hộp Settings: "Check for updates" + nút Check. Kết quả hiện ngay trên dòng đó: "Up to date (v1.2.2)", "v1.3.0 available" (nút đổi thành Update now), hoặc lỗi. Khi mở app sẽ tự kiểm tra ngầm một lần; nếu có bản mới thì chỉ hiện chữ báo, không tự cài.

**Thay file:** app tắt chính nó rồi chạy một script PowerShell đặt ngoài thư mục cài (file đang chạy không tự ghi đè được). Script: chờ tiến trình thoát (tối đa 60 giây), sao lưu bằng cách đổi tên `WuwaTappoAutoClaim.exe` và `_internal` thành `.old` trong cùng thư mục (tức thì, không chép dữ liệu, không tốn thêm dung lượng), rồi chép bản mới vào (giữ nguyên file lạ user tự thêm ở thư mục gốc); lỗi giữa chừng thì xóa phần đã chép và đổi tên `.old` về lại; cuối cùng mở lại exe và, khi bản mới đã chạy được, xóa `.old`. Zip và thư mục giải nén tạm cũng được xóa sau khi xong (đỉnh dung lượng tạm khoảng 120 MB, sau đó gần như bằng 0). Ghi log vào `update\update.log`. App chạy sẵn quyền admin nên script không bị hỏi UAC lần nữa.

**Khi chạy từ mã nguồn (không đóng gói):** nút chỉ mở trang release, không tự thay file.

**Quy trình phát hành:** thêm `tools/make_release.py`: đóng gói `dist\WuwaTappoAutoClaim` + README/LICENSE/vbs thành zip đúng tên và sinh file `.sha256`; bạn tải cả hai lên GitHub Release. Cập nhật README.

**Mạng:** dùng `requests` có tôn trọng proxy của hệ thống (khác với client gọi API Tappo vốn tắt proxy), giới hạn thời gian chờ, mọi lỗi mạng chỉ hiện thông báo, không làm hỏng việc claim.

## Kiểm thử
Test đơn vị cho: so sánh phiên bản, đọc release, kiểm tra hash (đúng, sai, thiếu), từ chối zip có đường dẫn nguy hiểm, kiểm tra URL, tải bằng máy chủ giả. Script thay file thử thật trên thư mục tạm giả lập (cả trường hợp lỗi để kiểm tra khôi phục).

## Lưu ý
- User đang dùng 1.2.2 không có tính năng này: lần đầu họ phải cài thủ công bản có updater, từ đó trở đi mới tự cập nhật.
- Đây là một bản phát hành mới (đề xuất v1.3.0); nó sẽ gồm cả hai bản sửa lỗi 70003 và đồng hồ vừa làm.
- Việc xây dựng và đẩy bản lên GitHub là phần bạn làm; mình không tự phát hành.
