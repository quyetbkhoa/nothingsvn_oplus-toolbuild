"""One local product workflow; technical configuration stays behind the UI."""
from pathlib import Path
import datetime
import hashlib
import json
import os
import re
import shutil
import time
import uuid
import zipfile
from contextlib import contextmanager

from . import desktop_build as engine
from .io import read_json, write_json, WRITE_GUARD, sha256
from .android_links import link_target
from .properties import Properties
from .tools import JOB_GUARD, ROOT

W_BUDGET = 200_000_000_000
RESERVE = 2 * 1024**3


class JobError(ValueError):
    def __init__(self, code, message, detail='', **actions):
        super().__init__(message)
        self.code, self.detail, self.actions = code, detail, actions


@contextmanager
def writer_lock(path):
    """One writer for the shared job registry; OS releases locks on process exit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open('a+b')
    locked = False
    try:
        if not path.stat().st_size:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise JobError('busy', 'Một cửa sổ ColorNT khác đang tạo ROM trong project này. Chờ job đó kết thúc.') from exc
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        # Keep the inode: unlinking a locked file permits another lock identity.
        stream.close()


def tree_bytes(path):
    path = Path(path)
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    def failed(error):
        raise error
    for current, dirs, files in os.walk(path, followlinks=False, onerror=failed):
        for name in files:
            try:
                total += (Path(current) / name).lstat().st_size
            except FileNotFoundError:
                # Native tools may rename an in-progress file during monitoring.
                continue
    return total


def volume(path):
    return Path(path).resolve().drive.upper() or '/'


def free_bytes(path):
    parent = Path(path).resolve()
    while not parent.exists():
        parent = parent.parent
    return shutil.disk_usage(parent).free


def source_stamp(summary):
    """Hash only structural metadata, never image content or the whole ROM."""
    source = Path(summary['path'])
    if source.is_file():
        s = source.stat()
        data = {'source': str(source), 'size': s.st_size, 'mtime': s.st_mtime_ns,
                'ctime': s.st_ctime_ns, 'identity': summary['identity']}
    else:
        records = []
        for name, item in sorted(summary['images'].items()):
            s = (source / item['path']).stat()
            records.append([name, item['path'], s.st_size, s.st_mtime_ns, s.st_ctime_ns, item['header']])
        data = {'source': str(source), 'identity': summary['identity'], 'images': records}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def local_config():
    path = ROOT / 'config/project.local.json'
    return read_json(path) if path.is_file() else {}


def settings_path():
    return ROOT / 'config/desktop.settings.json'


def load_choices():
    config = local_config()
    choices = {'base': config.get('base') or '',
               'donor': config.get('donor_folder') or config.get('donor_zip') or '',
               'output_parent': config.get('desktop_output_root') or str(ROOT / 'out')}
    if settings_path().is_file():
        try:
            saved = read_json(settings_path())
            choices.update({k: str(saved[k]) for k in choices if k in saved})
        except (ValueError, OSError):
            pass
    return choices


def save_choices(choices):
    write_json(settings_path(), {key: choices.get(key, '') for key in ('base', 'donor', 'output_parent')})


def select_rules(a, b):
    if not a.get('identity') or not b.get('identity'):
        return None
    for path in sorted((ROOT / 'profiles').glob('desktop-*.json')):
        adapter = read_json(path)
        if adapter.get('kind') != 'desktop-port-adapter':
            continue
        try:
            engine.check_identity(adapter, a['identity'], b['identity'])
            return path
        except ValueError:
            pass
    return None


def required_parts(adapter, summary, role, apps=False, fresh_avb=False, v14_bundle=False):
    if adapter:
        changed = {p.split('/')[0] for p in adapter['hardware_files']} | {'my_manifest', 'my_product', 'odm'}
        parts = ({p.split('/')[0] for p in adapter['hardware_files']} | {'system', 'my_manifest', 'my_product', 'odm'}) if role == 'base' else (changed - {'odm'}) | {'system'}
    else:
        parts = {'system', 'my_manifest', 'my_product', 'odm', 'system_ext'}
    if 'my_stock' in summary['images']:
        parts.add('my_stock')
    if v14_bundle:
        from .v14 import parts as v14_parts
        parts |= v14_parts(role)
    if fresh_avb:
        if role == 'base':
            parts.add('vendor')
        else:
            parts |= set(adapter['dynamic_partitions'] if adapter else summary['images']) & engine.OS_PARTITIONS
    # Extra APK collision scans are lazy in the engine; do not invalidate a
    # reusable core extraction merely because vendor/product have not been scanned.
    return sorted(parts & set(summary['images']))


def cache_manifest(path):
    """Exact Android names + inode/link stats; hash only bounded metadata files."""
    path = Path(path)
    fs = path / 'fs'
    records = []
    for p in engine.android_walk(fs):
        stat = p.lstat()
        target = link_target(p)
        kind = 'link' if target is not None else 'dir' if p.is_dir() else 'file'
        records.append([p.relative_to(fs).as_posix(), kind, stat.st_size if kind != 'dir' else 0,
                        stat.st_mtime_ns, stat.st_ctime_ns, stat.st_mode, target])
    metadata = {}
    for p in engine.android_walk(fs / 'config'):
        if p.is_file():
            if p.stat().st_size > 16 * 1024**2:
                raise ValueError('Oversized extraction metadata')
            metadata[p.relative_to(fs).as_posix()] = sha256(p)
    return {'schema': 1, 'entries': sorted(records, key=lambda r: r[0]), 'metadata_sha256': metadata,
            'scope': 'Exact paths/types/size/timestamps/link targets and small metadata; no source ROM or file-content hashes'}


def seal_cache(path, stamp):
    write_json(Path(path) / 'cache-integrity.json', cache_manifest(path))
    write_json(Path(path) / 'source-stamp.json', {'schema': 1, 'stamp': stamp})


def cache_ok(path, summary, parts, stamp=None, allow_legacy=False):
    """Validate source identity/headers and required artifacts, without ROM hashes."""
    path = Path(path)
    try:
        state = read_json(path / 'extraction.json')
        if state['status'] != 'complete' or Path(state['source']).resolve() != Path(summary['path']).resolve():
            return False
        marker = path / 'source-stamp.json'
        # Legacy extraction has no equivalent integrity evidence. Never adopt it.
        if not marker.is_file() or read_json(marker)['stamp'] != stamp:
            return False
        integrity = read_json(path / 'cache-integrity.json')
        if integrity.get('schema') != 1 or integrity != cache_manifest(path):
            return False
        records = {r['name']: r for r in state['partitions']}
        for name in parts:
            r = records[name]
            source = Path(summary['path']) / summary['images'][name]['path']
            if r['status'] != 'extracted' or r['image'] != summary['images'][name]['header']:
                return False
            if not marker.is_file() and source.stat().st_mtime_ns > (path / 'extraction.json').stat().st_mtime_ns:
                return False
            if not (path / 'fs' / name).is_dir():
                return False
            for suffix in ('fs_config', 'file_contexts', 'fs_options'):
                if not (path / 'fs/config' / f'{name}_{suffix}').is_file():
                    return False
            actual_paths = {name} | {p.relative_to(path / 'fs').as_posix() for p in engine.android_walk(path / 'fs' / name)}
            declared = {line.split()[0].rstrip('/') for line in engine.metadata_records(path / 'fs/config', name, 'fs_config')
                        if line.strip() and line.split()[0] != '/'}
            # Pinned extractor adds a synthetic lost+found default even for EROFS.
            if name + '/lost+found' not in actual_paths:
                declared.discard(name + '/lost+found')
            if declared != actual_paths:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def storage_fits(usage_w, work_need, output_need, free_work, free_output,
                 same_volume, work_on_w, output_on_w, reserve=RESERVE):
    additional_w = (work_need if work_on_w else 0) + (output_need if output_on_w else 0)
    return (usage_w + additional_w + (reserve if additional_w else 0) <= W_BUDGET and
            free_work >= work_need + (output_need if same_volume else 0) + reserve and
            free_output >= output_need + (work_need if same_volume else 0) + reserve)


class BuildJob:
    def __init__(self, selection, emit=lambda event: None, cancelled=lambda: False):
        self.selection = dict(selection)
        self.emit, self.cancelled = emit, cancelled
        self.config = local_config()
        self.work_base = Path(self.config.get('desktop_work_root') or ROOT / 'work').absolute()
        default_project = ROOT
        self.project = Path(self.config.get('project_data_root') or default_project).resolve()
        self.fallback = Path(self.config.get('desktop_fallback_root') or
                             Path(self.selection['output_parent']) / '.colornt-work').absolute()
        self.registry_path = self.work_base / 'session-index.json'
        self.registry = read_json(self.registry_path) if self.registry_path.is_file() else {}
        self.reused = {}
        self.session = None
        self.storage = {}
        self.monitored = []
        self.last_check = 0
        self.peak_w = 0
        self.scanned_growth_w = 0
        self.pending_w = 0

    def progress(self, text):
        self.guard()
        self.emit({'type': 'progress', 'message': text})

    def clean_transient(self):
        """Keep caches/reports; remove only generated trees of this owned session."""
        self.clean_prepare()
        work = getattr(self, 'build_work', None)
        if not work or not (work / 'build.json').is_file():
            return
        work = work.resolve(strict=True)
        if work.parent != self.session.resolve(strict=True) or read_json(self.session / 'job.json').get('owner') != 'ColorNT':
            raise ValueError('Transient cleanup ownership mismatch')
        for child in list(work.iterdir()):
            if not child.is_dir() or not (child.name in ('stage', '.colornt-bootstrap', 'packed') or child.name.startswith(('apk-scan-', 'verify-'))):
                continue
            if child.resolve().parent != work or child.is_symlink():
                raise ValueError('Transient cleanup escapes its session')
            for current, dirs, files in os.walk(child, followlinks=False):
                for name in dirs:
                    item = Path(current) / name
                    if item.is_symlink() or getattr(item.lstat(), 'st_file_attributes', 0) & 0x400:
                        raise ValueError('Cannot clean linked generated directory')
            archive = work / 'diagnostics' / child.name
            archive.mkdir(parents=True, exist_ok=True)
            for item in child.iterdir():
                if item.is_file() and item.suffix in ('.json', '.log'):
                    shutil.copyfile(item, archive / item.name)
            config = child / ('config' if child.name == 'stage' else 'fs/config')
            if config.is_dir():
                shutil.copytree(config, archive / 'config', dirs_exist_ok=True)
            shutil.rmtree(child)

    def clean_prepare(self):
        """Discard incomplete preparation, even before engine.build has started."""
        if not self.session or not (self.session / 'job.json').is_file():
            return
        owner = read_json(self.session / 'job.json')
        if owner.get('owner') != 'ColorNT' or Path(owner['project']).resolve() != self.project:
            raise ValueError('Preparation cleanup ownership mismatch')
        retained = []
        for item in self.registry.values():
            if not isinstance(item, dict) or 'extracted' not in item:
                continue
            try:
                summary = engine.source_summary(item['folder'])
                state = read_json(Path(item['extracted']) / 'extraction.json')
                parts = [r['name'] for r in state['partitions']]
                if cache_ok(item['extracted'], summary, parts, item['prepared_stamp']):
                    retained.extend([Path(item['folder']).resolve(), Path(item['extracted']).resolve()])
            except (OSError, ValueError, KeyError):
                pass
        for name in ('base', 'donor', 'input-base', 'input-donor'):
            child = self.session / name
            if not child.exists() or any(p == child.resolve() or p.is_relative_to(child.resolve()) for p in retained):
                continue
            if child.is_symlink() or child.resolve().parent != self.session.resolve():
                raise ValueError('Preparation cleanup escapes session')
            for current, dirs, _ in os.walk(child, followlinks=False):
                if any(getattr((Path(current) / n).lstat(), 'st_file_attributes', 0) & 0x400 for n in dirs):
                    raise ValueError('Cannot clean linked preparation directory')
            archive = self.session / 'diagnostics' / name
            archive.mkdir(parents=True, exist_ok=True)
            for item in child.iterdir():
                if item.is_file() and item.suffix in ('.json', '.log'):
                    shutil.copyfile(item, archive / item.name)
            shutil.rmtree(child)

    def clean_partial_output(self):
        output = getattr(self, 'current_output', None)
        work = getattr(self, 'build_work', None)
        if not output or not work or not output.is_dir():
            return
        report_path = Path(str(output) + '.build.json')
        if not report_path.is_file():
            return
        report = read_json(report_path)
        if output.is_symlink() or getattr(output.lstat(), 'st_file_attributes', 0) & 0x400:
            raise ValueError('Cannot clean a linked output root')
        if (report['status'] not in ('failed', 'cancelled') or Path(report['workspace']).resolve() != work.resolve()
                or Path(report['output']).resolve() != output.resolve() or
                output.resolve().parent != Path(self.selection['output_parent']).resolve()):
            raise ValueError('Partial output cleanup ownership mismatch')
        for current, dirs, files in os.walk(output, followlinks=False):
            for name in dirs + files:
                item = Path(current) / name
                if item.is_symlink() or getattr(item.lstat(), 'st_file_attributes', 0) & 0x400:
                    raise ValueError('Cannot clean linked output entry')
        shutil.rmtree(output)
        report['partial_output_removed'] = True
        write_json(report_path, report)

    def guard(self, force=False):
        if self.cancelled():
            raise JobError('cancelled', 'Đã dừng tạo ROM. Folder dở dang chưa phải ROM hoàn tất.')
        if not force and time.monotonic() - self.last_check < 10:
            return
        self.last_check = time.monotonic()
        if not self.session:
            return
        watched = []
        for p in sorted(self.monitored, key=lambda p: len(p.parts)):
            if volume(p) == 'W:' and not any(p == r or p.is_relative_to(r) for r in watched):
                watched.append(p)
        growth_w = sum(tree_bytes(p) for p in watched)
        self.scanned_growth_w, self.pending_w = growth_w, 0
        self.peak_w = max(self.peak_w, self.initial_w + growth_w)
        if self.peak_w > W_BUDGET:
            raise JobError('space', 'Dữ liệu dự án trên W đã chạm trần 200 GB. Chọn nơi lưu output trên H hoặc dọn dữ liệu tạm của app.')
        for path in self.monitored:
            if free_bytes(path) < RESERVE:
                raise JobError('space', 'Ổ đang dùng sắp hết dung lượng. Job đã dừng và giữ báo cáo để kiểm tra.')

    def reserve_write(self, path, size):
        """Admission accounting between scans; never write a Python chunk over W cap."""
        if size < 0:
            raise ValueError('Negative write size')
        if not self.session:
            return
        if volume(path) == 'W:':
            if self.initial_w + self.scanned_growth_w + self.pending_w + size > W_BUDGET - RESERVE:
                raise JobError('space', 'Ghi tiếp sẽ vượt ngân sách 200 GB trên W. Job đã dừng trước ghi.')
            self.pending_w += size
        if free_bytes(path) < size + RESERVE:
            raise JobError('space', 'Ổ đang dùng không đủ dung lượng dự phòng cho lần ghi tiếp.')

    def find_cache(self, summary, parts, role):
        key = source_stamp(summary)
        record = self.registry.get(key, '')
        if isinstance(record, dict):
            try:
                effective = engine.source_summary(record['folder'])
                wanted = parts or required_parts(None, effective, role)
                if (record['original_stamp'] == key and source_stamp(effective) == record['prepared_stamp'] and
                        cache_ok(record['extracted'], effective, wanted, record['prepared_stamp'])):
                    self.reused[role] = record
                    return Path(record['extracted'])
            except (OSError, ValueError, KeyError):
                pass
            record = ''
        candidates = [record] if summary['kind'] == 'fff-folder' else []
        # Historical research caches are protected and cannot be adopted without integrity.
        if summary['kind'] == 'fff-folder':
            candidates.append(str(self.work_base / ('gui-live-a' if role == 'base' else 'gui-live-b')))
        for candidate in candidates:
            if candidate and cache_ok(candidate, summary, parts, key):
                self.registry[key] = candidate
                return Path(candidate)
        if self.selection.get('mods', {}).get('v14_bundle'):
            candidate = self.config.get('desktop_v14_reference_caches', {}).get(role)
            if candidate:
                from .v14 import reference_cache
                if reference_cache(candidate, summary, role, content=False):
                    self.progress('Cache ' + role + ' đúng header/path/size; kiểm chứng từng byte tại stage trước đóng IMG.')
                    return Path(candidate)
        return None

    def allocate(self, summaries, rules):
        adapter = read_json(rules) if rules else None
        apps = bool(self.selection.get('mods', {}).get('apps'))
        fresh_avb = bool(engine.normalize_installation_request(self.selection).get('installation_policy'))
        self.caches = {}
        raw = extracted = stage = largest = apk_scans = 0
        for role, summary in zip(('base', 'donor'), summaries):
            full_v14 = bool(self.selection.get('mods', {}).get('v14_bundle'))
            parts = required_parts(adapter, summary, role, apps, fresh_avb, full_v14)
            cache = self.find_cache(summary, parts, role)
            self.caches[role] = cache
            sizing = engine.source_summary(self.reused[role]['folder']) if role in self.reused else summary
            parts = parts or required_parts(adapter, sizing, role, fresh_avb=fresh_avb)
            def expanded_size(item):
                return item.get('expanded_size', item.get('header', {}).get('expanded_size', item['size']))
            sizes = [expanded_size(sizing['images'][p]) for p in parts]
            if summary['kind'] == 'ota-zip' and role not in self.reused:
                from .ota import payload_info
                info = payload_info(Path(summary['path']))
                if info['minor_version'] != 0:
                    raise JobError('input', 'ZIP này là OTA cập nhật từng phần. Hãy chọn ROM đầy đủ hoặc folder đã khôi phục bằng FFF.')
                sizes = [p['new_size'] for p in info['partitions'] if p['new_size']]
                with zipfile.ZipFile(summary['path']) as z:
                    payload_size = sum(p.file_size for p in z.infolist() if p.filename.rsplit('/', 1)[-1] == 'payload.bin')
                raw += sum(sizes) + payload_size
            elif summary['kind'] == 'fff-zip' and role not in self.reused:
                raw += sum(i['size'] for i in summary['images'].values())
            if not cache:
                extracted += 4 * sum(sizes)
            # Conservative estimate; actual guard also runs during native processes.
            if adapter:
                changed = {p.split('/')[0] for p in adapter['hardware_files']} | {'my_manifest', 'my_product', 'odm'}
                if fresh_avb:
                    changed |= (set(adapter['dynamic_partitions']) & (engine.OS_PARTITIONS | {'vendor', 'odm'}))
                owned = changed & ({'vendor', 'odm'} if role == 'base' else changed - {'vendor', 'odm'})
                estimates = ([tree_bytes(cache / 'fs' / p) for p in owned] if cache else
                             [4 * expanded_size(sizing['images'][p]) for p in owned if p in sizing['images']])
            else:
                estimates = [4 * s for s in sizes]
            stage += sum(estimates)
            largest = max(largest, max(estimates, default=0))
            if apps:
                scan_parts = (set(sizing['images']) & engine.OS_PARTITIONS) if role == 'donor' else (set(sizing['images']) & {'vendor', 'odm'})
                cached_parts = {p['name'] for p in read_json(cache / 'extraction.json')['partitions']} if cache else set(parts)
                apk_scans += 4 * sum(expanded_size(sizing['images'][p]) for p in scan_parts - cached_parts)
        output_estimate = 2 * max(sum(expanded_size(i) for i in s['images'].values()) for s in summaries)
        if not output_estimate:
            output_estimate = raw or 2 * max(sum(expanded_size(i) for i in engine.source_summary(r['folder'])['images'].values()) for r in self.reused.values())
        apk_bytes = sum(Path(p).stat().st_size for p in self.selection.get('apks', []))
        output_estimate += apk_bytes
        output_estimate += sum(engine.inspect_image(p).get('expanded_size', p.stat().st_size) for p in engine.replacement_paths(self.selection.get('replacements', [])))
        work_estimate = raw + extracted + stage + largest + apk_scans + 2 * apk_bytes
        if self.selection.get('mods', {}).get('v14_bundle'):
            from .v14 import recipe as v14_recipe
            data = v14_recipe()
            part_bytes = {}
            for row in data['files']:
                part = row['path'].split('/')[0]
                part_bytes[part] = part_bytes.get(part, 0) + row['size']
            # Exact declared stage bytes plus one live repack/verification.
            work_estimate = raw + extracted + sum(part_bytes.values()) + 2 * max(part_bytes.values()) + apk_scans + 2 * apk_bytes
        parent = Path(self.selection['output_parent']).resolve()
        roots = [self.project] + [Path(s['path']) for s in summaries] + [self.work_base]
        roots += [Path(p) for p in self.registry.get('_outputs', [])]
        unique = []
        for p in sorted(roots, key=lambda p: len(p.parts)):
            if volume(p) == 'W:' and not any(p == r or p.is_relative_to(r) for r in unique):
                unique.append(p)
        self.initial_w = sum(tree_bytes(p) for p in unique)
        work_candidates = [self.work_base / 'case-jobs', self.fallback / hashlib.sha256(str(self.project).encode()).hexdigest()[:12]]
        # Native extraction has no proven size bound: keep its growth off budgeted W.
        if volume(self.project) == 'W:':
            work_candidates = [p for p in work_candidates if volume(p) != 'W:']
        for work in work_candidates:
            if volume(work) == 'H:' and not Path('H:/').exists():
                continue
            if storage_fits(self.initial_w, work_estimate, output_estimate, free_bytes(work), free_bytes(parent),
                            volume(work) == volume(parent), volume(work) == 'W:', volume(parent) == 'W:'):
                for s in summaries:
                    protected = Path(s['path']).resolve()
                    if work == protected or work.is_relative_to(protected):
                        raise JobError('input', 'Nơi làm việc trùng thư mục ROM. Hãy đổi nơi lưu.')
                from .workspace_fs import enable_case_sensitive
                from .workspace_prepare import prepare_case_flag, validate_owned_root
                protected = [s['path'] for s in summaries]
                protected += [self.config[k] for k in ('reference_output', 'build_candidate', 'repair_image') if self.config.get(k)]
                protected += list(engine.replacement_paths(self.selection.get('replacements', []))) + self.selection.get('apks', [])
                work = validate_owned_root(work, self.project, protected)
                marker = work / 'colornt-root.json'
                if marker.is_file():
                    owner = read_json(marker)
                    if owner.get('owner') != 'ColorNT' or Path(owner.get('project', '')).resolve() != self.project:
                        raise JobError('workspace', 'Nơi làm việc trùng dữ liệu khác. Chọn nơi lưu khác hoặc kiểm tra cấu hình cài đặt.')
                elif any(work.iterdir()):
                    raise JobError('workspace', 'Không tự nhận thư mục làm việc đã chứa dữ liệu không thuộc app.')
                prepare_case_flag(work, {})
                write_json(marker, {'owner': 'ColorNT', 'project': str(self.project)})
                self.session = work / ('job-' + uuid.uuid4().hex[:12])
                self.session.mkdir()
                enable_case_sensitive(self.session)
                self.monitored.append(self.session)
                self.storage = {'usage_w_before': self.initial_w, 'limit_w': W_BUDGET,
                                'estimated_work_bytes': work_estimate, 'estimated_output_bytes': output_estimate,
                                'work_volume': volume(work), 'output_volume': volume(parent),
                                'estimate_note': 'Expanded-size estimates + 2 GiB headroom, not a native bound; unbounded native work uses H on Windows; Python writes admitted before chunks'}
                write_json(self.session / 'job.json', {'schema': 1, 'owner': 'ColorNT', 'project': str(self.project),
                                                      'status': 'preparing', 'storage': self.storage})
                return
        raise JobError('space', 'W hoặc H không đủ chỗ cho job trong ngân sách 200 GB trên W. Hãy đổi nơi lưu output hoặc dọn dữ liệu tạm.',
                       f'work estimate={work_estimate}; output estimate={output_estimate}; W used by project={self.initial_w}')

    def prepare(self):
        if not all(self.selection.get(k) for k in ('base', 'donor', 'output_parent')):
            raise JobError('input', 'Hãy chọn ROM máy nhận A và ROM lấy hệ điều hành B.')
        engine.validate_options(self.selection.get('mods', {}))
        from .product import check_output_parent
        check_output_parent(self.selection['output_parent'])
        self.progress('Đang đọc thông tin hai ROM…')
        summaries = [engine.source_summary(self.selection[k]) for k in ('base', 'donor')]
        rules = select_rules(*summaries)
        self.validate_early(summaries, read_json(rules) if rules else None)
        self.allocate(summaries, rules)
        prepared = {}
        adapter = read_json(rules) if rules else None
        for role, summary in zip(('base', 'donor'), summaries):
            self.progress('Chuẩn bị ROM ' + ('A' if role == 'base' else 'B') + '…')
            if role in self.reused:
                folder, identity = Path(self.reused[role]['folder']), self.reused[role]['identity']
            else:
                folder, identity = engine.prepare_source(Path(summary['path']), self.session / ('input-' + role))
            effective = summary if summary['kind'] == 'fff-folder' else engine.source_summary(folder)
            parts = required_parts(adapter, effective, role, bool(self.selection.get('mods', {}).get('apps')),
                                   bool(engine.normalize_installation_request(self.selection).get('installation_policy')),
                                   bool(self.selection.get('mods', {}).get('v14_bundle')))
            cache = self.caches[role]
            if cache is None:
                cache = self.session / role
                state = engine.extract(folder, cache, parts, hashes=False)
                if any(p['status'] != 'extracted' for p in state['partitions']):
                    raise JobError('filesystem', 'Có phân vùng chưa hỗ trợ chỉnh sửa đầy đủ metadata. Chọn ROM phù hợp hoặc chờ backend filesystem được hoàn thiện.')
                seal_cache(cache, source_stamp(effective))
                if not cache_ok(cache, effective, parts, source_stamp(effective)):
                    state['status'] = 'invalid'
                    write_json(cache / 'extraction.json', state)
                    raise JobError('filesystem', 'Filesystem giải nén thiếu đường dẫn Android trong metadata. Job bị chặn; không dùng cache này để build.')
            prepared[role] = {'folder': str(folder), 'identity': identity, 'extracted': str(cache)}
            self.registry[source_stamp(summary)] = {'original_stamp': source_stamp(summary),
                'prepared_stamp': source_stamp(effective), 'folder': str(folder), 'identity': identity, 'extracted': str(cache)}
            write_json(self.registry_path, self.registry)
        write_json(self.registry_path, self.registry)
        if rules is None:
            from .discovery import discover
            request = {'base': prepared['base']['folder'], 'donor': prepared['donor']['folder'],
                       'output': str(self.session / 'unused-output'), 'work_root': str(self.session),
                       'prepared_inputs': prepared}
            if self.selection.get('capacity_source'):
                from .ota import payload_info
                info = payload_info(Path(self.selection['capacity_source']))
                if not info['groups']:
                    raise JobError('capacity', 'ROM A bổ sung không có metadata nhóm phân vùng cần thiết.')
                # Require bounded OTA identity metadata; never compare whole ZIP against folder.
                with zipfile.ZipFile(self.selection['capacity_source']) as z:
                    meta = z.getinfo('META-INF/com/android/metadata')
                    if meta.file_size > 1024**2:
                        raise JobError('capacity', 'Metadata ROM A bổ sung quá lớn.')
                    text = z.read(meta).decode('utf-8')
                props = Properties((Path(prepared['base']['extracted']) / 'fs/my_manifest/build.prop').read_bytes())
                devices = dict(line.split('=', 1) for line in text.splitlines() if '=' in line).get('pre-device', '').split('|')
                if props.one('ro.product.device') not in devices:
                    raise JobError('capacity', 'ROM A bổ sung không khớp thiết bị A đã chọn.')
                request['dynamic_groups'] = info['groups']
            self.progress('Đang suy ra quy tắc port từ hai ROM…')
            rules = discover(request, self.progress, self.cancelled)
        adapter = read_json(rules)
        sdk = donor_sdk(Path(prepared['donor']['extracted']))
        tag = '_'.join(re.sub(r'[^A-Za-z0-9_-]', '_', prepared[r]['identity']['product_name']) for r in ('base', 'donor'))
        output = Path(self.selection['output_parent']).resolve() / (tag + '-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:4])
        self.current_output = output
        self.monitored.extend([output, Path(str(output) + '.build.json')])
        self.registry.setdefault('_outputs', []).extend([str(output), str(output) + '.build.json'])
        write_json(self.registry_path, self.registry)
        request = {'schema': 1, 'base': prepared['base']['folder'], 'donor': prepared['donor']['folder'],
                   'base_cache': prepared['base']['extracted'], 'donor_cache': prepared['donor']['extracted'],
                   'adapter': str(rules), 'target_sdk': sdk, 'output': str(output), 'work_root': str(self.session),
                   'mods': self.selection.get('mods', {}), 'apks': self.selection.get('apks', []),
                   'replacements': self.selection.get('replacements', []), 'cleanup_verification': True,
                   'workspace_observer': lambda work: setattr(self, 'build_work', work)}
        for key in ('boot_animation', 'reboot_animation'):
            if self.selection.get(key):
                request[key] = self.selection[key]
        if self.selection.get('installation_policy'):
            request['installation_policy'] = self.selection['installation_policy']
        request = engine.normalize_installation_request(request)
        enabled_parts = {}
        if request['mods'].get('apps'):
            enabled_parts.setdefault(adapter['app_partition'], []).append('apps')
        for key in ('boot_animation', 'reboot_animation'):
            if request['mods'].get(key):
                enabled_parts.setdefault(adapter[key + '_target'].split('/')[0], []).append(key)
        conflict = set(engine.replacement_map(request['replacements'])) & enabled_parts.keys()
        if conflict:
            raise JobError('conflict', 'IMG đã chọn sẽ thay toàn bộ phân vùng đang có tùy chỉnh. Chọn giữ IMG hoặc giữ các tùy chỉnh.',
                           partitions=sorted(conflict), options=sorted({k for p in conflict for k in enabled_parts[p]}))
        self.guard(force=True)
        return request

    def validate_early(self, summaries, adapter):
        """Checks independent of extracted ROM metadata run before allocation."""
        mods = self.selection.get('mods', {})
        if mods.get('v14_bundle'):
            from .v14 import inspect as inspect_v14
            inspect_v14(*summaries)
        replacements = engine.replacement_map(self.selection.get('replacements', []))
        install = engine.normalize_installation_request(self.selection).get('installation_policy')
        if install:
            from .test_avb import INSTALLATION_POLICY
            if install != INSTALLATION_POLICY:
                raise ValueError('Unknown installation policy')
            changed = set(summaries[0]['images']) & (engine.OS_PARTITIONS | {'vendor', 'odm'})
            if set(replacements) & (changed | {'vbmeta', 'vbmeta_system', 'vbmeta_vendor'}):
                raise ValueError('Fresh AVB build cannot discard core repacks through IMG C')
        if mods.get('unlocked_avb'):
            from .unlocked_avb import generator_tool
            generator_tool()
            if set(replacements) & {'vbmeta', 'vbmeta_system', 'vbmeta_vendor'}:
                raise ValueError('IMG C vbmeta conflicts with selected unlocked AVB policy')
        a = summaries[0]
        if a['images'] and set(replacements) - (set(a['images']) - engine.PRIVATE_PARTITIONS):
            raise ValueError('C can replace only non-private partitions present in A')
        if a['kind'] == 'fff-folder':
            engine.check_replacement_types(engine.image_map(Path(a['path'])), replacements)
        apks = self.selection.get('apks', [])
        if bool(apks) != bool(mods.get('apps')):
            raise ValueError('APK selection and option must agree')
        engine.inspect_apks(apks, target_sdk=None)
        targets = {}
        if adapter and mods.get('apps'):
            targets.setdefault(adapter['app_partition'], []).append('apps')
        for key in ('boot_animation', 'reboot_animation'):
            if mods.get(key):
                engine.validate_animation(Path(self.selection.get(key, '')))
                if adapter:
                    targets.setdefault(adapter[key + '_target'].split('/')[0], []).append(key)
        if mods.get('debloat'):
            for relative in engine.read_debloat(Path(self.selection.get('debloat_list', ''))):
                targets.setdefault(relative.split('/')[0], []).append('debloat')
        conflict = set(replacements) & targets.keys()
        if conflict:
            raise JobError('conflict', 'IMG đã chọn sẽ thay phân vùng có tùy chỉnh. Chọn giữ IMG hoặc tùy chỉnh.',
                           partitions=sorted(conflict), options=sorted({k for p in conflict for k in targets[p]}))
        protected = [Path(s['path']).resolve() for s in summaries] + list(replacements.values()) + [Path(p).resolve() for p in apks]
        protected += [Path(self.selection[k]).resolve(strict=True) for k in ('boot_animation', 'reboot_animation', 'debloat_list', 'capacity_source') if self.selection.get(k)]
        parent = Path(self.selection['output_parent']).resolve()
        for root in protected:
            if parent == root or parent.is_relative_to(root):
                raise ValueError('Output overlaps protected input')
            if self.work_base == root or self.work_base.is_relative_to(root) or self.fallback == root or self.fallback.is_relative_to(root):
                raise ValueError('Workspace root overlaps protected input')

    def run(self):
        from .workspace_prepare import canonical_owned_path
        canonical_owned_path(self.work_base)
        with writer_lock(self.work_base / 'product-job.lock'):
            self.registry = read_json(self.registry_path) if self.registry_path.is_file() else {}
            return self._run_locked()

    def _run_locked(self):
        token = JOB_GUARD.set(self.guard)
        write_token = WRITE_GUARD.set(self.reserve_write)
        try:
            request = self.prepare()
            self.progress('Đang tạo ROM…')
            result = engine.build(request, self.progress, self.cancelled)
            self.clean_transient()
            self.guard(force=True)
            result['storage'] = dict(self.storage, peak_w_observed=self.peak_w)
            write_json(self.session / 'job.json', {'schema': 1, 'owner': 'ColorNT', 'project': str(self.project),
                                                  'status': result['status'], 'output': result['output'], 'storage': result['storage']})
            write_json(Path(result['output'] + '.build.json'), result)
            return result
        except Exception as exc:
            cleanup_error = ''
            try:
                self.clean_transient()
                self.clean_partial_output()
            except (OSError, ValueError, KeyError) as cleanup_exc:
                cleanup_error = str(cleanup_exc)
            if self.session:
                write_json(self.session / 'job.json', {'schema': 1, 'owner': 'ColorNT', 'project': str(self.project),
                    'status': 'cancelled' if self.cancelled() else 'failed', 'error': str(exc),
                    'cleanup_error': cleanup_error, 'storage': self.storage})
            if isinstance(exc, JobError):
                raise
            text = str(exc)
            if isinstance(exc, PermissionError) and ('Android' in text or 'case-sensitive' in text or hasattr(exc, 'workspace_diagnostic')):
                detail = text + '\n' + json.dumps(getattr(exc, 'workspace_diagnostic', {}), ensure_ascii=False, indent=2)
                raise JobError('workspace_permission', 'Windows chưa cấp khả năng phân biệt chữ hoa/thường cho workspace. Bước chuẩn bị sẽ kiểm tra quyền owner; xem Chi tiết.', detail) from exc
            if 'dynamic group capacity is missing' in text:
                raise JobError('capacity', 'Folder A thiếu thông tin bố cục phân vùng. Chọn ZIP ROM A đầy đủ để đọc metadata bổ sung; không cần nhập số.', text) from exc
            if 'rules are incomplete' in text or 'property contract' in text or 'currently supports' in text:
                raise JobError('rules', 'Cặp ROM này còn thiếu quy tắc port trong core. Báo cáo phân tích đã được lưu; chưa tạo ROM hoàn tất.', text) from exc
            if 'APK package already present' in text or 'Duplicate selected package' in text:
                raise JobError('apps', 'Có APK trùng ứng dụng đã có trong ROM hoặc trong danh sách. Bỏ APK trùng rồi tạo lại. ' + text.split(':', 1)[-1], text) from exc
            if 'requires SDK' in text:
                raise JobError('apps', 'APK cần phiên bản Android cao hơn ROM B. Chọn phiên bản APK phù hợp; xem Chi tiết để biết tên ứng dụng.', text) from exc
            if 'Missing tool' in text:
                raise JobError('tools', 'Thiếu công cụ build. Cài đặt hoặc cập nhật lại ColorNT rồi thử lại.', text) from exc
            if 'firmware_info' in text or 'ZIP must contain' in text or 'No IMG files' in text or isinstance(exc, zipfile.BadZipFile):
                raise JobError('input', 'Input chưa đúng dạng ROM. Chọn ZIP ROM đầy đủ hoặc folder đã khôi phục bằng FFF có firmware_info và IMG.', text) from exc
            if 'IMG C' in text or 'replacement' in text.lower():
                raise JobError('images', 'IMG thay thế chưa phù hợp với phân vùng A. Kiểm tra file và chọn lại phân vùng đích.', text) from exc
            raise JobError('build', 'Chưa tạo được ROM. ' + text, text) from exc
        finally:
            WRITE_GUARD.reset(write_token)
            JOB_GUARD.reset(token)


def donor_sdk(cache):
    try:
        value = Properties((Path(cache) / 'fs/system/system/build.prop').read_bytes()).one('ro.build.version.sdk')
        if not value.isdecimal() or not 1 <= int(value) <= 1000:
            raise ValueError('SDK must be a positive integer')
        return int(value)
    except (OSError, ValueError) as exc:
        raise JobError('input', 'ROM B thiếu hoặc có SDK Android không hợp lệ; không thể kiểm tra APK.', str(exc)) from exc
