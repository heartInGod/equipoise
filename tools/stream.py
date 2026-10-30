"""连续视频流截图：tools/vstream（adb screenrecord H.264 裸流 -> VideoToolbox 硬解，内存只留最新一帧）。
接口与 bot_core.Grabber 相同：get(min_t) -> (帧文件路径, 帧时间)，帧文件是与 adb screencap 相同的 .raw
（16 字节头 w,h,1,1 + RGBA），下游 parse / keep_frame / golden 不用改。无额外依赖（不需要 ffmpeg / scrcpy）。

帧时间语义（与 Grabber 的“截图开始时间”同样保守）：
- screenrecord 只在画面变化时出帧。流活着时，最新一帧就是“当前画面”，有效到 now - LAT
  （LAT = 画面变化 -> 解码完成 的延迟上界，实测见 SKILL.md）。所以 get() 返回的 t 一律取 now - LAT；
- 有新帧立即返回；没有新帧时每 REPEAT 秒把同一帧再返回一次，让 bot 的计时（卡住检测、预设补点等）照常走；
- screenrecord 单段最长 180s（系统写死，不能调大）：vstream 每 170s 提前起下一段，新段出首帧即切换（无缝，不打日志）；
  只有段意外断开才有空档，空档期 / vstream 挂掉时退回一次 adb screencap。"""
import os, subprocess, threading, time
from pathlib import Path

import logroll

LOG = logroll.logger("stream")
BIN = Path(__file__).resolve().parent / "vstream"
LAT = 0.25            # 画面变化 -> 解码完成 的延迟上界（秒）
REPEAT = 0.5          # 静止画面重复返回同一帧的间隔
BIT_RATE = 20_000_000


def available():
    return BIN.exists() and os.access(BIN, os.X_OK)


class StreamGrabber:
    def __init__(self, io, ring_dir, n=6, lat=LAT, repeat=REPEAT, bit_rate=BIT_RATE):
        self.io, self.ring, self.n = io, Path(ring_dir), n
        self.ring.mkdir(parents=True, exist_ok=True)
        self.lat, self.repeat, self.bit_rate = lat, repeat, bit_rate
        self.p, self.seq, self.path, self.i = None, 0, None, 0
        self.t_ret, self.periods, self.stop, self.fb = 0.0, [], False, 0
        self.lock = threading.Lock()

    # ---------------- 进程 ----------------
    def start(self):
        self._spawn(); return self

    def _spawn(self):
        self._kill()
        cmd = [str(BIN), "--", self.io.adb, "-s", self.io.dev, "exec-out", "screenrecord",
               "--output-format=h264", "--bit-rate", str(self.bit_rate), "-"]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        self.seq, self.path = 0, None
        threading.Thread(target=self._errlog, args=(self.p,), daemon=True).start()
        LOG.info("[stream] 视频流启动 bit_rate=%d lat=%.2fs", self.bit_rate, self.lat)

    @staticmethod
    def _errlog(p):
        for line in p.stderr:
            line = line.strip()
            if line.startswith("stream ended"): LOG.warning("[stream] 视频流意外断开，自动重连（%s）", line)
            elif line.startswith(("rotated", "format")): pass                  # 每 170s 无缝换段，正常
            elif line: LOG.warning("[stream] %s", line)

    def _kill(self):
        """先关 stdin + SIGTERM，让 vstream 关掉它的 screenrecord 子进程；2s 不退再 kill"""
        if self.p is not None:
            try: self.p.stdin.close()
            except Exception: pass
            try: self.p.terminate(); self.p.wait(timeout=2)
            except Exception:
                try: self.p.kill()
                except Exception: pass
        self.p = None

    def close(self):
        self.stop = True; self._kill()

    def _cmd(self, s):
        if self.p is None or self.p.poll() is not None: raise OSError("vstream 不在")
        self.p.stdin.write(s + "\n"); self.p.stdin.flush()
        r = self.p.stdout.readline()
        if not r: raise OSError("vstream 退出")
        return r.split()

    # ---------------- 取帧 ----------------
    def _mark(self, t):
        if self.t_ret: self.periods = (self.periods + [t - self.t_ret])[-50:]
        self.t_ret = t

    def _fallback(self, min_t):
        """流不可用：退回一次 adb screencap（t = 截图开始时间，与 Grabber 相同）"""
        self.fb += 1
        if self.fb % 20 == 1: LOG.warning("[stream] 视频流不可用，退回 screencap（第 %d 次）", self.fb)
        d = min_t - time.time()
        if d > 0: time.sleep(d)
        slot = self.ring / f"s{self.i % self.n}.raw"; self.i += 1
        tmp = self.ring / "stream_fb.tmp"
        t = time.time()
        try: ok = self.io.capture(str(tmp))
        except Exception: ok = False
        if not ok:
            time.sleep(1); return None
        os.replace(tmp, slot)
        self.path, self.seq = None, self.seq        # 下一帧重新从流里 snap
        self._mark(t)
        return str(slot), t

    def get(self, min_t, timeout=600):
        end = time.time() + timeout
        with self.lock:
            while time.time() < end and not self.stop:
                d = min_t + self.lat - time.time()  # 返回的内容必须反映 min_t 之后的画面
                if d > 0: time.sleep(d)
                try:
                    if self.p is None or self.p.poll() is not None:
                        LOG.warning("[stream] vstream 不在，重启"); self._spawn()
                    ms = 1500 if self.path is None else max(0, int((self.t_ret + self.repeat - time.time() + self.lat) * 1000))
                    seq, _t_recv, alive, _restarts, _errs = self._cmd(f"wait {self.seq} {ms}")
                    seq, alive = int(seq), alive == "1"
                    if not alive:
                        r = self._fallback(min_t)
                        if r: return r
                        continue
                    t = time.time() - self.lat
                    if seq > self.seq or self.path is None:
                        slot = self.ring / f"s{self.i % self.n}.raw"; self.i += 1
                        r = self._cmd(f"snap {slot}")
                        if r[0] != "ok": raise OSError("snap 失败: " + " ".join(r))
                        self.seq, self.path = int(r[1]), str(slot)   # 原子替换，已 hardlink 走的旧帧不受影响
                    self._mark(t)
                    return self.path, t
                except (OSError, ValueError) as e:
                    LOG.warning("[stream] %s，重启 vstream", e)
                    self._kill()
                    r = self._fallback(min_t)
                    if r: return r
        raise RuntimeError("截图超时（设备掉线？）")
