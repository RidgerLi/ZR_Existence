import threading
from enum import Enum
from pathlib import Path
from queue import Queue
from typing import Callable

import pygame
from loguru import logger

from common.concurrent.abs_runnable import ThreadRunnable
from common.concurrent.killable_thread import KillableThread
from event.event_data import DeviceSpeakerPlayEvent
from event.event_emitter import emitter

pygame.mixer.init()
_system_sound = False


class SystemSoundEnum(str, Enum):
    warn: str = "warn.wav"
    error: str = "error.wav"
    start: str = "start.wav"
    exit: str = "exit.wav"
    enable_func: str = "microphone-recoding.wav"
    disable_func: str = "microphone-stopped.wav"
    filtered: str = "filtered.wav"


class Speaker(ThreadRunnable):

    def name(self):
        return 'Speaker'

    def __init__(self):
        super().__init__()
        self._stop_flag = False
        self._semaphore = threading.Event()
        self._speaker_thread = KillableThread(target=self._run)
        self.audio_clips: Queue[Path] = Queue()
        # 本地播放开始 / 结束的回调钩子。用于半双工回声抑制：
        # 播放本地音频前后通知麦克风关门 / 开门，避免机器人采集到自己的声音。
        self._on_playback_start: Callable[[], None] | None = None
        self._on_playback_end: Callable[[], None] | None = None

    def set_playback_hooks(self, on_start: Callable[[], None] | None,
                           on_end: Callable[[], None] | None):
        """设置本地播放前后的回调（用于半双工回声抑制）。"""
        self._on_playback_start = on_start
        self._on_playback_end = on_end

    def start(self):
        super().start()
        self._stop_flag = False
        self._semaphore.set()
        self._speaker_thread.start()

    def stop(self):
        super().stop()
        self._stop_flag = True
        self.audio_clips = None
        self._speaker_thread.kill()

    def _run(self):
        while not self._stop_flag:

            if self.audio_clips.empty():
                self._semaphore.clear()
            self._semaphore.wait()
            audio_clip = self.audio_clips.get()
            emitter.emit(DeviceSpeakerPlayEvent(audio_path=audio_clip))
            # 半双工：播放前关闭麦克风采集，播放后再恢复（钩子内部负责尾巴延时）。
            if self._on_playback_start is not None:
                try:
                    self._on_playback_start()
                except Exception as e:
                    logger.exception(e)
            try:
                self.playsound(audio_clip, block=True)
            finally:
                if self._on_playback_end is not None:
                    try:
                        self._on_playback_end()
                    except Exception as e:
                        logger.exception(e)

    def enqueue_sound(self, path_or_data: Path):
        self.activate_check()
        self.audio_clips.put(path_or_data)
        self._semaphore.set()

    def stop_now(self):
        pygame.mixer.stop()
        self.audio_clips = Queue()

    @staticmethod
    def playsound(path: Path, block: bool = True):
        if block:
            Speaker._sync_playsound(path)
        else:
            Speaker._async_playsound(path)

    @staticmethod
    def _sync_playsound(path: Path):
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        Speaker.wait()

    @staticmethod
    def wait():
        while pygame.mixer.music.get_busy():
            continue

    @staticmethod
    def _async_playsound(path: Path):
        sound = pygame.mixer.Sound(path)
        pygame.mixer.Sound.play(sound)
