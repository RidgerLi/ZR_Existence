import time
from collections import deque
from typing import Union

from common.concurrent.abs_runnable import ThreadRunnable
from devices.headless import is_headless
from event.event_emitter import emitter
from event.event_data import DeviceKeyboardPressEvent
import threading
from loguru import logger

if not is_headless():
    try:
        from pynput import keyboard
        from pynput.keyboard import Key, KeyCode
    except:
        raise ImportError(f'Pynput not installed, please try "pip install pynput" to solve this problem.')

"""
Keyborad 函数只监听所有特定的按键, 并触发控制函数 hotkey_handler
(目前只在 Windows11 下测试过)
"""
class SmartKeyboard(ThreadRunnable):
    def __init__(self, hotkeys: list):
        super().__init__()
        self._hotkeys = set()
        for string in hotkeys:
            assert type(string) is str, "hotkeys must be string type"
            if string in self._hotkeys:
                assert False, "some hotkeys are set to be the same, please check your config.yaml setting"
            self._hotkeys.add(self.str_to_key(string))
        self._current_hotkey: Union[Key, KeyCode] = None
        self._toggle_debounce: bool = False   # 防抖
        self._key_listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release
        )
        self._key_listener.daemon = False

        # 外部环境可用的锁
        self.microphone_state_lock = threading.Lock()

        # 全局敲击时间戳（供大脑感知层判定"专注打字"做模式自动感知）。
        # 记录所有按键（不止热键），按滑动窗口统计频率。
        self._key_times: deque = deque()
        self._key_times_lock = threading.Lock()
        self._key_times_window_s = 60.0

    def start(self):
        super().start()
        try:
            self._key_listener.start()
        except Exception as e:
            logger.error(e)

    def stop(self):
        super().stop()
        self._key_listener.stop()
    
    def _on_key_press(self, key):
        # logger.debug(f'Press {key}')
        # 先记录全局敲击（所有按键），用于活跃度统计。
        self._record_keystroke()

        if key not in self._hotkeys:
            return
        
        if self._toggle_debounce:
            return
        self._toggle_debounce = True
        self._current_hotkey = key

        emitter.emit(DeviceKeyboardPressEvent(hotkey=self.key_to_str(key)))

    def _on_key_release(self, key):
        # logger.debug(f'Release {key}')
        if key == self._current_hotkey:
            self._toggle_debounce = False
            self._current_hotkey = None
    
    def _record_keystroke(self):
        now = time.monotonic()
        with self._key_times_lock:
            self._key_times.append(now)
            cutoff = now - self._key_times_window_s
            while self._key_times and self._key_times[0] < cutoff:
                self._key_times.popleft()

    def recent_keystroke_count(self, window_s: float = 30.0) -> int:
        """返回最近 window_s 秒内的全局敲击次数（供大脑模式自动感知）。"""
        now = time.monotonic()
        cutoff = now - min(window_s, self._key_times_window_s)
        with self._key_times_lock:
            return sum(1 for t in self._key_times if t >= cutoff)

    def name(self):
        return "SmartKeyboard"
    
    @staticmethod
    def str_to_key(s: str) -> Union[Key, KeyCode]:
        if not isinstance(s, str):
            raise TypeError("hotkey must be str")

        s = s.strip()
        if not s:
            raise ValueError("hotkey string is empty")

        # 不允许传 "Key.f8" 这种写法
        # if s.lower().startswith("key."):
        #     s = s.split(".", 1)[1].strip()

        name = s.lower()

        if name in Key.__members__:
            return Key[name]

        if len(s) == 1:
            return KeyCode.from_char(s)

        raise ValueError(
            f"Unknown key: {s!r}. "
            f"Try one of Key names like: {', '.join(list(Key.__members__.keys())[:20])} ..."
        )
    
    @staticmethod
    def key_to_str(k: Union[Key, KeyCode]) -> str:
        if hasattr(k, "name") and k.name is not None:   # Key
            return k.name
        if hasattr(k, "char") and k.char is not None:   # KeyCode
            return k.char
        return str(k)