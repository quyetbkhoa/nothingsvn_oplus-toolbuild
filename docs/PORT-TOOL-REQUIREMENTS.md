# Yêu cầu toolbuild — chuẩn v1.4, 2026-10-14

User xác nhận trực tiếp: **bản toolbuild/mod v1.4 mới là chuẩn feature**;
repo legacy chỉ dùng tham khảo. Mục tiêu vẫn là tool Oplus hoàn chỉnh qua GUI
Windows và toolbuild CLI, không hoàn thành chỉ bằng pipeline port thuần.

Tên hiển thị chuẩn của source legacy:
**Chỉ dùng tham khảo — tungtx57-pixel/nothingsvn_oplus-toolbuild**.
Identity/provenance giữ nguyên commit `b0331c4817c07f431f718bde69c8adf2331e4ef4`,
path `reference/legacy`, artifact `shared.source.legacy`. Nhãn này không đổi
upstream repository hoặc lịch sử Git. Source v1.0/KaoriosToolbox của legacy
không phải chuẩn nghiệm thu đầy đủ feature của bản v1.4/NTManager/NexAlloy.

## Backend hiện tại

Source0.4.2 thêm lựa chọn full bundle v1.4 đúng cặp/version hiện có qua GUI/CLI.
Nó tái tạo source map và dependency integration của mẫu OOS đã audit, dựng lại
metadata cài đặt bằng pipeline port thuần đã boot. Module evidence/CIL/round-trip
và checksum candidate phải được kiểm chứng trước giao; không kế thừa boot PASS.
Đây là bước triển khai theo version, chưa thay thế các yêu cầu engine tổng quát
hoặc nghiệm thu từng feature/runtime bên dưới.

## Giao diện và build

- Chọn A = ROM của phần cứng đích, B = OS donor; folder FFF hoặc ZIP.
- Chọn nơi lưu output qua GUI hoặc CLI `--output-parent`; luôn tạo candidate
  mới, không ghi đè hay lồng output vào ROM đã freeze.
- Port chéo Oppo/Realme/OnePlus, ColorOS/RealmeUI/OxygenOS là mục tiêu sản phẩm.
  Ace 3V/PJF110 ← OnePlus15/CPH2745 là cặp đầu có bằng chứng boot, không phải
  phạm vi toàn bộ tool. Không bắt user viết JSON/adapter/capacity.
- Tách hardware port khỏi mod; tất cả mod mặc định OFF. Cho chọn APK, IMG C,
  bật/tắt feature thực sự có backend; báo dependency/conflict/điều kiện còn thiếu.
- GUI và CLI dùng cùng BuildJob/engine, recipe/source map, tools pin, guards
  dung lượng/cancel/cache và metadata Android. Không chạy input scripts.
- Port thuần vừa boot phải được tái lập bằng pipeline chung: fresh repacks,
  fresh unsigned installation metadata cho máy unlocked và kiểm chứng đầy đủ.

## Feature parity bắt buộc với v1.4

Đối chiếu ROM mod v1.4 đã audit/stock-mod đã chấp nhận và evidence output OOS;
ghi nguồn/version/precondition/dependency/tác động/test cho từng nhóm:

1. NTManager, NexAlloy/payload native, framework/services/Oplus hooks, permissions,
   init/SELinux và bridge management. Chép APK đơn lẻ không đủ.
2. Toolbox v1.4/xeutoolbox và các hành vi đã audit; không thay bằng Kaorios legacy.
3. InstallerX, app/debloat/region selection, GMS/dialer và app integration.
4. Settings/SystemUI/launcher, clone-app/feature XML, resource/branding.
5. Boot/reboot animation, splash, themes/icons, audio/OPAXG và hardware conditions.
6. OTA/config/certificate/Manager bridge và các chế độ đóng gói thật sự được quan
   sát ở v1.4; signing/authentication phải ghi đúng giới hạn.
7. Các mod integrity/privacy/game hoặc behavior khác được chứng minh trong audit
   v1.4. Không suy diễn số method/class/file thành số feature hoạt động.

Replay payload ghim đúng một version là một backend có scope cụ thể, chưa thay
cho transformer tổng quát. Không copy JAR ColorOS vào OOS khác phiên bản, không
gán mod/security bypass thành yêu cầu bắt buộc của port. Các hook/security/OTA
patch là lựa chọn có dependency và bằng chứng riêng. OFF phải giữ payload nguồn.

## Kiểm chứng và nghiệm thu

Manifest/request/source snapshot/source map/checker/checklist đi cùng output;
EROFS round-trip đối chiếu content/link/UID/GID/mode/capability/SELinux và tên
case-sensitive. Budget/AVB/package/compatibility checks phải đúng scope.
Mỗi candidate mới bắt đầu boot/hardware NOT_TESTED, AWAITING_USER; user tự cài/test.
Không kết luận full feature parity từ checkbox, unit tests, hoặc một bản port
thuần boot. Stock-mod acceptance và port-core boot không chuyển sang variant mới.

Bảo toàn inputs/reference/stock-mod/port đã boot. W tổng ≤200000000000 byte;
heavy work trên H. Không agent flash/wipe/reboot, push/publish/upload/notify hoặc
auto-commit. Feature có khả năng upload/notify nếu cần parity chỉ được chạy khi
user cấp quyền cho hành động cụ thể, không tự gửi trong build/test.

## Trạng thái hiện tại

Port core mods-OFF đã boot theo user; hardware/stability còn chờ. Cập nhật source
GUI/CLI0.3.5 có output picker, common product pipeline, fresh AVB, auto freeze,
checker và source snapshot; **chưa là toolbuild đầy đủ feature v1.4**.
Backend tích hợp/generic transformer các nhóm trên còn phải triển khai và test.
Đọc active state trước khi resume; giữ mục tiêu full v1.4, không đổi sang legacy.
