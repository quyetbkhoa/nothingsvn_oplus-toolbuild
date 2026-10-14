# Phương pháp dựng port đã boot — 2026-10-14

Candidate `PJF110-CPH2745-minimal-port-20261014-000001`,
`H:\ColorNT-work\port-20261014-000001\ROM`;
manifest SHA256 `5540d3e526fc303ba648763d4216b24996b691f64e5728b300a07354962b3e3e`.
User xác nhận flash được và điện thoại đã lên. Đây là **port thuần, mods OFF**;
không thêm Toolbox/NTManager/NexAlloy/InstallerX/framework-mod pack.
Hardware/stability chưa nghiệm thu. Mục tiêu tool đầy đủ theo
[yêu cầu v1.4](PORT-TOOL-REQUIREMENTS.md) vẫn còn hiệu lực.

## Inputs và request tái lập

A: `W:\ColorNT-transfer\ROM-Inputs\PJF110_16.0.5.1001(CN01)`.
B: `W:\ColorNT-transfer\ROM-Inputs\CPH2745_16.0.10.601(EX01)`.
Request thật: `H:\ColorNT-work\port-20261014-000001\request.json`.
Source snapshot thật: cùng job `/source-snapshot/INDEX.json`, SHA256
`d777e3739381eca5afbb0ef94e23271f488e03eaac56453181eb9dceff5ddc40`.
Native build dùng `colornt.desktop_build.build`, không dùng stock-mod output làm ROM.

```json
{
  "mods": {},
  "installation_policy": "unlocked-rebuilt-metadata-v1",
  "cleanup_verification": true
}
```

Output/work phải là folder mới trên H. Cache A đã được chọn explicit từ stock
extraction của job kiểm chứng; cache không đủ nguồn/partition/metadata thì phải
giải nén lại. Product BuildJob dùng registry/fingerprint/cache-integrity; không
tự tin tưởng cache nghiên cứu cũ chỉ vì tồn tại. Không hash toàn bộ ROM input.

## Core transforms

1. Giữ A boot/init_boot/vendor_boot/dtbo, firmware/radio/boot chain, DLKM, vendor
   và ODM; chọn userspace/OS partitions từ B, output layout theo A, bỏ device data.
2. Chọn profile nội bộ `profiles/desktop-pjf110-cph2745.json` đúng hai version.
   Ghép65 file hardware A, trong đó VNDK34 APEX phục vụ vendor34, cùng audio/
   display/sensor/SKU files và overlays. Policy35 key nằm ở
   `recipes/oplus-a16-in-core-properties.json`; giữ version/userspace B.
3. Với installation policy trên, repack13 EROFS: my_bigball, my_carrier,
   my_engineering, my_heytap, my_manifest, my_product, my_region, my_stock, odm,
   product, system, system_ext, vendor. Giữ UID/GID/mode/capability/SELinux,
   links/empty directories, UUID/build time/mtime/cluster options từ metadata.
4. Case-sensitive stage/inventory bằng os.walk và string keys; không dùng
   Path.rglob để gộp ES-Vodafone/es-vodafone. Không bỏ qua content mismatch.
5. Kiểm tra staged ARM64 executables, zygote/imports và VNDK APEX structure.
   Không gọi check này là full ELF symbol/kernel/VINTF/APEX compatibility solver.

## AVB và kiểm chứng

Rebuilt IMG có hashtree/footer mới bằng avbtool đã pin: algorithm NONE,
SHA256, salt rỗng, block4096, không FEC; verify_image từng ảnh. Không dùng child
descriptors cũ để mô tả payload mới. Regenerate vbmeta_system từ system/
system_ext/product và vbmeta_vendor từ vendor, flags0. Root canonical unsigned
flags3 theo `unlocked-top-level-disabled-v1`. Giữ rollback fields theo metadata
được chuẩn bị; không tái tạo chữ ký OEM hoặc tuyên bố dùng trên máy locked.

Re-extract13 ảnh và đối chiếu round-trip:20514 files/links,4020 directories PASS.
48 IMG/12069601280 byte; dynamic11160182784/16101933056. Independent package gate
`experimental-package-ready` và checksum/offline flash plan PASS. Đây là policy
cài thử unlocked; flasher vẫn kiểm tra máy/unlocked/slot/mode/capacity thật.

QA bổ sung của đúng candidate: stock control + CIL stage v33 host compile PASS;
3 overlay + B framework-res signatures/platform signer PASS. A factory AI archive
không có chữ ký APK chuẩn, byte unchanged; custom-loader trust chưa kiểm chứng.
Full VINTF/kernel/APEX/linker-symbol solver NOT_RUN. Không biến host QA thành
boot proof; bằng chứng boot là phản hồi riêng từ user.

## Cách dùng pipeline mới

GUI chọn A/B, nơi lưu output và giữ các mod OFF. Điều kiện cài đặt mặc định
“Máy đích đã unlock bootloader” dựng fresh installation metadata như candidate.
Tắt điều kiện này giữ metadata nguồn và có thể trả package-blocked.

```powershell
python toolbuild.py port --base "W:\ColorNT-transfer\ROM-Inputs\PJF110_16.0.5.1001(CN01)" --donor "W:\ColorNT-transfer\ROM-Inputs\CPH2745_16.0.10.601(EX01)" --output-parent "H:\ColorNT-work\user-builds" --plan
```

Bỏ `--plan` để build local thật qua cùng BuildJob với GUI. Không gọi nhánh `/bop`
legacy/replay khi muốn pipeline port thuần này. APK/IMG/animation là opt-in;
fresh AVB không cho IMG C bỏ các core repacks/vbmeta cần thiết. Cặp khác được
phân tích hoặc chặn khi thiếu contract, không giả sử đã hỗ trợ.

Sau gate, output tự có BUILD-MANIFEST/BUILD-REPORT/BUILD-REQUEST, SOURCE-MAP,
BUILD-SOURCE code/tool/rules, SUPPLEMENTAL-EVIDENCE, candidate ID, checker,
checklist, delivery record và flash bundle. Input preservation là size/mtime;
checksum IMG chỉ ghim output; file cache/asset được đối chiếu theo recipe khi
cần tái tạo mod. CIL/APK QA chỉ reuse khi identity + đúng bytes
khớp reference đã kiểm; nếu khác ghi NOT_RUN, không đổi thành PASS.
Mỗi output mới là NOT_TESTED/AWAITING_USER dù cặp/version cũ đã boot.

Không sửa candidate đã giao. Variant có mod phải là build mới, giữ port thuần
và stock-mod làm control; đối chiếu feature dependency trước, user tự test sau.
Nghiệm thu port thuần không đồng nghĩa tool đã tái tạo đầy đủ mod/toolbox v1.4.

## Variant v1.4 trong app0.4.2

Thêm `--v14-full` vào lệnh `port`, hoặc bật bundle v1.4 trong GUI. Cùng A/B,
output picker, fresh unlocked metadata và candidate freeze; pipeline stage
dùng recipe OOS v1.4 ghim checksum, source map25.315 files/links,14 EROFS.
Không thay JAR OOS bằng JAR stock-mod ColorOS. Các APK NTManager/NexAlloy,
payload nhúng/JNI và xeutoolbox có hash trùng v1.4 đã audit, nhưng framework/
policy dùng đúng nguồn OOS theo version. Backend này chưa là transformer tổng quát.

Cache được preflight source/header/path/type/size; mọi file được recipe chọn
phải khớp checksum tại stage trước repack. Không hash cùng file hai lần chỉ để
preflight rồi copy; bước stage vẫn là barrier bắt buộc trước khi tạo IMG. Cache
B của port thuần thiếu ODM, nên giải nén riêng ODM B ở workspace mới để đọc
90 preset OPAXG. Nguồn partition bổ sung được ghim vào request/report, không
ghi vào cache cũ hoặc input. Không hardlink các file input sang stage.

Compile current staged CIL policy33 trên host theo chế độ split-policy loader
`-N`, giữ audit neverallow riêng (hiện FAIL); không inject policy vào ROM.
Chữ ký chuẩn của NTManager/NexAlloy/InstallerX/launcher PASS, Settings/SystemUI
patched có CHUNKED_SHA256 mismatch. Các kiểm tra này không chứng nhận platform
trust/runtime. Giao candidate riêng và test bridge/hooks/mod cùng hardware.
