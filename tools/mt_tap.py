"""四指輕點偵測：直接讀 MultitouchSupport（macOS 私有框架）的原始觸控資料。

這就是 BetterTouchTool 底層用的方法。不需要任何權限，每秒約 100 幀，
每幀給出每根手指的狀態與正規化座標 (0~1)。

判斷規則（一次「觸控段」= 從第一根手指落板到全部離板）：
  - 段內同時在板上的手指數最多剛好 = fingers（預設 4）
  - 整段長度 < max_duration 秒
  - 每根手指移動距離 < max_move（避免把四指滑動當成輕點）
符合就呼叫 on_tap（在框架自己的執行緒上，呼叫端自行丟回主執行緒）。
"""

import ctypes
import threading
import time

_MT_PATH = "/System/Library/PrivateFrameworks/MultitouchSupport.framework/MultitouchSupport"
_CF_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_float), ("y", ctypes.c_float)]


class _Readout(ctypes.Structure):
    _fields_ = [("pos", _Point), ("vel", _Point)]


class _Finger(ctypes.Structure):
    _fields_ = [
        ("frame", ctypes.c_int),
        ("timestamp", ctypes.c_double),
        ("identifier", ctypes.c_int),
        ("state", ctypes.c_int),       # 3=落板 4=在板上 5=離板
        ("foo3", ctypes.c_int),
        ("foo4", ctypes.c_int),
        ("normalized", _Readout),      # 0~1 座標
        ("size", ctypes.c_float),
        ("zero1", ctypes.c_int),
        ("angle", ctypes.c_float),
        ("major_axis", ctypes.c_float),
        ("minor_axis", ctypes.c_float),
        ("mm", _Readout),
        ("zero2", ctypes.c_int * 2),
        ("unk2", ctypes.c_float),
    ]


_CALLBACK = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_Finger),
    ctypes.c_int, ctypes.c_double, ctypes.c_int,
)

_TOUCHING = (3, 4)


class TapDetector:
    def __init__(self, on_tap, fingers=4, max_duration=0.35, max_move=0.05,
                 debounce=0.5):
        self.on_tap = on_tap
        self.fingers = fingers
        self.max_duration = max_duration
        self.max_move = max_move
        self.debounce = debounce
        self._lock = threading.Lock()
        self._reset()
        self._last_tap = 0.0
        self._cb = _CALLBACK(self._frame)   # 保住參照，否則被 GC 後框架會呼叫到野指標
        self._devices = []
        self.frames = 0                      # 診斷用：收到的幀數

    def _reset(self):
        self._active = False
        self._start = 0.0
        self._max = 0
        self._origin = {}
        self._moved = False

    def _frame(self, _dev, data, n, ts, _frame):
        try:
            self.frames += 1
            touching = [data[i] for i in range(n) if data[i].state in _TOUCHING]
            with self._lock:
                if touching:
                    if not self._active:
                        self._reset()
                        self._active, self._start = True, ts
                    self._max = max(self._max, len(touching))
                    for f in touching:
                        p = (f.normalized.pos.x, f.normalized.pos.y)
                        o = self._origin.setdefault(f.identifier, p)
                        if abs(p[0] - o[0]) > self.max_move or abs(p[1] - o[1]) > self.max_move:
                            self._moved = True
                elif self._active:
                    dur = ts - self._start
                    ok = (self._max == self.fingers and dur < self.max_duration
                          and not self._moved)
                    self._reset()
                    if ok and time.time() - self._last_tap > self.debounce:
                        self._last_tap = time.time()
                        self.on_tap()
        except Exception:
            pass
        return 0

    def start(self):
        """回傳啟動了幾個觸控裝置；0 表示框架不可用。"""
        mt = ctypes.CDLL(_MT_PATH)
        cf = ctypes.CDLL(_CF_PATH)
        mt.MTDeviceCreateList.restype = ctypes.c_void_p
        mt.MTRegisterContactFrameCallback.argtypes = [ctypes.c_void_p, _CALLBACK]
        mt.MTDeviceStart.argtypes = [ctypes.c_void_p, ctypes.c_int]
        cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p

        devs = mt.MTDeviceCreateList()
        if not devs:
            return 0
        self._devlist = devs  # 保住 CFArray
        for i in range(cf.CFArrayGetCount(devs)):
            d = cf.CFArrayGetValueAtIndex(devs, i)
            mt.MTRegisterContactFrameCallback(d, self._cb)
            mt.MTDeviceStart(d, 0)
            self._devices.append(d)
        self._mt = mt
        return len(self._devices)


if __name__ == "__main__":  # 診斷：python3 mt_tap.py 然後在觸控板上四指輕點
    det = TapDetector(on_tap=lambda: print("*** 4 finger tap ***", flush=True))
    print("devices:", det.start())
    print("在觸控板上試著四指輕點（Ctrl+C 結束）")
    while True:
        time.sleep(1)
        print("frames:", det.frames, flush=True)
