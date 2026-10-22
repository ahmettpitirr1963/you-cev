"""GPU (CUDA) kütüphanelerinin Windows'ta bulunur hale getirilmesi. Whisper ve NLLB ortak kullanır."""
import os
import sys
import threading
from pathlib import Path

_done = False
_lock = threading.Lock()


def add_nvidia_dlls():
    """pip ile kurulan nvidia-cublas / nvidia-cudnn DLL'lerini bulunur yapar (bir kez)."""
    global _done
    with _lock:
        if _done or sys.platform != "win32":
            return
        for base in map(Path, sys.path):
            nv = base / "nvidia"
            if not nv.is_dir():
                continue
            for sub in nv.iterdir():
                dll_dir = sub / "bin"
                if dll_dir.is_dir():
                    os.add_dll_directory(str(dll_dir))
                    os.environ["PATH"] = str(dll_dir) + os.pathsep + os.environ.get("PATH", "")
        _done = True
