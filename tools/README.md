# External tools

Boot diagnosis13-10 uses a separate optional WSL2 distro `ColorNT-Policy`, rooted
at `H:\ColorNT-work\42ee7b542eb0\policy-runtime`. Alpine3.24.2 rootfs, immutable
upstream secilc3.10 source and direct GCC/libsepol/OpenSSL APK dependencies are
pinned in lock.json. It compiled stockA and both port CIL compositions successfully;
it does not prove kernel policy load or replace the app's Windows EROFS tools.
Existing Ubuntu/default distro were not changed. The owned runtime was terminated
after checks; it can be launched by name for future host QA. Source BSD license
header is retained locally. Upstream/GCC/libsepol/OpenSSL licenses still apply;
these runtime packages/binaries are not vendored into Git or the app installer.

Versions, download URLs, archive SHA-256 and source repositories are in `lock.json`.
Binaries live only in ignored `tools/local/`. These hashes authenticate the exact
tool downloads used here; they are not new checksum checks of the user's ROMs.

- EROFS: [sekaiacg/erofs-tools](https://github.com/sekaiacg/erofs-tools), release
  `v1.8.10-251217`, Cygwin x86_64, with its bundled cygwin1.dll. Supports extraction
  and Android fs_config/file_contexts. The wrapper converts Windows paths to
  `/cygdrive/<drive>/...`; no system Cygwin installation is needed.
- Payload: [ssut/payload-dumper-go](https://github.com/ssut/payload-dumper-go),
  release `2.1.0`, Windows amd64. Incremental OTA extraction requires source
  images (`-old`); the current wrapper does not supply those yet and must fail
  if the payload cannot be reconstructed from the available data.
- EXT content inspection: the installed [7-Zip](https://www.7-zip.org/) 25.01 is
  resolved from Program Files on Windows or PATH. The toolkit records an inode
  listing, but does not claim preservation of Android xattrs for EXT repacking.

Linux x86_64 releases are also pinned. Run `setup-tool erofs-linux` and
`setup-tool payload-linux` in a separate Linux checkout. The sample native
round-trip has been exercised on Windows; Linux execution remains untested.
Windows binaries are not used on Linux.

External components retain their upstream licenses. Source and release provenance
must be retained when redistributing them; this repository does not vendor binaries.
