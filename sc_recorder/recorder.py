import os
import shutil
import subprocess
import tempfile
import threading
import time
import tkinter as tk
import wave
import imageio_ffmpeg
import dxcam
import pyaudiowpatch as pyaudio
from tkinter import filedialog, messagebox, ttk

import mss

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def get_ffmpeg():
    try:
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        path = shutil.which("ffmpeg")
        if path:
            return path
    raise RuntimeError("ffmpeg tapılmadı. 'pip install imageio-ffmpeg' yaz.")


class DxcamGrabber:

    def __init__(self):
        self.cam = dxcam.create(output_color="BGRA")
        frame = None
        end = time.time() + 3
        while frame is None and time.time() < end:
            frame = self.cam.grab()
            if frame is None:
                time.sleep(0.02)
        if frame is None:
            raise RuntimeError("dxcam kadr verə bilmədi")
        self.height, self.width = frame.shape[:2]
        self.last_bytes = frame.tobytes()

    def grab(self):
        frame = self.cam.grab()
        if frame is not None:  
            self.last_bytes = frame.tobytes()
        return self.last_bytes

    def close(self):
        try:
            self.cam.release()
        except Exception:
            pass


class MssGrabber:

    def __init__(self):
        self.sct = mss.mss()
        self.mon = self.sct.monitors[1]
        self.width, self.height = self.mon["width"], self.mon["height"]

    def grab(self):
        return self.sct.grab(self.mon).bgra

    def close(self):
        try:
            self.sct.close()
        except Exception:
            pass


def make_grabber():
    try:
        return DxcamGrabber()
    except Exception:
        return MssGrabber()


class AudioRecorder:

    def __init__(self, path):
        self.path = path
        self.pa = None
        self.stream = None
        self.wf = None

    def start(self, t0):

        self._continue = pyaudio.paContinue
        self.pa = pyaudio.PyAudio()
        try:
            wasapi = self.pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            dev = self.pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
            if not dev.get("isLoopbackDevice"):
                for lb in self.pa.get_loopback_device_info_generator():
                    if dev["name"] in lb["name"]:
                        dev = lb
                        break
                else:
                    raise RuntimeError("Loopback səs cihazı tapılmadı")

            self.channels = int(dev["maxInputChannels"])
            self.rate = int(dev["defaultSampleRate"])
            self.t0 = t0
            self.written = 0

            self.wf = wave.open(self.path, "wb")
            self.wf.setnchannels(self.channels)
            self.wf.setsampwidth(2)
            self.wf.setframerate(self.rate)

            self.stream = self.pa.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.rate,
                input=True,
                input_device_index=dev["index"],
                frames_per_buffer=1024,
                stream_callback=self._callback,
            )
        except Exception:
            self.pa.terminate()
            raise

    def _silence(self, samples):
        if samples > 0:
            self.wf.writeframes(b"\x00" * (samples * self.channels * 2))
            self.written += samples

    def _callback(self, in_data, frame_count, time_info, status):
        expected = int((time.perf_counter() - self.t0) * self.rate)
        gap = expected - self.written - frame_count
        if gap > self.rate // 20:
            self._silence(gap)
        self.wf.writeframes(in_data)
        self.written += frame_count
        return (None, self._continue)

    def stop(self, total_seconds):
        try:
            self.stream.stop_stream()
            self.stream.close()
        except Exception:
            pass
        try:
            self._silence(int(total_seconds * self.rate) - self.written)
            self.wf.close()
        except Exception:
            pass
        try:
            self.pa.terminate()
        except Exception:
            pass


class ScreenRecorderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Screen Recorder")
        self.root.geometry("340x300")
        self.root.resizable(False, False)

        self.recording = False
        self.thread = None
        self.stop_event = threading.Event()
        self.error = None
        self.notes = []
        self.output_path = None
        self.start_time = 0

        self.status = tk.Label(root, text="ready", font=("Arial", 12))
        self.status.pack(pady=(12, 2))

        self.timer = tk.Label(root, text="00:00:00", font=("Consolas", 24, "bold"))
        self.timer.pack(pady=2)

        opts = tk.Frame(root)
        opts.pack(pady=6)

        tk.Label(opts, text="FPS:").grid(row=0, column=0, sticky="e", padx=5, pady=2)
        self.fps_var = tk.StringVar(value="30")
        self.fps_box = ttk.Combobox(opts, textvariable=self.fps_var, width=12,
                                    values="30", state="readonly")
        self.fps_box.grid(row=0, column=1, pady=2)

        tk.Label(opts, text="Quality:").grid(row=1, column=0, sticky="e", padx=5, pady=2)
        self.res_var = tk.StringVar(value="1080p")
        self.res_box = ttk.Combobox(opts, textvariable=self.res_var, width=12,
                                    values="1080p", state="readonly")
        self.res_box.grid(row=1, column=1, pady=2)

        self.audio_var = tk.BooleanVar(value=True)
        self.audio_chk = tk.Checkbutton(root, text="Record System Audio", variable=self.audio_var)
        self.audio_chk.pack()

        btns = tk.Frame(root)
        btns.pack(pady=12)
        self.start_btn = tk.Button(btns, text="Start Record", width=13, height=2,
                                   bg="#2e9e4f", fg="white", command=self.start_recording)
        self.start_btn.grid(row=0, column=0, padx=8)
        self.stop_btn = tk.Button(btns, text="Stop", width=13, height=2,
                                  bg="#c0392b", fg="white", state="disabled",
                                  command=self.stop_recording)
        self.stop_btn.grid(row=0, column=1, padx=8)

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _set_options_state(self, enabled):
        self.fps_box.config(state="readonly" if enabled else "disabled")
        self.res_box.config(state="readonly" if enabled else "disabled")
        self.audio_chk.config(state="normal" if enabled else "disabled")

   
    def start_recording(self):
        path = filedialog.asksaveasfilename(
            title="Where do you want to save the video?",
            defaultextension=".mp4",
            filetypes=[("MP4 video", "*.mp4")],
            initialfile="recording.mp4",
        )
        if not path:
            return

        self.output_path = path
        self.error = None
        self.notes = []
        self.stop_event.clear()
        self.recording = True
        self.start_time = time.time()

        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._set_options_state(False)
        self.status.config(text="● Recording...", fg="red")

        args = (path, int(self.fps_var.get()), self.res_var.get(), self.audio_var.get())
        self.thread = threading.Thread(target=self._record, args=args, daemon=True)
        self.thread.start()
        self._update_timer()


    def _record(self, path, fps, res, want_audio):
        tmpdir = tempfile.mkdtemp(prefix="screenrec_")
        video_tmp = os.path.join(tmpdir, "video.mp4")
        wav_tmp = os.path.join(tmpdir, "audio.wav")
        grabber = proc = audio = None
        t0 = None

        try:
            ffmpeg = get_ffmpeg()
            grabber = make_grabber()
            w, h = grabber.width, grabber.height

            if res == "1080p":
                vf = "scale=-2:'min(ih,1080)':flags=fast_bilinear"
            elif res == "720p":
                vf = "scale=-2:'min(ih,720)':flags=fast_bilinear"
            else:
                vf = "scale=trunc(iw/2)*2:trunc(ih/2)*2"

            cmd = [
                ffmpeg, "-y",
                "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{w}x{h}",
                "-framerate", str(fps), "-i", "-",
                "-vf", vf,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26",
                "-pix_fmt", "yuv420p",
                video_tmp,
            ]
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)

            t0 = time.perf_counter()
            if want_audio:
                try:
                    audio = AudioRecorder(wav_tmp)
                    audio.start(t0)
                except Exception as e:
                    audio = None
                    self.notes.append(f"Audio could not be recorded: {e}")

            frames_written = 0
            while not self.stop_event.is_set():
                elapsed = time.perf_counter() - t0
                missing = int(elapsed * fps) + 1 - frames_written
                if missing <= 0:
                    time.sleep(0.002)
                    continue
                data = grabber.grab()
                for _ in range(missing):
                    proc.stdin.write(data)
                frames_written += missing

        except Exception as e:
            self.error = str(e)

        finally:
            total = (time.perf_counter() - t0) if t0 else 0
            if grabber:
                grabber.close()
            if proc:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                proc.wait()
            if audio:
                audio.stop(total)

        if not self.error:
            try:
                if audio and os.path.exists(wav_tmp):
                    cmd = [ffmpeg, "-y", "-i", video_tmp, "-i", wav_tmp,
                           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                           "-shortest", path]
                    r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
                    if r.returncode != 0:
                        raise RuntimeError("Video and audio could not be combined.")
                else:
                    shutil.move(video_tmp, path)
            except Exception as e:
                self.error = str(e)

        shutil.rmtree(tmpdir, ignore_errors=True)

    def stop_recording(self):
        if not self.recording:
            return
        self.stop_event.set()
        self.stop_btn.config(state="disabled")
        self.status.config(text="Saving...", fg="black")
        self.root.after(200, self._wait_for_finish)

    def _wait_for_finish(self):
        if self.thread and self.thread.is_alive():
            self.root.after(200, self._wait_for_finish)
            return

        self.recording = False
        self.start_btn.config(state="normal")
        self._set_options_state(True)
        self.status.config(text="Ready", fg="black")

        if self.error:
            messagebox.showerror("Error", self.error)
        else:
            msg = f"Video saved:\n{self.output_path}"
            if self.notes:
                msg += "\n\n" + "\n".join(self.notes)
            messagebox.showinfo("Ready", msg)

    def _update_timer(self):
        if self.recording and not self.stop_event.is_set():
            elapsed = int(time.time() - self.start_time)
            h, rem = divmod(elapsed, 3600)
            m, s = divmod(rem, 60)
            self.timer.config(text=f"{h:02d}:{m:02d}:{s:02d}")
            self.root.after(500, self._update_timer)

    def on_close(self):
        if self.recording:
            if not messagebox.askyesno("Recording in Progress", "Do you want to stop recording and exit?"):
                return
            self.stop_event.set()
            if self.thread:
                self.thread.join()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    ScreenRecorderApp(root)
    root.mainloop()