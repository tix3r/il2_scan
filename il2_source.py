"""il2_scan: Windows IL2 Stats monitor. Standard library only."""
import ctypes
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import pystray
    from PIL import Image
except ImportError:
    pystray = None
    Image = None

SERVERS = [
    ("Wings of Liberty - Крылья Свободы", "http://il2stat.aviaskins.com:8008/ru/online/"),
    ("ABC Duel Server 1x1 - SVAO", "http://46.229.212.86/ru/online/"),
]
APPDIR = Path(os.getenv('APPDATA') or str(Path.home())) / 'il2_scan'
CONFIG = APPDIR / 'settings.json'
LEGACY_CONFIGS = [
    Path(os.getenv('APPDATA') or str(Path.home())) / 'IL2 scan' / 'settings.json',
    Path(os.getenv('APPDATA') or str(Path.home())) / 'Il-2 connect' / 'settings.json',
]
INTERVAL_MS = 30000
MISSION_URL = 'http://il2stat.aviaskins.com:8008/ru/'

# Windows single-instance guard and local activation channel.
# A named mutex prevents two copies even if the first window is hidden in tray.
MUTEX_NAME = r'Local\Il2Connect_SingleInstance_2026'
ACTIVATION_PORT = 47839
ERROR_ALREADY_EXISTS = 183


def acquire_single_instance():
    if sys.platform != 'win32':
        return None, True
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        raise ctypes.WinError()
    return handle, kernel.GetLastError() != ERROR_ALREADY_EXISTS


def request_existing_window():
    # The newly launched process can grant the existing instance permission
    # to take foreground focus before asking it to restore the window.
    if sys.platform == 'win32':
        try:
            ctypes.windll.user32.AllowSetForegroundWindow(ctypes.c_uint(0xFFFFFFFF))
        except (OSError, AttributeError):
            pass
    # Allow the first process time to initialize its local listener.
    for _ in range(30):
        try:
            with socket.create_connection(('127.0.0.1', ACTIVATION_PORT), timeout=0.2) as sock:
                sock.sendall(b'SHOW\n')
                return True
        except OSError:
            time.sleep(0.1)
    return False


class ActivationServer:
    def __init__(self):
        self.events = queue.Queue()
        self.stop_event = threading.Event()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', ACTIVATION_PORT))
        self.listener.listen(4)
        self.listener.settimeout(0.5)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                conn, _ = self.listener.accept()
                with conn:
                    conn.settimeout(1)
                    if conn.recv(32).strip() == b'SHOW':
                        self.events.put('show')
            except (socket.timeout, OSError):
                continue

    def close(self):
        self.stop_event.set()
        self.listener.close()




def format_time_left(value):
    """IL2 Stats mission duration is hours:minutes; display as 2h 30m."""
    match = re.fullmatch(r'\s*(\d+)\s*:\s*([0-5]?\d)\s*', value)
    if match:
        return f'{int(match.group(1))}h {int(match.group(2)):02d}m'
    return value.strip() or '—'


def resource_path(name):
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    return base / 'assets' / name


class AudioPlayer:
    """Windows Media Control Interface: MP3 playback without console or third-party libs."""
    def __init__(self):
        self.enabled = sys.platform == 'win32'
        self.volume = 65
        self.lock = threading.Lock()
        self.alias = 'il2connectaudio'
        self.generation = 0
        self.winmm = ctypes.windll.winmm if self.enabled else None

    def _cmd(self, command):
        if not self.enabled:
            return False
        buf = ctypes.create_unicode_buffer(512)
        return self.winmm.mciSendStringW(command, buf, len(buf), None) == 0

    def set_volume(self, value):
        self.volume = int(float(value))
        with self.lock:
            self._cmd(f'setaudio {self.alias} volume to {self.volume * 10}')

    def play(self, filename):
        if not self.enabled or self.volume <= 0:
            return
        path = resource_path(filename)
        if not path.exists():
            return
        with self.lock:
            self._cmd(f'stop {self.alias}')
            self._cmd(f'close {self.alias}')
            if self._cmd(f'open "{path}" type mpegvideo alias {self.alias}'):
                self._cmd(f'setaudio {self.alias} volume to {self.volume * 10}')
                self._cmd(f'play {self.alias}')

    def close(self):
        with self.lock:
            self._cmd(f'close {self.alias}')


class MissionParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_mission = False
        self.mission_depth = 0
        self.depth = 0
        self.in_num = False
        self.num_depth = 0
        self.num_parts = []
        self.values = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = a.get('class', '').split()
        if tag == 'div' and a.get('id') == 'current_mission' and 'online_widget' in classes:
            self.in_mission = True
            self.mission_depth = self.depth
        if self.in_mission and tag == 'div' and 'total_num' in classes:
            self.in_num = True
            self.num_depth = self.depth
            self.num_parts = []
        if tag not in ('meta', 'link', 'img', 'input', 'br', 'hr', 'source', 'area'):
            self.depth += 1

    def handle_endtag(self, tag):
        if tag not in ('meta', 'link', 'img', 'input', 'br', 'hr', 'source', 'area'):
            self.depth = max(0, self.depth - 1)
        if tag == 'div' and self.in_num and self.depth == self.num_depth:
            self.values.append(''.join(self.num_parts).strip())
            self.in_num = False
        if tag == 'div' and self.in_mission and self.depth == self.mission_depth:
            self.in_mission = False

    def handle_data(self, data):
        if self.in_num:
            self.num_parts.append(data)


def fetch_mission():
    req = urllib.request.Request(MISSION_URL, headers={'User-Agent': 'il2_scan/1.0', 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(req, timeout=12) as response:
        raw = response.read(3_000_000)
        charset = response.headers.get_content_charset() or 'utf-8'
    parser = MissionParser()
    parser.feed(raw.decode(charset, errors='replace'))
    if len(parser.values) < 2:
        raise ValueError('Не удалось прочитать миссию или время')
    return {'mission': parser.values[0], 'time_left': format_time_left(parser.values[1])}


class OnlineParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.coal = None
        self.row = None
        self.in_cell = False
        self.cell_depth = 0
        self.rows = {1: [], 2: []}
        self.depth = 0
        self.coal_depth = None
        self.in_title = False
        self.title_parts = []
        self.all_text = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = a.get('class', '').split()
        if tag == 'title': self.in_title = True
        if tag == 'div' and ('online_coal_1' in classes or 'online_coal_2' in classes):
            self.coal = 1 if 'online_coal_1' in classes else 2
            self.coal_depth = self.depth
        if tag == 'a' and self.coal and 'row' in classes:
            self.row = {'href': a.get('href', ''), 'name': ''}
        if tag == 'div' and self.row is not None and 'cell' in classes:
            self.in_cell = True
            self.cell_depth = self.depth
        if tag not in ('meta', 'link', 'img', 'input', 'br', 'hr', 'source', 'area'):
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == 'title': self.in_title = False
        if tag not in ('meta', 'link', 'img', 'input', 'br', 'hr', 'source', 'area'):
            self.depth = max(0, self.depth - 1)
        if tag == 'div' and self.in_cell and self.depth == self.cell_depth:
            self.in_cell = False
        if tag == 'a' and self.row is not None:
            name = self.row['name'].strip()
            if name and self.coal:
                self.row['name'] = name
                self.rows[self.coal].append(self.row)
            self.row = None
            self.in_cell = False
        if tag == 'div' and self.coal_depth is not None and self.depth == self.coal_depth:
            self.coal = None
            self.coal_depth = None

    def handle_data(self, data):
        if self.in_title: self.title_parts.append(data)
        if self.in_cell and self.row is not None: self.row['name'] += data
        self.all_text.append(data)


def fetch_server(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'il2_scan/1.0 (IL2 Stats monitor)', 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(req, timeout=12) as response:
        raw = response.read(3_000_000)
        charset = response.headers.get_content_charset() or 'utf-8'
    parser = OnlineParser()
    parser.feed(raw.decode(charset, errors='replace'))
    text = ' '.join(parser.all_text)
    if not re.search(r'Пилотов\s+на\s+сервере\s*:', text, re.I) or 'online_coal_1' not in raw.decode(charset, errors='replace'):
        raise ValueError('Неожиданная структура страницы IL2 Stats')
    counts = [len(parser.rows[1]), len(parser.rows[2])]
    return {'players': parser.rows, 'count': sum(counts), 'allies': counts[0], 'axis': counts[1]}


class WTConnect(tk.Tk):
    def __init__(self, activation_server=None):
        super().__init__()
        self.activation_server = activation_server
        self.title('il2_scan v2.1')
        self.geometry('820x705')
        self.minsize(720, 590)
        self.configure(bg='#f0f0f0')
        self.apply_app_icon(self)
        APPDIR.mkdir(parents=True, exist_ok=True)
        self.settings = self.load_settings()
        self.favorites = set(self.settings.get('favorites', []))
        # Older releases stored modes by row number (duel=0, Wings=1).
        # Migrate once to new row order (Wings=0, duel=1).
        saved_modes = self.settings.get('only_favorites_by_server', {})
        # Defaults: Wings (index 0) ON, Duel (index 1) OFF.
        # Preserve any explicitly saved choices, including legacy row ordering.
        if self.settings.get('server_order_version') == 2:
            self.modes = {'0': bool(saved_modes.get('0', True)),
                          '1': bool(saved_modes.get('1', False))}
        else:
            self.modes = {'0': bool(saved_modes.get('1', True)),
                          '1': bool(saved_modes.get('0', False))}
        self.settings['server_order_version'] = 2
        self.only_favorites = tk.BooleanVar(value=self.modes['0'])
        self.startup_sound = tk.BooleanVar(value=bool(self.settings.get('startup_sound', True)))
        self.refresh_seconds = tk.IntVar(value=max(5, min(3600, int(self.settings.get('refresh_seconds', 30)))))
        self.refresh_job = None
        saved_audio = self.settings.get('server_audio_enabled', {})
        self.server_audio_enabled = {str(i): bool(saved_audio.get(str(i), True)) for i in range(len(SERVERS))}
        self.volume = tk.IntVar(value=int(self.settings.get('volume', 65)))
        self.audio = AudioPlayer()
        self.audio.set_volume(self.volume.get())
        self.server_index = tk.IntVar(value=0)
        self.data = {}
        self.previous = {}
        self.results = queue.Queue()
        self.working = False
        self.last_update = tk.StringVar(value='Ожидание обновления')
        self.status = tk.StringVar(value='Готово')
        self.trees = {}
        self.create_widgets()
        self.tray_icon = None
        self.tray_thread = None
        self._exit_requested = False
        self.bind('<Unmap>', self.on_window_minimize)
        self.after(100, self.check_activation)
        self.after(200, self.check_results)
        self.after(300, self.refresh)
        self.after(600, self.play_startup_sound)
        self.schedule_refresh()
        self.protocol('WM_DELETE_WINDOW', self.close)

    def check_activation(self):
        if self._exit_requested:
            return
        if self.activation_server is not None:
            try:
                while self.activation_server.events.get_nowait() == 'show':
                    self.restore_from_tray()
            except queue.Empty:
                pass
        self.after(150, self.check_activation)

    def on_window_minimize(self, _event=None):
        # Windows sends Unmap when the window is minimized. Hide it from the taskbar
        # and keep the process alive in the notification area.
        if self._exit_requested:
            return
        try:
            if self.state() == 'iconic':
                self.after_idle(self.hide_to_tray)
        except tk.TclError:
            pass

    def ensure_tray_icon(self):
        if self.tray_icon is not None or pystray is None or Image is None:
            return
        icon_path = resource_path('icon.ico')
        try:
            image = Image.open(icon_path).convert('RGBA')
            menu = pystray.Menu(
                pystray.MenuItem('Открыть il2_scan', self._tray_restore, default=True),
                pystray.MenuItem('Выход', self._tray_exit),
            )
            self.tray_icon = pystray.Icon('il2_scan', image, 'il2_scan', menu)
            self.tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
            self.tray_thread.start()
        except Exception:
            self.tray_icon = None

    def hide_to_tray(self):
        if self._exit_requested:
            return
        if pystray is None:
            # Build installs pystray; this fallback prevents accidental disappearance
            # if dependencies were not installed in a source run.
            self.iconify()
            return
        self.ensure_tray_icon()
        if self.tray_icon is not None:
            self.withdraw()

    def _tray_restore(self, _icon=None, _item=None):
        try:
            self.after(0, self.restore_from_tray)
        except tk.TclError:
            pass

    def restore_from_tray(self):
        if self._exit_requested:
            return
        self.deiconify()
        self.state('normal')
        self.lift()
        self.update_idletasks()
        if sys.platform == 'win32':
            try:
                user32 = ctypes.windll.user32
                user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                user32.GetAncestor.restype = ctypes.c_void_p
                hwnd = user32.GetAncestor(self.winfo_id(), 2)  # GA_ROOT
                if hwnd:
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
            except (OSError, AttributeError, ValueError):
                pass
        # Brief topmost pulse brings the window in front without keeping it
        # permanently above other applications.
        try:
            self.attributes('-topmost', True)
            self.lift()
            self.after(250, lambda: self.attributes('-topmost', False))
            self.focus_force()
        except tk.TclError:
            pass

    def _tray_exit(self, _icon=None, _item=None):
        try:
            self.after(0, self.close)
        except tk.TclError:
            pass

    def stop_tray_icon(self):
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
            self.tray_icon = None

    def apply_app_icon(self, window):
        icon_file = resource_path('icon.ico')
        if icon_file.exists():
            try:
                window.iconbitmap(default=str(icon_file))
            except tk.TclError:
                pass

    def load_settings(self):
        try:
            config_path = next((p for p in [CONFIG, *LEGACY_CONFIGS] if p.exists()), CONFIG)
            return json.loads(config_path.read_text(encoding='utf-8'))
        except (OSError, ValueError): return {}

    def save_settings(self):
        try:
            CONFIG.write_text(json.dumps({'favorites': sorted(self.favorites), 'only_favorites_by_server': self.modes, 'server_order_version': 2, 'volume': self.volume.get(), 'server_audio_enabled': self.server_audio_enabled, 'startup_sound': self.startup_sound.get(), 'refresh_seconds': self.refresh_seconds.get()}, ensure_ascii=False, indent=2), encoding='utf-8')
        except OSError: pass

    def create_widgets(self):
        style = ttk.Style(self)
        if 'clam' in style.theme_names(): style.theme_use('clam')
        style.configure('Treeview', rowheight=26, font=('Segoe UI', 10), background='white', fieldbackground='white')
        style.configure('Treeview.Heading', font=('Segoe UI', 10, 'bold'))
        style.map('Treeview', background=[('selected', '#8ab7ee')], foreground=[('selected', 'black')])
        main = ttk.Frame(self, padding=12)
        main.pack(fill='both', expand=True)
        bar = ttk.Frame(main)
        bar.pack(fill='x', pady=(0, 10))
        ttk.Button(bar, text='Favorites list', command=self.open_favorites).pack(side='left', padx=(0, 6))
        ttk.Button(bar, text='Find player', command=self.open_find).pack(side='left', padx=(0, 6))
        ttk.Button(bar, text='About', command=self.about).pack(side='left')

        sf = ttk.LabelFrame(main, text='Серверы')
        sf.pack(fill='x', pady=(0, 10))
        columns = ('select', 'server', 'sound', 'players', 'map', 'time', 'status')
        st = ttk.Treeview(sf, columns=columns, show='headings', height=2, selectmode='browse')
        self.server_tree = st
        for col, title, width, anchor in [('select','#',35,'center'),('server','Server',240,'w'),('sound','Sound',58,'center'),('players','Players',70,'center'),('map','Map / Mission',190,'w'),('time','Time left',80,'center'),('status','Status',70,'center')]:
            st.heading(col, text=title)
            st.column(col, width=width, anchor=anchor, stretch=(col in ('server','map')))
        st.pack(fill='x', padx=5, pady=5)
        for i, (name, _) in enumerate(SERVERS):
            st.insert('', 'end', iid=str(i), values=('◉' if i == 0 else '○', name, self.sound_icon(i), '—', '—', '—', 'ожидание'))
        st.selection_set('0')
        st.bind('<<TreeviewSelect>>', self.select_server)
        st.bind('<Button-1>', self.on_server_click)

        pf = ttk.LabelFrame(main, text='Игроки онлайн')
        pf.pack(fill='both', expand=True, pady=(0, 10))
        wrap = ttk.Frame(pf)
        wrap.pack(fill='both', expand=True, padx=5, pady=5)
        cols = ('fav', 'player', 'score', 'aircraft', 'army')
        pt = ttk.Treeview(wrap, columns=cols, show='headings', selectmode='browse')
        self.player_tree = pt
        for col, title, width, anchor in [('fav','#',40,'center'),('player','Player name',205,'w'),('score','Score',65,'center'),('aircraft','Aircraft',95,'center'),('army','Army',70,'center')]:
            pt.heading(col, text=title)
            pt.column(col, width=width, minwidth=45 if col != 'fav' else 30, anchor=anchor, stretch=(col == 'player'))
        scroll = ttk.Scrollbar(wrap, orient='vertical', command=pt.yview)
        pt.configure(yscrollcommand=scroll.set)
        pt.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        pt.tag_configure('favorite', background='#8061b2', foreground='white')
        pt.tag_configure('axis', background='#f1f5fb')
        pt.bind('<ButtonRelease-1>', self.on_player_checkbox_click)
        pt.bind('<Double-1>', self.open_profile)
        pt.bind('<Button-3>', self.on_player_right_click)
        ttk.Label(pf, text='Галка или ПКМ — избранное. Двойной щелчок — профиль.', foreground='#666').pack(anchor='w', padx=7, pady=(0, 5))

        controls = ttk.Frame(main)
        controls.pack(fill='x')
        self.count_label = ttk.Label(controls, text='Allies: —     Axis: —', font=('Segoe UI', 10, 'bold'))
        self.count_label.pack(side='left')
        ttk.Button(controls, text='Обновить сейчас', command=self.refresh).pack(side='right')
        refresh_options = ttk.Frame(controls)
        refresh_options.pack(side='right', padx=(0, 12))
        ttk.Label(refresh_options, text='Обновлять каждые').pack(side='left', padx=(0, 5))
        self.refresh_spinbox = ttk.Spinbox(refresh_options, from_=5, to=3600, increment=5, width=5,
                                           textvariable=self.refresh_seconds, command=self.on_refresh_interval_change)
        self.refresh_spinbox.pack(side='left')
        ttk.Label(refresh_options, text='сек.').pack(side='left', padx=(4, 0))
        self.refresh_spinbox.bind('<Return>', self.on_refresh_interval_change)
        self.refresh_spinbox.bind('<FocusOut>', self.on_refresh_interval_change)
        opts = ttk.Frame(main)
        opts.pack(fill='x', pady=(8, 5))
        ttk.Label(opts, text='🔊').pack(side='left', pady=(0, 4))
        ttk.Scale(opts, from_=0, to=100, orient='horizontal', variable=self.volume,
                  command=self.on_volume_change, length=150).pack(side='left', padx=(4, 16))
        ttk.Checkbutton(opts, text='only favorites', variable=self.only_favorites,
                        command=self.on_mode_change).pack(side='left')
        ttk.Checkbutton(opts, text='звук при запуске', variable=self.startup_sound,
                        command=self.save_settings).pack(side='left', padx=(14, 0))
        status_row = ttk.Frame(main)
        status_row.pack(fill='x', pady=(4, 0))
        ttk.Label(status_row, textvariable=self.last_update).pack(side='right')
        ttk.Label(status_row, textvariable=self.status, foreground='#555').pack(side='left')

    def play_startup_sound(self):
        if self.startup_sound.get():
            self.audio.play('program-start.mp3')

    def schedule_refresh(self):
        if self.refresh_job is not None:
            self.after_cancel(self.refresh_job)
        self.refresh_job = self.after(self.refresh_seconds.get() * 1000, self.auto_refresh)

    def on_refresh_interval_change(self, _event=None):
        try:
            seconds = int(self.refresh_spinbox.get())
        except (ValueError, tk.TclError):
            seconds = 30
        seconds = max(5, min(3600, seconds))
        self.refresh_seconds.set(seconds)
        self.save_settings()
        self.schedule_refresh()

    def on_player_checkbox_click(self, event):
        tree = self.player_tree
        if tree.identify_region(event.x, event.y) != 'cell' or tree.identify_column(event.x) != '#1':
            return
        row = tree.identify_row(event.y)
        if row:
            self.toggle_favorite_for_row(row)

    def on_player_right_click(self, event):
        row = self.player_tree.identify_row(event.y)
        if row:
            self.toggle_favorite_for_row(row)
        return 'break'

    def toggle_favorite_for_row(self, row):
        values = self.player_tree.item(row, 'values')
        if not values:
            return
        name = values[1]
        matching = next((x for x in self.favorites if x.casefold() == name.casefold()), None)
        if matching:
            self.favorites.remove(matching)
        else:
            self.favorites.add(name)
        self.filter_players()

    def sound_icon(self, idx):
        return '🔊' if self.server_audio_enabled[str(idx)] else '🔇'

    def on_server_click(self, event):
        tree = self.server_tree
        row = tree.identify_row(event.y)
        col = tree.identify_column(event.x)
        # Third column is the independent sound toggle.
        if row and col == '#3':
            enabled = not self.server_audio_enabled[row]
            self.server_audio_enabled[row] = enabled
            vals = list(tree.item(row, 'values'))
            vals[2] = self.sound_icon(int(row))
            tree.item(row, values=vals)
            self.save_settings()
            return 'break'  # Do not select the server when clicking its speaker.

    def select_server(self, _event=None):
        selected = self.server_tree.selection()
        if not selected: return
        self.server_index.set(int(selected[0]))
        for i in range(len(SERVERS)):
            values = list(self.server_tree.item(str(i), 'values'))
            values[0] = '◉' if i == self.server_index.get() else '○'
            self.server_tree.item(str(i), values=values)
        self.only_favorites.set(bool(self.modes.get(str(self.server_index.get()), self.server_index.get() == 0)))
        self.filter_players()

    def on_volume_change(self, value):
        self.audio.set_volume(value)
        self.save_settings()

    def on_mode_change(self):
        self.modes[str(self.server_index.get())] = self.only_favorites.get()
        self.filter_players()

    def refresh(self):
        if self.working: return
        self.working = True
        self.status.set('Обновление серверов...')
        threading.Thread(target=self.worker, daemon=True).start()

    def worker(self):
        for idx, (_, url) in enumerate(SERVERS):
            try: self.results.put((idx, fetch_server(url), None))
            except Exception as e: self.results.put((idx, None, str(e)))
        try: self.results.put(('mission', fetch_mission(), None))
        except Exception as e: self.results.put(('mission', None, str(e)))
        self.results.put(('done', None, None))

    def check_results(self):
        try:
            while True:
                idx, data, error = self.results.get_nowait()
                if idx == 'mission':
                    vals = list(self.server_tree.item('0', 'values'))
                    if error:
                        self.status.set(f'Миссия Wings of Liberty: {error[:110]}')
                    else:
                        vals[4], vals[5] = data['mission'], data['time_left']
                        self.server_tree.item('0', values=vals)
                    continue
                if idx == 'done':
                    self.working = False
                    self.last_update.set(datetime.now().strftime('%d.%m.%Y %H:%M:%S'))
                    self.status.set('Обновлено. Следующая проверка через 30 сек.')
                    continue
                vals = list(self.server_tree.item(str(idx), 'values'))
                if error:
                    vals[3], vals[4], vals[5], vals[6] = '—', '—', '—', 'error'
                    self.status.set(f'{SERVERS[idx][0]}: {error[:130]}')
                else:
                    vals[3], vals[6] = str(data['count']), 'ok'
                    self.notify_new_players(idx, data)
                    self.data[idx] = data
                self.server_tree.item(str(idx), values=vals)
                if idx == self.server_index.get(): self.filter_players()
        except queue.Empty: pass
        self.after(200, self.check_results)

    def notify_new_players(self, idx, data):
        current = {p['name'].casefold(): p['name'] for side in (1, 2) for p in data['players'][side]}
        old = self.previous.get(idx)
        self.previous[idx] = set(current)
        if old is None:
            return  # no notification for players already online at startup
        new = set(current) - old
        favorites_lower = {f.casefold() for f in self.favorites}
        only = bool(self.modes.get(str(idx), idx == 0))
        matches = new & favorites_lower if only else new - favorites_lower
        if matches and self.server_audio_enabled.get(str(idx), True):
            self.audio.play('connecting-a-new-user.mp3')
            self.status.set(f'Новый пилот на {SERVERS[idx][0]}: {", ".join(current[n] for n in sorted(matches))}')

    def filter_players(self):
        for item in self.player_tree.get_children():
            self.player_tree.delete(item)
        data = self.data.get(self.server_index.get())
        if not data:
            self.count_label.configure(text='Allies: —     Axis: —')
            return
        self.count_label.configure(text=f'Allies: {data["allies"]}     Axis: {data["axis"]}')
        favorites_lower = {f.casefold() for f in self.favorites}
        only = self.only_favorites.get()
        rows = [(side, p) for side in (1, 2) for p in data['players'][side]]
        if only:
            rows.sort(key=lambda x: (x[1]['name'].casefold() not in favorites_lower, x[0], x[1]['name'].casefold()))
        for side, p in rows:
            favorite = p['name'].casefold() in favorites_lower
            army = 'Allies' if side == 1 else 'Axis'
            tag = 'favorite' if only and favorite else ('axis' if side == 2 else '')
            self.player_tree.insert('', 'end', values=('☑' if favorite else '☐', p['name'], '—', '—', army), tags=(tag,), iid=f'{side}|{p["name"]}')
        self.save_settings()

    def toggle_favorite(self, _event=None):
        sel = self.player_tree.selection()
        if sel:
            self.toggle_favorite_for_row(sel[0])

    def open_profile(self, event=None):
        row = self.player_tree.identify_row(event.y) if event is not None else None
        if not row:
            sel = self.player_tree.selection()
            if not sel: return
            row = sel[0]
        name = self.player_tree.item(row, 'values')[1]
        data = self.data.get(self.server_index.get(), {})
        for side in (1, 2):
            for p in data.get('players', {}).get(side, []):
                if p['name'] == name and p['href'].startswith('/ru/pilot/'):
                    from urllib.parse import urljoin
                    webbrowser.open(urljoin(SERVERS[self.server_index.get()][1], p['href']))
                    return

    def open_favorites(self):
        win = tk.Toplevel(self)
        self.apply_app_icon(win)
        win.title('Favorites list')
        win.geometry('290x400')
        win.transient(self)
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill='both', expand=True)
        lb = tk.Listbox(frame, font=('Segoe UI', 10))
        lb.pack(fill='both', expand=True)
        entry = ttk.Entry(frame)
        entry.pack(fill='x', pady=(8, 5))
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x')
        def redraw():
            lb.delete(0, 'end')
            for n in sorted(self.favorites, key=str.casefold): lb.insert('end', n)
            self.filter_players()
        def add():
            name = entry.get().strip()
            if name and not any(x.casefold() == name.casefold() for x in self.favorites): self.favorites.add(name)
            entry.delete(0, 'end')
            redraw()
        def delete():
            selection = lb.curselection()
            if selection:
                self.favorites.discard(lb.get(selection[0]))
                redraw()
        ttk.Button(buttons, text='DEL', command=delete).pack(side='left')
        ttk.Button(buttons, text='ADD', command=add).pack(side='right')
        entry.bind('<Return>', lambda _e: add())
        redraw()

    def open_find(self):
        win = tk.Toplevel(self)
        self.apply_app_icon(win)
        win.title('Find player')
        win.geometry('320x190')
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill='both', expand=True)
        entry = ttk.Entry(frame)
        entry.pack(fill='x', pady=(8, 14))
        result = ttk.Label(frame, text='Введите никнейм или его часть', wraplength=285)
        result.pack(fill='x')
        def find():
            term = entry.get().strip().casefold()
            if not term:
                result.configure(text='Введите имя пилота')
                return
            matches = []
            for idx, data in self.data.items():
                for side in (1, 2):
                    for p in data['players'][side]:
                        if term in p['name'].casefold(): matches.append(f'{p["name"]} — {SERVERS[idx][0]}')
            result.configure(text='\n'.join(matches[:8]) if matches else 'Пилот не найден в последних полученных списках')
        ttk.Button(frame, text='FIND', command=find).pack(anchor='e', pady=10)
        entry.bind('<Return>', lambda _e: find())

    def about(self):
        messagebox.showinfo('About il2_scan', 'il2_scan v2.1\nРазработано с помощью ChatGPT\n22.10.2026 00:28 (UTC+2)')

    def auto_refresh(self):
        self.refresh_job = None
        if self._exit_requested:
            return
        self.refresh()
        self.schedule_refresh()

    def close(self):
        self._exit_requested = True
        self.save_settings()
        self.stop_tray_icon()
        if self.activation_server is not None:
            self.activation_server.close()
        self.audio.close()
        self.destroy()

if __name__ == '__main__':
    mutex_handle, is_first = acquire_single_instance()
    if not is_first:
        request_existing_window()
        if mutex_handle:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex_handle))
        sys.exit(0)

    activation = None
    try:
        if sys.platform == 'win32':
            activation = ActivationServer()
        WTConnect(activation_server=activation).mainloop()
    finally:
        if activation is not None:
            activation.close()
        if mutex_handle:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex_handle))
