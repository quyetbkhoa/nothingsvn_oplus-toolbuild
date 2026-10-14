# ColorNT Toolbuild 0.5.0rc3

GUI và CLI dùng chung pipeline: chọn ROM A của phần cứng máy nhận, ROM B lấy
hệ điều hành, nơi lưu output, rồi tạo candidate mới. Mọi mod mặc định OFF.

## Cài trên máy Windows khác

1. Tải ZIP tool từ GitHub Releases và giải nén vào folder riêng trên ổ NTFS có
   dung lượng trống. Cài Python 3.11 trở lên, có Tk, chọn Add Python to PATH.
2. Chạy `Setup.cmd`; công cụ chỉ chuẩn bị dependency trong folder tool.
3. Chạy `ColorNT.cmd`. Chọn A, B và nơi lưu output trong GUI. Không cần nhập
   adapter, capacity bytes hay chỉnh JSON. CLI tương đương ở bên dưới.
4. Để bật bộ mod v1.4: tải thêm ZIP dữ liệu mod cùng release, bấm **Nhập dữ liệu
   mod v1.4…**. Checksum/link và metadata của từng file được đối chiếu recipe;
   import sai hoặc thiếu sẽ bị chặn. Không ghi đè bộ dữ liệu đã có.
5. Full v1.4 cần WSL2 được cài trước. Bấm **Chuẩn bị SELinux (WSL2)…** một lần
   hoặc chạy `Setup-Policy.cmd`. Tool tạo distro `ColorNT-Policy-Portable` riêng,
   không thay distro đang dùng và không tự bật các Windows features.
6. Tick bộ mod v1.4 nếu cần, rồi **Tạo ROM**. Mỗi job tạo folder mới và giữ
   báo cáo, nguồn build, manifest, checker, flasher và checklist bên trong.

Không cần ổ W:/H:, cache phân tích hay thư mục ROM của máy đã dựng tool.
Workspace mặc định nằm trong folder tool hoặc `.colornt-work` bên cạnh output.
Disk được kiểm tra trước và trong quá trình build. Build từ ROM mới cần nhiều
dung lượng cho extraction, stage và roundtrip; chọn ổ NTFS trống rộng. Không
để output/workspace nằm trong ROM input hoặc folder candidate đã tạo trước.

## CLI

```powershell
python toolbuild.py port --base "D:\ROM-A" --donor "D:\ROM-B" --output-parent "E:\Built-ROMs" --plan
python toolbuild.py port --base "D:\ROM-A" --donor "D:\ROM-B" --output-parent "E:\Built-ROMs"
python toolbuild.py import-v14-pack "D:\Downloads\ColorNT-v14-data-pjf110-cph2745-20261014.zip"
python toolbuild.py setup-policy
python toolbuild.py port --base "D:\ROM-A" --donor "D:\ROM-B" --output-parent "E:\Built-ROMs" --v14-full
```

Folder IMG có metadata firmware hoặc ZIP đầy đủ được nhận; OTA incremental,
super.img chưa tách logical IMG và cặp chưa có compatibility rules sẽ bị chặn.
GUI/CLI hỗ trợ thêm APK thường, IMG thay phân vùng và boot/reboot animation.
Không tự chạy script trong ROM và không tự flash điện thoại.

## Phạm vi đã biết

- Core port đầu tiên: Ace 3V/PJF110 `16.0.5.1001(CN01)` nhận OS của OnePlus
  15/CPH2745IN `16.0.10.601(EX01)`, Android16. Một candidate port thuần đã được
  người dùng xác nhận boot. Kết quả đó không chuyển sang candidate mới.
- Bộ v1.4 mang NTManager, NexAlloy, InstallerX, xeutoolbox, framework/services,
  permissions, SELinux/init, UI/Settings/launcher/SystemUI, theme và các dữ liệu
  trong recipe ghim. Tái tạo đúng cặp và full variant đã quan sát; chưa phải
  engine patch từng mod tùy ý cho mọi ROM Oplus. Repo legacy
  `tungtx57-pixel/nothingsvn_oplus-toolbuild` **chỉ dùng tham khảo**.
- ROM output dùng metadata AVB unsigned theo phương pháp cài trên máy unlocked.
  Có kiểm chứng native extract/repack/extract, nội dung/link và Android metadata.
  Host SELinux compile PASS có audit neverallow FAIL riêng, không chứng minh
  SELinux load hoặc mod chạy được trên máy thật.
- Kiểm chứng chữ ký APK đã ghi nhận Settings/SystemUI có CHUNKED_SHA256 mismatch;
  không quảng cáo toàn bộ APK đạt chuẩn chữ ký. Tương thích runtime cần kiểm thử.
- Full v1.4 thêm `my_company`/`my_preload`; flasher yêu cầu logical partitions
  đã tồn tại. Không tự tạo phân vùng. Phần này cần kiểm tra trên máy người dùng.
- Full VINTF/APEX/kernel/linker solver, generic mod transformers, OTA signing
  và mọi thiết bị/region chưa thử vẫn chưa hoàn thành. Đây là release candidate,
  chưa được nghiệm thu đầy đủ về boot, phần cứng, mod hay độ ổn định.

## Kiểm tra bản build

Chạy `python Check-Candidate.py` trong folder output trước khi dùng. Checker
đối chiếu IMG và metadata với manifest đã đóng băng; PASS là kiểm tra offline.
Đọc `USER-TEST-CHECKLIST.md`, tự cài bằng flasher đi kèm và báo candidate ID cùng
log nếu lỗi. Giữ bản stock hoạt động tốt để phục hồi. Tool không tự flash/wipe.

Flasher có lựa chọn giữ/wipe data lúc vào, log màu, OKAY thẳng hàng, lỗi đỏ,
stop/retry/skip và progress 0–100% theo bước. Full log ghi lỗi lên đầu.
Chỉ IMG nằm trong allowlist được chọn; file lạ không được flash.

## Dependency và source

EROFS, payload-dumper và AVB dùng upstream/version/checksum trong
`tools/lock.json`. Compiler SELinux static có source, relink object và license
trong asset `ColorNT-policy-corresponding-source-20261014.zip` của cùng release.
Source secilc/libsepol 3.10 và musl 1.2.6 được kèm theo; compiler dùng cho kiểm
chứng host, không được inject vào ROM. Bộ mod dữ liệu là payload từ phiên bản
tham chiếu đã audit; không chứa full ROM A/B hay output ROM để flash.
