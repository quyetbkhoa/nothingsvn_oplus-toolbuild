"""Product UI: two ROMs, optional apps/images/mods, one create action."""
from pathlib import Path
import os
import queue
import threading
import uuid
import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .desktop_build import PRIVATE_PARTITIONS, inspect_apks, source_summary, validate_animation
from .images import inspect_image
from .jobs import BuildJob, JobError, load_choices, save_choices
from .test_avb import INSTALLATION_POLICY
from .tools import ROOT

MOD_LABELS = {'boot_animation': 'Boot animation', 'reboot_animation': 'Reboot animation'}
PART_LABELS = {'boot': 'Boot / kernel', 'init_boot': 'Init boot', 'vendor_boot': 'Vendor boot',
               'system': 'Hệ điều hành', 'system_ext': 'Thành phần hệ điều hành',
               'my_product': 'Ứng dụng và cấu hình máy', 'vendor': 'Phần cứng / vendor',
               'odm': 'Cấu hình phần cứng', 'vbmeta': 'VBMeta'}


def package_result_message(validation):
    if not validation.get('assembled'):
        return 'Folder output chưa đủ bố cục hoặc metadata; package bị chặn. Xem Chi tiết.'
    if not validation.get('filesystem_verified'):
        return 'Đã ghép folder IMG; kiểm chứng filesystem và package chưa đạt. Xem Chi tiết.'
    return 'Filesystem đã kiểm chứng; package chưa sẵn sàng do AVB. Folder giữ lại để xem báo cáo.'


class App:
    def __init__(self, root):
        self.root = root
        root.title('ColorNT ' + __version__ + ' — Tạo ROM port')
        root.geometry('1000x760')
        root.minsize(850, 620)
        self.events, self.cancel = queue.Queue(), threading.Event()
        self.busy = False
        self.workspace_preparation = None
        self.preview_generation = 0
        self.preview_timer = None
        self.summaries = {}
        self.apks, self.app_infos, self.replacements = [], {}, []
        self.output = None
        self.capacity_source = ''
        self.controls = []
        self.paths = {key: tk.StringVar(value=value) for key, value in load_choices().items()}
        self.mods = {key: tk.BooleanVar(value=False) for key in ('apps', 'v14_bundle', *MOD_LABELS)}
        self.install_unlocked = tk.BooleanVar(value=True)
        self.animations = {key: '' for key in MOD_LABELS}
        self.info = {key: tk.StringVar(value='Chưa chọn ROM') for key in ('base', 'donor')}
        self.status = tk.StringVar(value='Chọn ROM A và B để bắt đầu. Các tùy chỉnh mặc định tắt.')
        self.summary_text = tk.StringVar()
        self.logs = []
        frame = ttk.Frame(root, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='ColorNT', font=('Segoe UI', 22, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='Lấy hệ điều hành từ ROM B cho phần cứng máy A', font=('Segoe UI', 11)).pack(anchor='w', pady=(0, 10))
        content = ttk.Frame(frame)
        content.pack(fill='both', expand=True)
        canvas = tk.Canvas(content, highlightthickness=0)
        scroll = ttk.Scrollbar(content, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        body = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=body, anchor='nw')
        body.bind('<Configure>', lambda event: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))
        roms = ttk.Frame(body)
        roms.pack(fill='x')
        for i, (role, label) in enumerate((('base', 'ROM A — máy nhận'), ('donor', 'ROM B — lấy hệ điều hành'))):
            group = ttk.LabelFrame(roms, text=label, padding=10)
            group.grid(row=0, column=i, sticky='nsew', padx=(0, 6) if i == 0 else (6, 0))
            roms.columnconfigure(i, weight=1)
            entry = ttk.Entry(group, textvariable=self.paths[role], state='readonly')
            entry.pack(fill='x', pady=(0, 7))
            self.controls.append(entry)
            buttons = ttk.Frame(group)
            buttons.pack(fill='x')
            self.button(buttons, 'Chọn ZIP…', lambda r=role: self.pick_rom(r, False)).pack(side='left', padx=(0, 6))
            self.button(buttons, 'Chọn folder…', lambda r=role: self.pick_rom(r, True)).pack(side='left')
            ttk.Label(group, textvariable=self.info[role], wraplength=390).pack(anchor='w', pady=(8, 0))
        mods = ttk.LabelFrame(body, text='Tùy chỉnh', padding=10)
        mods.pack(fill='x', pady=10)
        self.animation_labels = {}
        check = ttk.Checkbutton(mods, text='Bộ mod v1.4 đầy đủ: NTManager / NexAlloy / Toolbox / framework / UI (đúng cặp A/B mẫu)',
                                variable=self.mods['v14_bundle'], command=self.refresh_summary)
        check.pack(anchor='w', pady=(0, 7)); self.controls.append(check)
        ttk.Label(mods, text='Backend tái tạo payload OOS đã ghim version; mỗi bản mới cần user test. Cặp khác cần transformer tương thích.',
                  wraplength=850).pack(anchor='w')
        setup_row = ttk.Frame(mods)
        setup_row.pack(fill='x', pady=5)
        self.button(setup_row, 'Nhập dữ liệu mod v1.4…', self.import_mod_pack).pack(side='left')
        self.button(setup_row, 'Chuẩn bị SELinux (WSL2)…', self.setup_policy).pack(side='left', padx=8)
        for key, label in MOD_LABELS.items():
            row = ttk.Frame(mods)
            row.pack(fill='x', pady=3)
            check = ttk.Checkbutton(row, text=label, variable=self.mods[key], command=lambda k=key: self.toggle_animation(k))
            check.pack(side='left')
            self.controls.append(check)
            self.button(row, 'Chọn file…', lambda k=key: self.choose_animation(k)).pack(side='right')
            self.animation_labels[key] = tk.StringVar(value='Giữ như ROM nguồn')
            ttk.Label(row, textvariable=self.animation_labels[key]).pack(side='right', padx=8)
        ttk.Label(mods, text='Bộ v1.4 áp dụng cùng nhau theo recipe đã kiểm chứng; tùy chọn từng mod riêng cần engine tương thích.').pack(anchor='w', pady=(7, 0))
        install_group = ttk.LabelFrame(body, text='Điều kiện cài đặt', padding=10)
        install_group.pack(fill='x', pady=(0, 10))
        check = ttk.Checkbutton(install_group, text='Máy đích đã unlock bootloader — dựng metadata cài thử mới',
                                variable=self.install_unlocked, command=self.refresh_summary)
        check.pack(anchor='w', pady=(7, 0))
        self.controls.append(check)
        ttk.Label(install_group, text='Mặc định theo phương pháp bản port đã boot: unsigned, tắt verification. Các mod vẫn OFF.',
                  wraplength=850).pack(anchor='w')
        app_group = ttk.LabelFrame(body, text='Ứng dụng thêm vào ROM', padding=10)
        app_group.pack(fill='x', pady=(0, 10))
        app_bar = ttk.Frame(app_group)
        app_bar.pack(fill='x')
        self.button(app_bar, 'Thêm APK…', self.pick_apks).pack(side='left')
        check = ttk.Checkbutton(app_bar, text='Thêm các APK đã chọn', variable=self.mods['apps'], command=self.refresh_lists)
        check.pack(side='left', padx=12)
        self.controls.append(check)
        self.button(app_bar, 'Bỏ đã chọn', self.remove_apks).pack(side='right')
        self.app_box = ttk.Treeview(app_group, columns=('app', 'version', 'state'), show='headings', height=3)
        for key, label, width in (('app', 'Ứng dụng', 450), ('version', 'Phiên bản', 160), ('state', 'Trạng thái', 160)):
            self.app_box.heading(key, text=label)
            self.app_box.column(key, width=width)
        self.app_box.pack(fill='x', pady=(7, 0))
        img_group = ttk.LabelFrame(body, text='IMG thay thế trong ROM output (tùy chọn)', padding=10)
        img_group.pack(fill='x', pady=(0, 10))
        img_bar = ttk.Frame(img_group)
        img_bar.pack(fill='x')
        self.button(img_bar, 'Thêm IMG…', self.pick_images).pack(side='left')
        self.button(img_bar, 'Đổi phân vùng đích…', self.edit_target).pack(side='left', padx=7)
        self.button(img_bar, 'Bỏ đã chọn', self.remove_images).pack(side='right')
        self.img_box = ttk.Treeview(img_group, columns=('file', 'target'), show='headings', height=3)
        self.img_box.heading('file', text='File IMG từ PC')
        self.img_box.heading('target', text='Phân vùng output sẽ thay')
        self.img_box.pack(fill='x', pady=(7, 0))
        self.img_box.bind('<Double-1>', lambda event: self.edit_target())
        output_bar = ttk.LabelFrame(body, text='Nơi lưu kết quả', padding=10)
        output_bar.pack(fill='x')
        entry = ttk.Entry(output_bar, textvariable=self.paths['output_parent'], state='readonly')
        entry.pack(side='left', fill='x', expand=True, padx=(0, 8))
        self.controls.append(entry)
        self.button(output_bar, 'Chọn nơi lưu output…', self.pick_output).pack(side='right')
        ttk.Label(body, text='App tạo folder candidate mới trong nơi đã chọn; không ghi đè ROM đã có.').pack(anchor='w')
        self.capacity_button = self.button(body, 'Bổ sung dữ liệu ROM A…', self.pick_capacity)
        self.workspace_button = self.button(body, 'Chuẩn bị thư mục làm việc…', self.prepare_workspace)
        ttk.Separator(frame).pack(fill='x', pady=10)
        ttk.Label(frame, textvariable=self.summary_text, wraplength=900).pack(anchor='w')
        actions = ttk.Frame(frame)
        actions.pack(fill='x', pady=7)
        self.start_button = self.button(actions, 'Tạo ROM', self.start)
        self.start_button.pack(side='right')
        self.stop_button = ttk.Button(actions, text='Dừng', command=self.cancel.set, state='disabled')
        self.stop_button.pack(side='right', padx=7)
        self.open_button = ttk.Button(actions, text='Mở folder output', command=self.open_output, state='disabled')
        self.open_button.pack(side='left')
        ttk.Button(actions, text='Chi tiết', command=self.show_details).pack(side='left', padx=7)
        ttk.Label(frame, textvariable=self.status, wraplength=930).pack(anchor='w')
        self.progress = ttk.Progressbar(frame, mode='indeterminate')
        self.progress.pack(fill='x', pady=(5, 0))
        for key in ('base', 'donor'):
            self.paths[key].trace_add('write', lambda *args: self.schedule_preview())
        self.refresh_lists()
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(100, self.poll)
        root.after(150, self.preview)

    def button(self, parent, label, command):
        button = ttk.Button(parent, text=label, command=command)
        self.controls.append(button)
        return button

    def pick_rom(self, role, folder):
        if self.busy:
            return
        path = filedialog.askdirectory(title='Chọn folder ROM') if folder else filedialog.askopenfilename(title='Chọn ZIP ROM', filetypes=[('ROM ZIP', '*.zip')])
        if path:
            if role == 'base':
                self.capacity_source = ''
                self.capacity_button.pack_forget()
            self.paths[role].set(path)

    def schedule_preview(self):
        if self.preview_timer:
            self.root.after_cancel(self.preview_timer)
        self.preview_timer = self.root.after(350, self.preview)

    def preview(self):
        self.preview_timer = None
        self.preview_generation += 1
        generation = self.preview_generation
        paths = {key: self.paths[key].get() for key in ('base', 'donor')}
        def worker():
            result = {}
            for role, path in paths.items():
                if path:
                    try:
                        result[role] = source_summary(path)
                    except Exception as exc:
                        result[role] = {'error': str(exc)}
            self.events.put({'type': 'preview', 'generation': generation, 'paths': paths, 'result': result})
        threading.Thread(target=worker, daemon=True).start()

    def pick_output(self):
        if not self.busy:
            path = filedialog.askdirectory(title='Chọn nơi lưu; app tạo folder ROM mới bên trong')
            if path:
                self.paths['output_parent'].set(path)

    def pick_capacity(self):
        if not self.busy:
            path = filedialog.askopenfilename(title='Chọn ZIP ROM A đầy đủ để đọc metadata', filetypes=[('ROM ZIP', '*.zip')])
            if path:
                self.capacity_source = path
                self.status.set('Đã chọn dữ liệu bổ sung cho ROM A. Bấm Tạo ROM để thử lại.')

    def prepare_workspace(self):
        if self.busy or os.name != 'nt':
            return
        from .workspace_prepare import launch_preparation
        nonce = uuid.uuid4().hex
        result = ROOT / 'config' / ('workspace-result-' + nonce + '.json')
        try:
            save_choices(self.selection())
            handle = launch_preparation(ROOT / 'scripts/prepare_windows_workspace.py', result, nonce, ROOT)
        except OSError as exc:
            self.status.set('Windows chưa cấp quyền chạy bước chuẩn bị workspace.')
            self.logs.append(str(exc))
            return
        self.workspace_preparation = {'handle': handle, 'result': result, 'nonce': nonce}
        self.busy = True
        for widget in self.controls:
            widget.state(['disabled'])
        self.progress.start(12)
        self.status.set('Đang chờ kết quả chuẩn bị workspace từ Windows…')

    def poll_workspace_preparation(self):
        pending = self.workspace_preparation
        if not pending:
            return
        from .io import read_json
        from .workspace_prepare import preparation_exited
        try:
            if pending['result'].is_file():
                result = read_json(pending['result'])
                if result.get('nonce') != pending['nonce']:
                    raise ValueError('Workspace helper result does not match this request')
            elif preparation_exited(pending['handle']):
                result = {'status': 'failed', 'error': 'Helper exited without a result; workspace capability is unknown.'}
            else:
                return
            preparation_exited(pending['handle'], close=True)
            self.workspace_preparation = None
            self.set_idle()
            self.logs.append(json.dumps(result, ensure_ascii=False, indent=2))
            if result['status'] == 'success':
                self.workspace_button.pack_forget()
                self.status.set('Workspace đã kiểm tra tên hoa/thường ở root và thư mục con. Có thể tạo ROM.')
            else:
                failures = [r for r in result.get('roots', []) if r['status'] != 'success']
                reason = '; '.join(f"{r.get('stage', 'unknown')}: error {r.get('error_code', '?')}" for r in failures)
                self.status.set('Workspace chưa sẵn sàng. ' + (reason or result.get('error', 'Xem Chi tiết.')))
        except (OSError, ValueError) as exc:
            preparation_exited(pending['handle'], close=True)
            self.workspace_preparation = None
            self.set_idle()
            self.status.set('Không đọc được kết quả chuẩn bị workspace. Xem Chi tiết.')
            self.logs.append(str(exc))

    def choose_animation(self, key):
        if self.busy:
            return False
        path = filedialog.askopenfilename(title='Chọn ' + MOD_LABELS[key], filetypes=[('Animation ZIP', '*.zip')])
        if not path:
            return False
        try:
            validate_animation(Path(path))
        except Exception as exc:
            messagebox.showerror('Animation chưa hợp lệ', str(exc))
            return False
        self.animations[key] = path
        self.mods[key].set(True)
        self.animation_labels[key].set(Path(path).name)
        self.refresh_summary()
        return True

    def toggle_animation(self, key):
        if self.mods[key].get() and not self.animations[key] and not self.choose_animation(key):
            self.mods[key].set(False)
        self.animation_labels[key].set(Path(self.animations[key]).name if self.mods[key].get() else 'Giữ như ROM nguồn')
        self.refresh_summary()

    def pick_apks(self):
        if self.busy:
            return
        paths = filedialog.askopenfilenames(title='Chọn nhiều ứng dụng APK', filetypes=[('Android APK', '*.apk')])
        proposed = self.apks + [p for p in paths if p not in self.apks]
        try:
            infos = inspect_apks(proposed, 100_000)
        except Exception as exc:
            messagebox.showerror('Chưa thêm được APK', str(exc))
            return
        self.apks = proposed
        self.app_infos = {p: info for p, info in zip(proposed, infos)}
        if paths:
            self.mods['apps'].set(True)
        self.refresh_lists()

    def targets(self):
        return sorted(set(self.summaries.get('base', {}).get('images', {})) - PRIVATE_PARTITIONS)

    def select_target(self, initial=''):
        names = self.targets()
        if not names:
            messagebox.showinfo('Chọn ROM A trước', 'Chọn ROM A và chờ đọc thông tin để biết các phân vùng có thể thay.')
            return None
        dialog = tk.Toplevel(self.root)
        dialog.title('Chọn phân vùng output')
        dialog.transient(self.root)
        dialog.grab_set()
        ttk.Label(dialog, text='IMG sẽ thay toàn bộ phân vùng được chọn:', padding=12).pack()
        choices = [name + (' — ' + PART_LABELS[name] if name in PART_LABELS else '') for name in names]
        value = tk.StringVar(value=choices[names.index(initial)] if initial in names else '')
        ttk.Combobox(dialog, textvariable=value, values=choices, state='readonly', width=48).pack(padx=12, pady=7)
        result = []
        def done():
            if value.get():
                result.append(names[choices.index(value.get())])
                dialog.destroy()
        ttk.Button(dialog, text='Chọn', command=done).pack(pady=12)
        self.root.wait_window(dialog)
        return result[0] if result else None

    def pick_images(self):
        if self.busy:
            return
        if not self.targets():
            self.select_target()
            return
        for path in filedialog.askopenfilenames(title='Chọn IMG thay thế', filetypes=[('Partition IMG', '*.img')]):
            try:
                if inspect_image(Path(path))['format'] == 'unknown':
                    raise ValueError('Không nhận diện được định dạng IMG: ' + Path(path).name)
                target = Path(path).stem if Path(path).stem in self.targets() else self.select_target()
                if target:
                    if any(p['partition'] == target for p in self.replacements):
                        raise ValueError('Đã có IMG thay phân vùng ' + target + '. Bỏ hoặc đổi IMG đó trước.')
                    self.replacements.append({'path': path, 'partition': target})
            except Exception as exc:
                messagebox.showerror('Chưa thêm được IMG', str(exc))
        self.refresh_lists()

    def edit_target(self):
        if self.busy or not self.img_box.selection():
            return
        index = int(self.img_box.selection()[0])
        target = self.select_target(self.replacements[index]['partition'])
        if target and not any(p['partition'] == target for i, p in enumerate(self.replacements) if i != index):
            self.replacements[index]['partition'] = target
        elif target:
            messagebox.showerror('Trùng phân vùng', 'Một IMG khác đã chọn phân vùng này.')
        self.refresh_lists()

    def remove_apks(self):
        if not self.busy:
            for index in sorted((int(i) for i in self.app_box.selection()), reverse=True):
                self.apks.pop(index)
            if not self.apks:
                self.mods['apps'].set(False)
            self.refresh_lists()

    def remove_images(self):
        if not self.busy:
            for index in sorted((int(i) for i in self.img_box.selection()), reverse=True):
                self.replacements.pop(index)
            self.refresh_lists()

    def refresh_lists(self):
        self.app_box.delete(*self.app_box.get_children())
        self.img_box.delete(*self.img_box.get_children())
        for i, path in enumerate(self.apks):
            info = self.app_infos.get(path, {})
            self.app_box.insert('', 'end', iid=str(i), values=(Path(path).stem + ' (' + info.get('package', '') + ')', info.get('version_name', ''), 'Sẽ thêm' if self.mods['apps'].get() else 'Không áp dụng'))
        for i, selected in enumerate(self.replacements):
            target = selected['partition']
            self.img_box.insert('', 'end', iid=str(i), values=(Path(selected['path']).name, target + (' — ' + PART_LABELS[target] if target in PART_LABELS else '')))
        self.refresh_summary()

    def refresh_summary(self):
        identities = [self.summaries.get(k, {}).get('identity') or {} for k in ('base', 'donor')]
        a, b = [item.get('product_name', label) for item, label in zip(identities, ('máy A', 'OS B'))]
        mods = [label for key, label in MOD_LABELS.items() if self.mods[key].get()]
        if self.mods['v14_bundle'].get():
            mods.append('Full bundle v1.4')
        installation = 'Máy unlocked, metadata mới' if self.install_unlocked.get() else 'Giữ metadata nguồn'
        self.summary_text.set(f'{a} ← {b} • {len(self.apks) if self.mods["apps"].get() else 0} APK • {len(self.replacements)} IMG thay thế • ' +
                              (', '.join(mods) or 'Mods OFF') + ' • ' + installation)

    def selection(self):
        result = {key: value.get().strip() for key, value in self.paths.items()}
        result.update(mods={key: value.get() for key, value in self.mods.items()},
                      apks=list(self.apks) if self.mods['apps'].get() else [],
                      replacements=[dict(p) for p in self.replacements], capacity_source=self.capacity_source,
                      installation_policy=INSTALLATION_POLICY if self.install_unlocked.get() else None)
        result.update({key: value for key, value in self.animations.items() if self.mods[key].get()})
        return result

    def setup_task(self, function):
        if self.busy:
            return
        self.busy = True
        for control in self.controls:
            control.state(['disabled'])
        self.start_button.state(['disabled'])
        self.progress.start(10)
        self.status.set('Đang chuẩn bị công cụ/dữ liệu…')
        def worker():
            try:
                result = function(lambda msg: self.events.put({'type': 'progress', 'message': msg}))
                self.events.put({'type': 'setup_complete', 'result': result})
            except Exception as exc:
                self.events.put({'type': 'error', 'code': 'setup', 'message': str(exc), 'detail': '', 'actions': {}})
        threading.Thread(target=worker, daemon=True).start()

    def import_mod_pack(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(title='Bộ dữ liệu mod v1.4', filetypes=[('ColorNT mod data', '*.zip')])
        if path:
            from .portable import import_v14_pack
            self.setup_task(lambda progress: import_v14_pack(path, progress))

    def setup_policy(self):
        from .policy_runtime import setup
        self.setup_task(setup)

    def start(self):
        if self.busy:
            return
        selection = self.selection()
        if not selection['base'] or not selection['donor']:
            messagebox.showinfo('Chọn hai ROM', 'Hãy chọn ROM A và ROM B trước.')
            return
        try:
            save_choices(selection)
        except OSError as exc:
            messagebox.showerror('Không lưu được lựa chọn', str(exc))
            return
        self.busy = True
        self.cancel.clear()
        self.output = None
        for widget in self.controls:
            widget.state(['disabled'])
        self.open_button.state(['disabled'])
        self.stop_button.state(['!disabled'])
        self.progress.start(12)
        self.status.set('Đang chuẩn bị hai ROM…')
        def worker():
            try:
                from .product import create
                result = create(selection, self.events.put, self.cancel.is_set)
                self.events.put({'type': 'package_blocked' if result['status'] == 'package-blocked' else 'complete', 'result': result})
            except JobError as exc:
                self.events.put({'type': 'error', 'code': exc.code, 'message': str(exc), 'detail': exc.detail, 'actions': exc.actions})
            except Exception as exc:
                self.events.put({'type': 'error', 'code': 'build', 'message': 'Chưa tạo được ROM. Xem Chi tiết để kiểm tra.', 'detail': repr(exc), 'actions': {}})
        threading.Thread(target=worker, daemon=True).start()

    def set_idle(self):
        self.busy = False
        self.progress.stop()
        for widget in self.controls:
            widget.state(['!disabled'])
        self.stop_button.state(['disabled'])

    def poll(self):
        self.poll_workspace_preparation()
        try:
            while True:
                event = self.events.get_nowait()
                kind = event['type']
                if kind == 'preview':
                    if event['generation'] != self.preview_generation or any(self.paths[k].get() != v for k, v in event['paths'].items()):
                        continue
                    self.summaries = event['result']
                    for role in ('base', 'donor'):
                        result = self.summaries.get(role, {})
                        identity = result.get('identity') or {}
                        text = ' • '.join(str(identity[k]) for k in ('product_name', 'android_version', 'version_name') if identity.get(k))
                        self.info[role].set('Chưa đọc được ROM; chọn lại file/folder.' if 'error' in result else text or ('ZIP OTA; thông tin sẽ được đọc khi tạo ROM.' if result else 'Chưa chọn ROM'))
                    self.refresh_summary()
                elif kind == 'progress':
                    self.status.set(event['message'])
                    self.logs.append(event['message'])
                elif kind == 'setup_complete':
                    self.set_idle()
                    self.logs.append(json.dumps(event['result'], ensure_ascii=False, indent=2))
                    self.status.set('Đã kiểm chứng và chuẩn bị xong công cụ/dữ liệu đã chọn.')
                elif kind in ('complete', 'package_blocked'):
                    self.set_idle()
                    self.output = Path(event['result']['output'])
                    self.open_button.state(['!disabled'])
                    self.status.set(package_result_message(event['result']['package_validation']) if kind == 'package_blocked' else 'Package thử nghiệm đã qua gate; chưa kiểm chứng trên thiết bị.')
                    if kind == 'package_blocked':
                        self.logs.append(json.dumps(event['result']['package_validation'], ensure_ascii=False, indent=2))
                    self.logs.append('Output: ' + str(self.output))
                    if event['result'].get('delivery'):
                        self.logs.append('Candidate: ' + event['result']['delivery']['candidate_id'] +
                                         '\nManifest SHA256: ' + event['result']['delivery']['manifest_sha256'])
                elif kind == 'error':
                    self.set_idle()
                    self.status.set(event['message'])
                    self.logs.append(event['detail'] or event['message'])
                    if event['code'] == 'capacity':
                        self.capacity_button.pack(fill='x', pady=8)
                    elif event['code'] == 'workspace_permission':
                        self.workspace_button.pack(fill='x', pady=8)
                    elif event['code'] == 'conflict':
                        actions = event['actions']
                        answer = messagebox.askyesnocancel('Giữ IMG hay tùy chỉnh?', 'IMG thay phân vùng: ' + ', '.join(actions['partitions']) + '\n\nCó: giữ IMG, tắt tùy chỉnh xung đột.\nKhông: bỏ IMG xung đột, giữ tùy chỉnh.\nHủy: giữ lựa chọn để tự sửa.')
                        if answer is True:
                            for option in actions['options']:
                                self.mods[option].set(False)
                        elif answer is False:
                            self.replacements = [p for p in self.replacements if p['partition'] not in actions['partitions']]
                        self.refresh_lists()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def show_details(self):
        dialog = tk.Toplevel(self.root)
        dialog.title('Chi tiết job')
        dialog.geometry('850x420')
        text = tk.Text(dialog, wrap='word', font=('Consolas', 10))
        text.pack(fill='both', expand=True)
        text.insert('1.0', '\n'.join(self.logs[-500:]) or 'Chưa có job.')
        text.configure(state='disabled')

    def open_output(self):
        if self.output and self.output.is_dir() and os.name == 'nt':
            os.startfile(self.output)

    def close(self):
        if self.busy:
            self.cancel.set()
            self.status.set('Đang dừng job; chờ công cụ hiện tại kết thúc rồi đóng app.')
            return
        self.root.destroy()


def main():
    root = tk.Tk()
    ttk.Style().theme_use('vista' if os.name == 'nt' else 'clam')
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
