import os
import re
import json
import subprocess
import sys
import locale
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, scrolledtext, ttk, messagebox
import threading
import shutil
import time

# ==================== 配置文件路径（兼容打包） ====================
CONFIG_FILE = "config.json"


def setup_dpi_awareness():
    """
    配置 Windows 高 DPI 适配。
    返回：
        system_scale: 系统DPI缩放比例（100% = 1.0，125% = 1.25）
        is_high_dpi: 是否启用了高 DPI 感知
    """
    system_scale = 1.0
    if sys.platform == "win32":
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
            hdc = windll.user32.GetDC(0)
            dpi = windll.gdi32.GetDeviceCaps(hdc, 90)
            windll.user32.ReleaseDC(0, hdc)
            system_scale = dpi / 96.0
            return system_scale, True
        except Exception as e:
            print(f"DPI 配置失败: {e}")
            return system_scale, False
    return system_scale, False


def resource_path(relative_path):
    """获取资源的绝对路径，兼容开发环境和 PyInstaller 打包后的环境"""
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def get_config_path():
    """获取配置文件的写入路径（打包后保存到 %APPDATA%）"""
    if getattr(sys, 'frozen', False):
        appdata = os.environ.get('APPDATA', os.path.expanduser('~'))
        config_dir = os.path.join(appdata, 'B站缓存转MP4')
        if not os.path.exists(config_dir):
            os.makedirs(config_dir)
        return os.path.join(config_dir, CONFIG_FILE)
    else:
        return CONFIG_FILE


def load_config():
    config_path = get_config_path()
    default = {
        "default_input_dir": os.path.join(os.path.expanduser("~"), "Videos", "bilibili"),
        "default_output_dir": os.path.expanduser("~"),
        "naming_template": "[title]_[UP]",
        "delete_original": False,
        "duplicate_handling": "覆盖",
        "log_level": "INFO",
        "log_enabled": True,
        "open_output_dir": False,
        "ui_scale": 100,
        "cached_dirs": []
    }
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for k in default:
                    if k not in data:
                        data[k] = default[k]
                return data
        except PermissionError:
            print(f"权限不足：无法读取配置文件 {config_path}")
            return default
        except json.JSONDecodeError:
            print(f"配置文件格式错误：{config_path}，将使用默认配置")
            return default
        except FileNotFoundError:
            print(f"配置文件不存在：{config_path}")
            return default
        except OSError as e:
            print(f"读取配置文件失败: {config_path} - {e}")
            return default
        except Exception as e:
            print(f"加载配置文件失败: {type(e).__name__}: {e}")
            return default
    return default


def save_config(data):
    config_path = get_config_path()
    try:
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except PermissionError:
        print(f"权限不足：无法写入配置文件 {config_path}")
    except OSError as e:
        if e.errno == 28:
            print(f"磁盘空间不足：无法保存配置文件")
        else:
            print(f"写入配置文件失败: {config_path} - {e}")
    except Exception as e:
        print(f"保存配置文件失败: {type(e).__name__}: {e}")


# ==================== 右下角通知 ====================
def show_notification(title, msg):
    """使用 plyer 发送右下角通知，自动消失，兼容 Win7 ~ Win11"""
    try:
        from plyer import notification
        notification.notify(
            title=title,
            message=msg,
            timeout=5
        )
    except ImportError:
        # 库没装就静默失败，不影响主功能
        print(f"通知: {title} - {msg}")
    except Exception as e:
        print(f"通知失败: {e}")


# ==================== 转换器类 ====================
class BilibiliCacheConverter:
    def __init__(self, root_directory):
        self.root_directory = root_directory
        self.output_base_dir = None
        self.naming_template = "[UP]_[title]"
        self.duplicate_handling = "覆盖"
        self.delete_original = False
        self.ffmpeg_path = self._find_ffmpeg()
        self.stop_requested = False

    def _find_ffmpeg(self):
        """一次性检测 ffmpeg 路径，优先用自带的，其次用系统 PATH"""
        bundled = resource_path("ffmpeg.exe")
        if os.path.exists(bundled):
            return bundled
        system_ffmpeg = shutil.which("ffmpeg")
        if system_ffmpeg:
            return system_ffmpeg
        return None

    def request_stop(self):
        self.stop_requested = True

    def scan_cache_directories(self):
        return self._scan_cache_directories(self.root_directory, None)

    def scan_cache_directories_incremental(self, root_dir, cached_set):
        return self._scan_cache_directories(root_dir, cached_set)

    def _scan_cache_directories(self, root_dir, cached_set=None):
        if not os.path.exists(root_dir):
            return []
        if sys.platform == "win32":
            return self._scan_windows_fast(root_dir, cached_set)
        return self._scan_os_walk(root_dir, cached_set)

    def _scan_windows_fast(self, root_dir, cached_set=None):
        try:
            cmd = ['cmd.exe', '/c', f'dir /s /b "{root_dir}"']
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE

            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding=locale.getpreferredencoding(False),
                errors='replace',
                startupinfo=startupinfo
            )

            if result.returncode != 0:
                return self._scan_os_walk(root_dir, cached_set)

            target_dirs = set()
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line.endswith('.m4s') or not os.path.isfile(line):
                    continue
                parent_dir = os.path.dirname(line)
                if cached_set is None or parent_dir not in cached_set:
                    target_dirs.add(parent_dir)

            return sorted(target_dirs)

        except Exception:
            return self._scan_os_walk(root_dir, cached_set)

    def _scan_os_walk(self, root_dir, cached_set=None):
        target_dirs = []
        try:
            for dirpath, dirnames, filenames in os.walk(root_dir):
                if any(f.endswith('.m4s') for f in filenames):
                    if cached_set is None or dirpath not in cached_set:
                        target_dirs.append(dirpath)
                    dirnames.clear()
            return target_dirs
        except Exception:
            return []

    def get_video_info(self, directory):
        json_path = os.path.join(directory, 'videoInfo.json')
        if not os.path.exists(json_path):
            return None
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f"读取视频信息失败: {e}")
            return None

    def remove_bilibili_header(self, input_path, output_path, log_callback=None):
        chunk_size = 1024 * 1024
        try:
            with open(input_path, 'rb') as infile:
                header = infile.read(9)
                with open(output_path, 'wb') as outfile:
                    if header != b'000000000':
                        outfile.write(header)
                    while True:
                        chunk = infile.read(chunk_size)
                        if not chunk:
                            break
                        outfile.write(chunk)
            return True
        except PermissionError:
            msg = f"权限不足：无法读取或写入文件 - {os.path.basename(input_path)}"
            if log_callback:
                log_callback(msg, "ERROR")
            else:
                print(msg)
            return False
        except FileNotFoundError:
            msg = f"文件不存在：{input_path}"
            if log_callback:
                log_callback(msg, "ERROR")
            else:
                print(msg)
            return False
        except OSError as e:
            if e.errno == 28:
                msg = f"磁盘空间不足：无法写入临时文件"
            else:
                msg = f"文件操作错误：{os.path.basename(input_path)} - {e}"
            if log_callback:
                log_callback(msg, "ERROR")
            else:
                print(msg)
            return False
        except Exception as e:
            msg = f"处理文件失败：{os.path.basename(input_path)} - {type(e).__name__}: {e}"
            if log_callback:
                log_callback(msg, "ERROR")
            else:
                print(msg)
            return False

    def find_m4s_pairs(self, directory):
        pairs = []
        grouped_files = {}
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if not entry.is_file() or not entry.name.endswith('.m4s'):
                        continue
                    match = re.search(r'(?P<prefix>.*?)(?P<id>\d+)-1-(?P<type>30\d{3})\.m4s$', entry.name)
                    if not match:
                        continue
                    group_id = match.group('id')
                    file_type = match.group('type')
                    grouped_files.setdefault(group_id, []).append((file_type, entry.path))
        except (PermissionError, OSError):
            return pairs

        for group_id, file_list in grouped_files.items():
            video_path = audio_path = None
            for file_type, path in file_list:
                if file_type.startswith('300'):
                    video_path = path
                elif file_type.startswith('302'):
                    audio_path = path
            if video_path and audio_path:
                pairs.append((video_path, audio_path, group_id))
        return pairs

    def merge_m4s_to_mp4(self, video_path, audio_path, output_path, log_callback=None):
        if not self.ffmpeg_path:
            msg = "未找到 ffmpeg.exe，请将 ffmpeg.exe 放在程序目录或添加到系统 PATH"
            if log_callback:
                log_callback(msg, "ERROR")
            return False
        
        if not os.path.exists(self.ffmpeg_path):
            msg = f"FFmpeg 路径不存在: {self.ffmpeg_path}"
            if log_callback:
                log_callback(msg, "ERROR")
            return False
        
        if not os.access(self.ffmpeg_path, os.X_OK):
            msg = f"FFmpeg 没有执行权限: {self.ffmpeg_path}"
            if log_callback:
                log_callback(msg, "ERROR")
            return False

        try:
            cmd = [self.ffmpeg_path, '-i', video_path, '-i', audio_path, '-c', 'copy', '-y', output_path]

            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding=locale.getpreferredencoding(False),
                errors='replace',
                startupinfo=startupinfo
            )

            stdout_lines = []
            stderr_lines = []
            while process.poll() is None:
                if self.stop_requested:
                    process.terminate()
                    process.wait()
                    return False
                try:
                    stdout_part, stderr_part = process.communicate(timeout=0.5)
                    if stdout_part:
                        stdout_lines.append(stdout_part)
                    if stderr_part:
                        stderr_lines.append(stderr_part)
                except subprocess.TimeoutExpired:
                    pass
            
            stdout, stderr = process.communicate()
            stdout_lines.append(stdout)
            stderr_lines.append(stderr)
            
            stdout_full = ''.join(stdout_lines)
            stderr_full = ''.join(stderr_lines)
            
            if log_callback:
                if stdout_full.strip():
                    log_callback(f"FFmpeg stdout: {stdout_full.strip()}", "DEBUG")
                if stderr_full.strip():
                    log_callback(f"FFmpeg stderr: {stderr_full.strip()}", "DEBUG")
            
            if process.returncode != 0:
                msg = f"FFmpeg 执行失败 (退出码: {process.returncode})"
                if "Invalid argument" in stderr_full:
                    msg += " - 可能是 FFmpeg 版本不兼容或编码格式不支持"
                elif "Permission denied" in stderr_full:
                    msg += " - 输出目录权限不足"
                elif "No space left on device" in stderr_full:
                    msg += " - 磁盘空间不足"
                elif "could not find codec" in stderr_full.lower():
                    msg += " - FFmpeg 缺少必要的编解码器"
                if log_callback:
                    log_callback(msg, "ERROR")
                return False
            
            if not os.path.exists(output_path):
                msg = f"FFmpeg 执行成功但输出文件不存在"
                if log_callback:
                    log_callback(msg, "ERROR")
                return False
            
            if os.path.getsize(output_path) == 0:
                msg = f"FFmpeg 生成的文件为空: {os.path.basename(output_path)}"
                if log_callback:
                    log_callback(msg, "ERROR")
                try:
                    os.remove(output_path)
                except:
                    pass
                return False
            
            return True

        except FileNotFoundError:
            msg = f"FFmpeg 可执行文件未找到: {self.ffmpeg_path}"
            if log_callback:
                log_callback(msg, "ERROR")
            return False
        except PermissionError:
            msg = f"执行 FFmpeg 权限不足，请以管理员身份运行程序"
            if log_callback:
                log_callback(msg, "ERROR")
            return False
        except OSError as e:
            if e.errno == 28:
                msg = "磁盘空间不足：无法生成输出文件"
            else:
                msg = f"系统错误: {e}"
            if log_callback:
                log_callback(msg, "ERROR")
            return False
        except Exception as e:
            msg = f"FFmpeg 调用异常: {type(e).__name__}: {e}"
            if log_callback:
                log_callback(msg, "ERROR")
            return False

    def _cleanup_temp_files(self, directory, group_id):
        """清理临时文件"""
        video_clean = os.path.join(directory, f"video_clean_{group_id}.m4s")
        audio_clean = os.path.join(directory, f"audio_clean_{group_id}.m4s")
        for f in [video_clean, audio_clean]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except PermissionError:
                    print(f"权限不足：无法删除临时文件 {f}")
                except OSError as e:
                    if e.errno == 32:
                        print(f"文件被占用：无法删除临时文件 {f}")
                    else:
                        print(f"删除临时文件失败: {f} - {e}")
                except Exception as e:
                    print(f"删除临时文件失败: {f} - {type(e).__name__}: {e}")

    def process_single_directory(self, directory, log_callback=None):
        def log(msg, level="INFO"):
            if log_callback:
                log_callback(msg, level)
            else:
                print(msg)

        if self.stop_requested:
            log("用户已停止处理", "WARNING")
            return False

        log(f"开始处理目录: {directory}", "DEBUG")

        if not os.path.exists(directory):
            log(f"目录不存在: {directory}", "ERROR")
            return False
        
        if not os.path.isdir(directory):
            log(f"路径不是目录: {directory}", "ERROR")
            return False
        
        try:
            os.listdir(directory)
        except PermissionError:
            log(f"权限不足：无法访问目录 {directory}", "ERROR")
            return False
        except OSError as e:
            log(f"访问目录失败: {directory} - {e}", "ERROR")
            return False

        pairs = self.find_m4s_pairs(directory)
        if not pairs:
            m4s_files = []
            try:
                m4s_files = [f for f in os.listdir(directory) if f.endswith('.m4s')]
            except (PermissionError, OSError):
                log(f"无法读取目录内容: {directory}", "ERROR")
                return False
                
            if not m4s_files:
                log(f"目录中未找到 m4s 文件: {directory}", "WARNING")
                try:
                    log(f"扫描目录内容: {os.listdir(directory)}", "DEBUG")
                except:
                    pass
            else:
                log(f"目录中存在 m4s 文件但未找到配对: {directory}", "WARNING")
                log(f"m4s 文件列表: {m4s_files}", "DEBUG")
            return False

        log(f"找到 {len(pairs)} 对音视频文件", "DEBUG")
        for i, (vp, ap, gid) in enumerate(pairs):
            log(f"  配对 {i+1}: video={os.path.basename(vp)}, audio={os.path.basename(ap)}, ID={gid}", "DEBUG")

        info = self.get_video_info(directory)
        title = info.get('title') if info else None
        owner = info.get('uname') if info else None
        safe_title = re.sub(r'[<>:"/\\|?*]', '', title).strip() if title else "未知标题"
        safe_owner = re.sub(r'[<>:"/\\|?*]', '', owner).strip() if owner else "未知UP主"
        dir_name = os.path.basename(directory)

        log(f"视频标题: {title} (安全: {safe_title})", "DEBUG")
        log(f"UP主: {owner} (安全: {safe_owner})", "DEBUG")

        final_name = self.naming_template.replace('[UP]', safe_owner).replace('[title]', safe_title)
        if not final_name.strip():
            final_name = dir_name
        final_name += ".mp4"
        log(f"最终文件名: {final_name}", "DEBUG")

        if self.output_base_dir:
            output_dir = self.output_base_dir
            try:
                os.makedirs(output_dir, exist_ok=True)
                log(f"输出目录: {output_dir}", "DEBUG")
            except PermissionError:
                log(f"权限不足：无法创建输出目录 {output_dir}", "ERROR")
                return False
            except OSError as e:
                if e.errno == 28:
                    log(f"磁盘空间不足：无法创建输出目录", "ERROR")
                else:
                    log(f"创建输出目录失败: {output_dir} - {e}", "ERROR")
                return False
        else:
            output_dir = directory
        output_path = os.path.join(output_dir, final_name)

        if os.path.exists(output_path):
            if self.duplicate_handling == "跳过":
                log(f"文件已存在，跳过: {final_name}", "WARNING")
                return "skipped"
            elif self.duplicate_handling == "保留两者":
                base, ext = os.path.splitext(output_path)
                counter = 1
                while os.path.exists(f"{base}_{counter}{ext}"):
                    counter += 1
                output_path = f"{base}_{counter}{ext}"
                log(f"文件已存在，重命名为: {os.path.basename(output_path)}", "INFO")
            else:
                log(f"文件已存在，将覆盖: {final_name}", "INFO")

        success = False
        for idx, (video_path, audio_path, group_id) in enumerate(pairs):
            if self.stop_requested:
                log("用户已停止处理", "WARNING")
                self._cleanup_temp_files(directory, group_id)
                return False

            if len(pairs) > 1:
                base, ext = os.path.splitext(final_name)
                pair_output_path = os.path.join(output_dir, f"{base}_{idx+1}{ext}")
            else:
                pair_output_path = output_path

            video_clean = os.path.join(directory, f"video_clean_{group_id}.m4s")
            audio_clean = os.path.join(directory, f"audio_clean_{group_id}.m4s")
            
            log(f"处理配对 {idx+1}/{len(pairs)}: video={os.path.basename(video_path)}, audio={os.path.basename(audio_path)}", "DEBUG")
            try:
                log(f"  视频文件大小: {os.path.getsize(video_path)} 字节", "DEBUG")
            except (OSError, FileNotFoundError):
                log(f"  视频文件不存在或无法访问", "WARNING")
            try:
                log(f"  音频文件大小: {os.path.getsize(audio_path)} 字节", "DEBUG")
            except (OSError, FileNotFoundError):
                log(f"  音频文件不存在或无法访问", "WARNING")

            try:
                log(f"  去除B站头部: {os.path.basename(video_path)} → {os.path.basename(video_clean)}", "DEBUG")
                if not self.remove_bilibili_header(video_path, video_clean, log_callback):
                    log(f"  去除视频头部失败，跳过此配对", "ERROR")
                    self._cleanup_temp_files(directory, group_id)
                    continue
                
                log(f"  去除B站头部: {os.path.basename(audio_path)} → {os.path.basename(audio_clean)}", "DEBUG")
                if not self.remove_bilibili_header(audio_path, audio_clean, log_callback):
                    log(f"  去除音频头部失败，跳过此配对", "ERROR")
                    self._cleanup_temp_files(directory, group_id)
                    continue
                
                log(f"  FFmpeg 合并中...", "DEBUG")
                if self.merge_m4s_to_mp4(video_clean, audio_clean, pair_output_path, log_callback):
                    self._cleanup_temp_files(directory, group_id)
                    log(f"合并成功: {os.path.basename(pair_output_path)}", "INFO")
                    success = True
                else:
                    log(f"合并失败: {os.path.basename(pair_output_path)}", "ERROR")
                    log(f"  保留临时文件: {os.path.basename(video_clean)}, {os.path.basename(audio_clean)}", "DEBUG")
            except Exception as e:
                log(f"处理出错: {e}", "ERROR")
                log(f"  异常详情: {type(e).__name__}: {e}", "DEBUG")
                self._cleanup_temp_files(directory, group_id)

        if success and self.delete_original:
            try:
                shutil.rmtree(directory)
                log(f"已删除原始缓存目录: {os.path.basename(directory)}", "INFO")
                log(f"  删除路径: {directory}", "DEBUG")
            except PermissionError:
                log(f"权限不足：无法删除目录 {directory}，请以管理员身份运行程序", "WARNING")
            except FileNotFoundError:
                log(f"目录不存在：{directory}，可能已被其他程序删除", "WARNING")
            except OSError as e:
                if e.errno == 32:
                    log(f"文件被占用：无法删除目录 {directory}，请关闭其他正在访问该目录的程序", "WARNING")
                else:
                    log(f"删除缓存目录失败: {directory} - {e}", "WARNING")
            except Exception as e:
                log(f"删除缓存目录失败: {type(e).__name__}: {e}", "WARNING")

        return success

    def process_selected(self, selected_dirs, output_dir, log_callback, progress_callback=None, skipped_callback=None):
        self.stop_requested = False
        self.output_base_dir = output_dir
        success = 0
        skipped = 0
        failed = 0
        total = len(selected_dirs)
        processed = 0
        for i, d in enumerate(selected_dirs, 1):
            if self.stop_requested:
                log_callback("用户已停止处理", "WARNING")
                break
            log_callback(f"[{i}/{total}] 处理中...", "INFO")
            result = self.process_single_directory(d, log_callback)
            processed += 1
            if result == "skipped":
                skipped += 1
                if skipped_callback:
                    skipped_callback(skipped)
            elif result:
                success += 1
                if progress_callback:
                    progress_callback(success, total)
            else:
                failed += 1

        unprocessed = total - processed
        return success, skipped, failed, unprocessed


# ==================== GUI ====================
class BiliConverterGUI:
    def __init__(self, root, system_scale=1.0):
        self.root = root
        self.system_scale = system_scale
        root.title("B站缓存转MP4")

        self.config = load_config()
        
        self.ui_scale = tk.IntVar(value=self.config.get("ui_scale", 100))
        self.scale = self.system_scale * (self.ui_scale.get() / 100.0)

        base_width, base_height = 810, 390
        root.geometry(f"{int(base_width * self.scale)}x{int(base_height * self.scale)}")
        root.resizable(False, False)

        self.FONT_FAMILY = self._get_available_font(root, ("KaiTi", "Microsoft YaHei", "SimSun"))

        self.input_dir = tk.StringVar(value="")
        self.output_dir = tk.StringVar(value="")
        
        self.default_input_dir = tk.StringVar(value=self.config.get("default_input_dir", ""))
        self.default_output_dir = tk.StringVar(value=self.config.get("default_output_dir", ""))
        self.naming_template = tk.StringVar(value=self.config.get("naming_template", "[title]_[UP]"))
        self.delete_original = tk.BooleanVar(value=self.config.get("delete_original", False))
        self.duplicate_handling = tk.StringVar(value=self.config.get("duplicate_handling", "覆盖"))
        self.log_level = tk.StringVar(value=self.config.get("log_level", "INFO"))
        self.log_enabled = tk.BooleanVar(value=self.config.get("log_enabled", True))
        self.open_output_dir = tk.BooleanVar(value=self.config.get("open_output_dir", False))

        self._apply_saved_values_to_ui()

        self.file_vars = []
        self.file_paths = []
        self.converter = None
        self.is_running = False
        self.settings_window = None
        self._log_batch = []
        self._log_scheduled = False
        self._log_lock = threading.Lock()
        self._max_log_lines = 5000

        self._build_left_frame()
        self._build_mid_top_frame()
        self._build_right_top_frame()
        self._build_mid_bottom_frame()
        self._build_right_bottom_frame()
        self._build_bottom_frame()
        self._setup_grid_weights()

        root.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.root.after_idle(self._preload_settings)

    def _rebuild_ui(self):
        old_input = self.input_dir.get()
        old_output = self.output_dir.get()
        old_log_text = ""
        if hasattr(self, 'log_text'):
            self.log_text.config(state="normal")
            old_log_text = self.log_text.get(1.0, tk.END)
            self.log_text.config(state="disabled")

        for widget in self.root.winfo_children():
            widget.destroy()

        self.scale = self.system_scale * (self.ui_scale.get() / 100.0)

        base_width, base_height = 810, 390
        self.root.geometry(f"{int(base_width * self.scale)}x{int(base_height * self.scale)}")

        self._build_left_frame()
        self._build_mid_top_frame()
        self._build_right_top_frame()
        self._build_mid_bottom_frame()
        self._build_right_bottom_frame()
        self._build_bottom_frame()
        self._setup_grid_weights()

        self.input_dir.set(old_input)
        self.output_dir.set(old_output)

        if old_log_text:
            self.log_text.config(state="normal")
            self.log_text.insert(1.0, old_log_text)
            self.log_text.config(state="disabled")

        self.settings_window = None
        self.root.after_idle(self._preload_settings)

    # ==================== 布局构建方法 ====================

    def _get_available_font(self, root, font_candidates):
        available = tkfont.families(root)
        for font in font_candidates:
            if font in available:
                return font
        return ""

    def _build_left_frame(self):
        scale = self.scale
        left_frame = tk.Frame(self.root, width=int(300 * scale))
        left_frame.grid(row=0, column=0, rowspan=3, padx=3, pady=3, sticky="nsew")
        left_frame.grid_propagate(False)
        self.left_frame = left_frame

        tk.Label(left_frame, text="文件列表", font=(self.FONT_FAMILY, int(13 * scale), "bold")).pack(anchor="w", pady=(0, 2))

        list_container = tk.Frame(left_frame)
        list_container.pack(fill="both", expand=True)

        h_scrollbar = tk.Scrollbar(list_container, orient="horizontal")
        v_scrollbar = tk.Scrollbar(list_container, orient="vertical")

        self.list_canvas = tk.Canvas(
            list_container,
            highlightthickness=0,
            xscrollcommand=h_scrollbar.set,
            yscrollcommand=v_scrollbar.set
        )

        h_scrollbar.config(command=self.list_canvas.xview)
        v_scrollbar.config(command=self.list_canvas.yview)

        self.list_inner = tk.Frame(self.list_canvas)
        self.list_canvas.create_window((0, 0), window=self.list_inner, anchor="nw")

        self.list_canvas.grid(row=0, column=0, sticky="nsew")
        v_scrollbar.grid(row=0, column=1, sticky="ns")
        h_scrollbar.grid(row=1, column=0, sticky="ew")

        list_container.grid_rowconfigure(0, weight=1)
        list_container.grid_columnconfigure(0, weight=1)

        def update_scrollregion(event=None):
            self.list_canvas.configure(scrollregion=self.list_canvas.bbox("all"))

        self.list_inner.bind("<Configure>", update_scrollregion)
        self.list_canvas.bind("<Configure>", update_scrollregion)
        
        def _on_mousewheel(event):
            self.list_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        
        self.list_canvas.bind("<MouseWheel>", _on_mousewheel)

    def _build_mid_top_frame(self):
        scale = self.scale
        mid_top_frame = tk.Frame(self.root)
        mid_top_frame.grid(row=0, column=1, padx=3, pady=3, sticky="nsew")
        self.mid_top_frame = mid_top_frame

        self.btn_settings = tk.Button(
            mid_top_frame,
            text="⚙️ 设置",
            command=self.open_settings,
            width=10,
            height=1,
            font=(self.FONT_FAMILY, int(11 * scale))
        )
        self.btn_settings.pack(expand=True)
        self.btn_settings.config(bg="#d0d0d0", activebackground="#b0b0b0")

    def _build_right_top_frame(self):
        scale = self.scale
        right_top_frame = tk.Frame(self.root)
        right_top_frame.grid(row=0, column=2, padx=int(15 * scale), pady=3, sticky="nsew")
        self.right_top_frame = right_top_frame

        right_top_frame.grid_rowconfigure(0, weight=0)
        right_top_frame.grid_rowconfigure(1, weight=0)
        right_top_frame.grid_columnconfigure(0, weight=0)
        right_top_frame.grid_columnconfigure(1, weight=1)
        right_top_frame.grid_columnconfigure(2, weight=0)

        tk.Label(right_top_frame, text="输入：", font=(self.FONT_FAMILY, int(11 * scale))).grid(
            row=0, column=0, padx=1, pady=1, sticky="e"
        )
        entry_in = tk.Entry(right_top_frame, textvariable=self.input_dir, font=(self.FONT_FAMILY, int(12 * scale)))
        entry_in.grid(row=0, column=1, padx=1, pady=1, sticky="ew", ipady=2)
        tk.Button(right_top_frame, text="浏览", command=self.browse_input, width=6, height=0, font=(self.FONT_FAMILY, int(10 * scale))).grid(
            row=0, column=2, padx=1, pady=1
        )

        tk.Label(right_top_frame, text="输出：", font=(self.FONT_FAMILY, int(11 * scale))).grid(
            row=1, column=0, padx=1, pady=1, sticky="e"
        )
        entry_out = tk.Entry(right_top_frame, textvariable=self.output_dir, font=(self.FONT_FAMILY, int(12 * scale)))
        entry_out.grid(row=1, column=1, padx=1, pady=1, sticky="ew", ipady=2)
        tk.Button(right_top_frame, text="浏览", command=self.browse_output, width=6, height=0, font=(self.FONT_FAMILY, int(10 * scale))).grid(
            row=1, column=2, padx=1, pady=1
        )

    def _build_mid_bottom_frame(self):
        scale = self.scale
        mid_bottom_frame = tk.Frame(self.root)
        mid_bottom_frame.grid(row=1, column=1, padx=3, pady=3, sticky="nsew")
        self.mid_bottom_frame = mid_bottom_frame

        self.btn_search = tk.Button(
            mid_bottom_frame,
            text="🔍 搜索",
            command=self.scan_files,
            width=10,
            height=1,
            font=(self.FONT_FAMILY, int(11 * scale))
        )
        self.btn_search.pack(pady=4)
        self.btn_search.config(bg="#4a90d9", fg="white", activebackground="#3a7bc8", activeforeground="white")

        self.btn_start = tk.Button(
            mid_bottom_frame,
            text="🚀 开始处理",
            command=self.start_processing,
            width=10,
            height=1,
            font=(self.FONT_FAMILY, int(11 * scale))
        )
        self.btn_start.pack(pady=4)
        self.btn_start.config(bg="#2ecc71", fg="white", activebackground="#27ae60", activeforeground="white")

        self.btn_stop = tk.Button(
            mid_bottom_frame,
            text="🛑 停止处理",
            command=self.stop_processing,
            width=10,
            height=1,
            font=(self.FONT_FAMILY, int(11 * scale)),
            state="disabled",
            fg="white"
        )
        self.btn_stop.pack(pady=4)
        self.btn_stop.config(bg="#e06060", activebackground="#cc5555", activeforeground="white")
        
        self.total_label = tk.Label(
            mid_bottom_frame,
            text="视频总数：0",
            font=(self.FONT_FAMILY, int(11 * scale)),
            fg="#555555"
        )
        self.total_label.pack(pady=1)
        
        self.completed_label = tk.Label(
            mid_bottom_frame,
            text="已处理数：0",
            font=(self.FONT_FAMILY, int(11 * scale)),
            fg="#555555"
        )
        self.completed_label.pack(pady=1)
        
        self.skipped_label = tk.Label(
            mid_bottom_frame,
            text="跳过数：0",
            font=(self.FONT_FAMILY, int(11 * scale)),
            fg="#555555"
        )
        self.skipped_label.pack(pady=1)
        
    def _build_right_bottom_frame(self):
        scale = self.scale
        right_bottom_frame = tk.Frame(self.root, width=int(400 * scale))
        right_bottom_frame.grid(row=1, column=2, rowspan=2, padx=int(15 * scale), pady=3, sticky="nsew")
        right_bottom_frame.grid_propagate(False)
        self.right_bottom_frame = right_bottom_frame

        right_bottom_frame.grid_rowconfigure(0, weight=0)
        right_bottom_frame.grid_rowconfigure(1, weight=1)
        right_bottom_frame.grid_columnconfigure(0, weight=1)

        log_header_frame = tk.Frame(right_bottom_frame)
        log_header_frame.grid(row=0, column=0, sticky="w")

        tk.Label(log_header_frame, text="日志", font=(self.FONT_FAMILY, int(11 * scale), "bold")).pack(side=tk.LEFT)

        tk.Label(log_header_frame, text="等级：", font=(self.FONT_FAMILY, int(11 * scale))).pack(side=tk.LEFT, padx=(int(15 * scale), 2))
        level_combo = tk.OptionMenu(
            log_header_frame,
            self.log_level,
            *["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        )
        level_combo.config(
            font=(self.FONT_FAMILY, int(11 * scale)),
            indicatoron=True,
            width=8
        )
        level_combo.pack(side=tk.LEFT)
        
        menu = level_combo.nametowidget(level_combo.cget("menu"))
        menu.config(font=(self.FONT_FAMILY, int(13 * scale)))

        self.log_enabled_cb = tk.Checkbutton(
            log_header_frame,
            text="启用日志",
            variable=self.log_enabled,
            font=(self.FONT_FAMILY, int(11 * scale))
        )
        self.log_enabled_cb.pack(side=tk.LEFT, padx=(int(10 * scale), 0))

        self.log_text = scrolledtext.ScrolledText(right_bottom_frame, state="disabled", font=(self.FONT_FAMILY, int(11 * scale)))
        self.log_text.grid(row=1, column=0, sticky="nsew")

    def _build_bottom_frame(self):
        scale = self.scale
        bottom_frame = tk.Frame(self.root)
        bottom_frame.grid(row=3, column=0, columnspan=3, padx=3, pady=3, sticky="ew")
        self.bottom_frame = bottom_frame

        self.select_all_var = tk.IntVar(value=0)
        self.select_all_cb = tk.Checkbutton(
            bottom_frame,
            text="全选",
            variable=self.select_all_var,
            command=self.toggle_all,
            state="disabled",
            font=(self.FONT_FAMILY, int(12 * scale))
        )
        self.select_all_cb.pack(side=tk.LEFT)

    def _setup_grid_weights(self):
        root = self.root
        scale = self.scale

        root.grid_rowconfigure(0, weight=0)
        root.grid_rowconfigure(1, weight=1)
        root.grid_rowconfigure(2, weight=0)
        root.grid_rowconfigure(3, weight=0)

        root.grid_columnconfigure(0, weight=0, minsize=int(300 * scale))
        root.grid_columnconfigure(1, weight=0)
        root.grid_columnconfigure(2, weight=0, minsize=int(400 * scale))

    def _apply_saved_values_to_ui(self):
        if not self.input_dir.get():
            self.input_dir.set(self.default_input_dir.get())
        if not self.output_dir.get():
            self.output_dir.set(self.default_output_dir.get())

    def _persist_config(self):
        self.config["default_input_dir"] = self.default_input_dir.get()
        self.config["default_output_dir"] = self.default_output_dir.get()
        self.config["naming_template"] = self.naming_template.get()
        self.config["delete_original"] = self.delete_original.get()
        self.config["duplicate_handling"] = self.duplicate_handling.get()
        self.config["log_level"] = self.log_level.get()
        self.config["log_enabled"] = self.log_enabled.get()
        self.config["open_output_dir"] = self.open_output_dir.get()
        self.config["ui_scale"] = self.ui_scale.get()
        save_config(self.config)

    def _set_processing_state(self, is_running):
        self.is_running = is_running
        self.btn_start.config(state="normal" if not is_running else "disabled")
        self.btn_stop.config(state="normal" if is_running else "disabled")
        self.root.config(cursor="watch" if is_running else "")

    def _update_status_labels(self, completed=None, skipped=None):
        if completed is not None:
            self.completed_label.config(text=f"已处理数：{completed}")
        if skipped is not None:
            self.skipped_label.config(text=f"跳过数：{skipped}")

    # ==================== 事件处理 ====================

    def browse_input(self):
        d = filedialog.askdirectory()
        if d:
            self.input_dir.set(d)

    def browse_output(self):
        d = filedialog.askdirectory()
        if d:
            self.output_dir.set(d)

    def browse_setting_dir(self, var):
        d = filedialog.askdirectory()
        if d:
            var.set(d)

    def toggle_all(self):
        v = self.select_all_var.get()
        for var in self.file_vars:
            var.set(v)
        if self.file_vars:
            canvas = self.list_canvas
            for idx in range(len(self.file_vars)):
                self._update_checkbox(canvas, idx, v)

    def _update_select_all_state(self):
        if not self.file_vars:
            return
        selected_count = sum(var.get() for var in self.file_vars)
        if selected_count == len(self.file_vars):
            self.select_all_var.set(1)
        else:
            self.select_all_var.set(0)

    # ==================== 日志系统 ====================

    def log(self, msg, level="INFO"):
        level_order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        current_level = self.log_level.get()
        if current_level in level_order:
            if level_order.index(level) < level_order.index(current_level):
                return
        formatted_msg = f"[{level}] {msg}\n"
        with self._log_lock:
            self._log_batch.append(formatted_msg)
            if not self._log_scheduled:
                self._log_scheduled = True
                self.root.after(50, self._flush_log_batch)

    def _flush_log_batch(self):
        with self._log_lock:
            if not self._log_batch:
                self._log_scheduled = False
                return
            batch_copy = list(self._log_batch)
            self._log_batch.clear()
            self._log_scheduled = False
        if not self.log_enabled.get():
            return
        self.log_text.config(state="normal")
        self.log_text.insert(tk.END, "".join(batch_copy))
        line_count = int(self.log_text.index('end-1c').split('.')[0])
        if line_count > self._max_log_lines:
            remove_count = line_count - self._max_log_lines
            self.log_text.delete(f"1.0", f"{remove_count+1}.0")
        self.log_text.see(tk.END)
        self.log_text.config(state="disabled")

    def log_debug(self, msg):
        self.log(msg, "DEBUG")

    def log_info(self, msg):
        self.log(msg, "INFO")

    def log_warning(self, msg):
        self.log(msg, "WARNING")

    def log_error(self, msg):
        self.log(msg, "ERROR")

    def log_critical(self, msg):
        self.log(msg, "CRITICAL")

    # ==================== 扫描逻辑 ====================

    def scan_files(self):
        if self.is_running:
            self.log_warning("正在处理中，请等待完成")
            return

        in_path = self.input_dir.get().strip()
        if not in_path or not os.path.isdir(in_path):
            self.log_error("输入目录无效")
            return

        self.btn_search.config(state="disabled", text="扫描中...")
        
        def scan_worker():
            try:
                self.converter = BilibiliCacheConverter(in_path)
                
                if not self.converter.ffmpeg_path:
                    self.root.after(0, lambda: self._scan_failed("未找到 ffmpeg.exe，请将 ffmpeg.exe 放在程序目录或添加到系统 PATH"))
                    return

                self.converter.naming_template = self.naming_template.get()
                self.converter.duplicate_handling = self.duplicate_handling.get()
                self.converter.delete_original = self.delete_original.get()
                
                cached_info = self.config.get("cached_dirs", [])
                
                cached_dict = {}
                for item in cached_info:
                    if isinstance(item, dict) and 'path' in item and os.path.isdir(item['path']):
                        cached_dict[item['path']] = {
                            'title': item.get('title'),
                            'uname': item.get('uname')
                        }
                    elif isinstance(item, str) and os.path.isdir(item):
                        cached_dict[item] = {'title': None, 'uname': None}
                
                valid_cached_paths = list(cached_dict.keys())
                
                if valid_cached_paths:
                    new_dirs = self.converter.scan_cache_directories_incremental(in_path, set(valid_cached_paths))
                    dirs = valid_cached_paths + new_dirs
                    if new_dirs:
                        self.root.after(0, lambda: self.log_info(f"新增: {len(new_dirs)} 个目录"))
                else:
                    dirs = self.converter.scan_cache_directories()
                
                deleted_count = len(cached_info) - len(valid_cached_paths)
                if deleted_count > 0:
                    self.root.after(0, lambda: self.log_info(f"已删除: {deleted_count} 个目录"))

                results = []
                cached_new = []
                for d in dirs:
                    cached_data = cached_dict.get(d)
                    if cached_data and cached_data.get('title'):
                        title = cached_data['title']
                        owner = cached_data.get('uname')
                    else:
                        info = self.converter.get_video_info(d)
                        title = info.get('title') if info else None
                        owner = info.get('uname') if info else None
                    
                    text = os.path.basename(d)
                    if title:
                        text += f"  — {title[:30]}"
                    if owner:
                        text += f"  (UP: {owner})"
                    results.append((d, text))
                    
                    cached_new.append({
                        'path': d,
                        'title': title,
                        'uname': owner
                    })

                self.config["cached_dirs"] = cached_new
                save_config(self.config)

                self.root.after(0, lambda: self._scan_complete(results))
            except Exception as e:
                self.root.after(0, lambda: self._scan_failed(f"扫描出错: {e}"))

        self.root.after(0, lambda: self._scan_start(in_path))
        threading.Thread(target=scan_worker, daemon=True).start()

    def _scan_start(self, in_path):
        self.log_text.config(state="normal")
        self.log_text.delete(1.0, tk.END)
        self.log_text.config(state="disabled")

        self.list_canvas.delete("all")
        self.file_vars.clear()
        self.file_paths.clear()
        self.select_all_var.set(0)
        self.select_all_cb.config(state="disabled")
        self.log_info(f"扫描: {in_path}")

    def _scan_complete(self, results):
        if not results:
            self.log_warning("未找到缓存目录")
        else:
            self.log_info(f"找到 {len(results)} 个缓存目录")
            self._build_list_with_canvas(results)
            self.log_info("请勾选后点击「开始处理」")
            self.select_all_cb.config(state="normal")
        
        self.btn_search.config(state="normal", text="🔍 搜索")

    def _build_list_with_canvas(self, results):
        scale = self.scale
        item_height = int(28 * scale)
        check_size = int(16 * scale)
        padding = int(5 * scale)
        
        canvas = self.list_canvas
        canvas.delete("all")
        
        self.file_vars = []
        self.file_paths = []
        
        canvas_width = canvas.winfo_width()
        if canvas_width < 100:
            canvas_width = 500
        
        self._build_list_index = 0
        self._build_list_results = results
        self._build_list_item_height = item_height
        self._build_list_check_size = check_size
        self._build_list_padding = padding
        self._build_list_canvas_width = canvas_width
        
        def build_batch():
            if self._build_list_index >= len(self._build_list_results):
                canvas.config(scrollregion=(0, 0, canvas_width, len(self._build_list_results) * item_height))
                return
            
            batch_size = 20
            end_idx = min(self._build_list_index + batch_size, len(self._build_list_results))
            
            for i in range(self._build_list_index, end_idx):
                d, text = self._build_list_results[i]
                var = tk.IntVar(value=0)
                self.file_vars.append(var)
                self.file_paths.append(d)
                
                y = i * item_height
                bg_color = "#f0f0f0" if i % 2 == 0 else "#ffffff"
                
                canvas.create_rectangle(0, y, canvas_width, y + item_height, 
                                       fill=bg_color, outline="", tags=f"row_{i}")
                
                check_x = padding
                check_y = y + (item_height - check_size) // 2
                
                canvas.create_rectangle(
                    check_x, check_y, check_x + check_size, check_y + check_size,
                    outline="#999999", width=2, tags=f"check_{i}"
                )
                
                text_x = check_x + check_size + padding
                text_y = y + item_height // 2
                
                canvas.create_text(
                    text_x, text_y, text=text, anchor="w",
                    font=(self.FONT_FAMILY, int(12 * scale)), fill="#333333",
                    tags=f"text_{i}"
                )
            
            self._build_list_index = end_idx
            self.root.after(0, build_batch)
        
        canvas.tag_bind("all", "<Button-1>", self._on_canvas_click)
        build_batch()
    
    def _on_canvas_click(self, event):
        canvas = self.list_canvas
        y = canvas.canvasy(event.y) if hasattr(event, 'y') else 0
        idx = int(y // self._build_list_item_height)
        
        if idx < 0 or idx >= len(self.file_vars):
            return
        
        try:
            current = self.file_vars[idx].get()
            new_val = 1 - current
            self.file_vars[idx].set(new_val)
            self._update_checkbox(canvas, idx, new_val)
            self._update_select_all_state()
        except Exception as e:
            print(f"点击事件出错: {e}")

    def _update_checkbox(self, canvas, idx, value):
        scale = self.scale
        check_size = int(16 * scale)
        padding = int(5 * scale)
        item_height = int(28 * scale)
        
        y = idx * item_height
        check_x = padding
        check_y = y + (item_height - check_size) // 2
        
        if value:
            canvas.itemconfig(f"check_{idx}", fill="#4a90d9", outline="#4a90d9")
            canvas.create_line(
                check_x + 3, check_y + check_size//2,
                check_x + check_size//3, check_y + check_size - 3,
                check_x + check_size - 3, check_y + 3,
                fill="white", width=2, tags=f"check_mark_{idx}"
            )
        else:
            canvas.itemconfig(f"check_{idx}", fill="", outline="#999999")
            canvas.delete(f"check_mark_{idx}")

    def _scan_failed(self, msg):
        self.log_error(msg)
        if "ffmpeg" in msg:
            messagebox.showerror("错误", msg)
        self.btn_search.config(state="normal", text="🔍 搜索")

    # ==================== 设置窗口 ====================

    def _on_scale_change(self, value):
        self.scale_label.config(text=f"{value}%")

    def _preload_settings(self):
        if self.settings_window is None:
            self._create_settings_window()
            self.settings_window.withdraw()

    def open_settings(self):
        if self.settings_window is None:
            self._create_settings_window()
        self.settings_window.deiconify()
        self.settings_window.lift()
        self.settings_window.focus_set()

    def _create_settings_window(self):
        scale = self.scale
        win = tk.Toplevel(self.root)
        win.title("设置")
        win.geometry(f"{int(550 * scale)}x{int(450 * scale)}")
        win.transient(self.root)
        win.resizable(False, False)
        self.settings_window = win

        row = 0

        tk.Label(win, text="📁 路径设置", font=(self.FONT_FAMILY, int(14 * scale), "bold")).grid(
            row=row, column=0, columnspan=3, padx=int(10 * scale), pady=(int(10 * scale), int(5 * scale)), sticky="w"
        )
        row += 1

        tk.Label(win, text="输入目录：", font=(self.FONT_FAMILY, int(12 * scale))).grid(
            row=row, column=0, padx=int(8 * scale), pady=int(4 * scale), sticky="e"
        )
        entry_default_in = tk.Entry(win, textvariable=self.default_input_dir, width=35, font=(self.FONT_FAMILY, int(12 * scale)))
        entry_default_in.grid(row=row, column=1, padx=int(4 * scale), pady=int(4 * scale), sticky="ew")
        tk.Button(
            win, text="浏览", command=lambda: self.browse_setting_dir(self.default_input_dir),
            width=5, height=0, font=(self.FONT_FAMILY, int(10 * scale))
        ).grid(row=row, column=2, padx=int(4 * scale), pady=int(4 * scale))
        row += 1

        tk.Label(win, text="输出目录：", font=(self.FONT_FAMILY, int(12 * scale))).grid(
            row=row, column=0, padx=int(8 * scale), pady=int(4 * scale), sticky="e"
        )
        entry_default_out = tk.Entry(win, textvariable=self.default_output_dir, width=35, font=(self.FONT_FAMILY, int(12 * scale)))
        entry_default_out.grid(row=row, column=1, padx=int(4 * scale), pady=int(4 * scale), sticky="ew")
        tk.Button(
            win, text="浏览", command=lambda: self.browse_setting_dir(self.default_output_dir),
            width=5, height=0, font=(self.FONT_FAMILY, int(10 * scale))
        ).grid(row=row, column=2, padx=int(4 * scale), pady=int(4 * scale))
        row += 2

        tk.Label(win, text="📝 文件处理", font=(self.FONT_FAMILY, int(14 * scale), "bold")).grid(
            row=row, column=0, columnspan=3, padx=int(10 * scale), pady=(int(10 * scale), int(5 * scale)), sticky="w"
        )
        row += 1

        tk.Label(win, text="命名模板：", font=(self.FONT_FAMILY, int(12 * scale))).grid(
            row=row, column=0, padx=int(8 * scale), pady=int(4 * scale), sticky="e"
        )
        entry = tk.Entry(win, textvariable=self.naming_template, width=35, font=(self.FONT_FAMILY, int(12 * scale)))
        entry.grid(row=row, column=1, columnspan=2, padx=int(4 * scale), pady=int(4 * scale), sticky="ew")
        row += 1

        tk.Label(win, text="[UP]=UP主名  [title]=视频标题", font=(self.FONT_FAMILY, int(10 * scale)), fg="gray").grid(
            row=row, column=1, columnspan=2, padx=int(4 * scale), sticky="w"
        )
        row += 1

        tk.Label(win, text="重复文件：", font=(self.FONT_FAMILY, int(12 * scale))).grid(
            row=row, column=0, padx=int(8 * scale), pady=int(4 * scale), sticky="e"
        )
        radio_frame = tk.Frame(win)
        radio_frame.grid(row=row, column=1, columnspan=2, padx=int(4 * scale), pady=int(4 * scale), sticky="w")

        tk.Radiobutton(
            radio_frame,
            text="覆盖",
            variable=self.duplicate_handling,
            value="覆盖",
            font=(self.FONT_FAMILY, int(13 * scale))
        ).pack(side=tk.LEFT, padx=int(12 * scale))

        tk.Radiobutton(
            radio_frame,
            text="跳过",
            variable=self.duplicate_handling,
            value="跳过",
            font=(self.FONT_FAMILY, int(13 * scale))
        ).pack(side=tk.LEFT, padx=int(12 * scale))

        tk.Radiobutton(
            radio_frame,
            text="保留两者",
            variable=self.duplicate_handling,
            value="保留两者",
            font=(self.FONT_FAMILY, int(13 * scale))
        ).pack(side=tk.LEFT, padx=int(12 * scale))
        row += 2

        tk.Label(win, text="⚙️ 操作选项", font=(self.FONT_FAMILY, int(14 * scale), "bold")).grid(
            row=row, column=0, columnspan=3, padx=int(10 * scale), pady=(int(10 * scale), int(5 * scale)), sticky="w"
        )
        row += 1

        cb = tk.Checkbutton(
            win,
            text="转换后删除原始缓存",
            variable=self.delete_original,
            font=(self.FONT_FAMILY, int(13 * scale))
        )
        cb.grid(row=row, column=1, columnspan=2, padx=int(4 * scale), pady=int(4 * scale), sticky="w")
        row += 1

        open_cb = tk.Checkbutton(
            win,
            text="转换完成后打开输出目录",
            variable=self.open_output_dir,
            font=(self.FONT_FAMILY, int(13 * scale))
        )
        open_cb.grid(row=row, column=1, columnspan=2, padx=int(4 * scale), pady=int(4 * scale), sticky="w")
        row += 2

        tk.Label(win, text="🖥️ 界面设置", font=(self.FONT_FAMILY, int(14 * scale), "bold")).grid(
            row=row, column=0, columnspan=3, padx=int(10 * scale), pady=(int(10 * scale), int(5 * scale)), sticky="w"
        )
        row += 1

        tk.Label(win, text="界面缩放：", font=(self.FONT_FAMILY, int(12 * scale))).grid(
            row=row, column=0, padx=int(8 * scale), pady=int(4 * scale), sticky="e"
        )
        
        scale_frame = tk.Frame(win)
        scale_frame.grid(row=row, column=1, columnspan=2, padx=int(4 * scale), pady=int(4 * scale), sticky="ew")
        
        scale_slider = tk.Scale(
            scale_frame,
            from_=25,
            to=200,
            variable=self.ui_scale,
            orient=tk.HORIZONTAL,
            length=int(280 * scale),
            width=int(22 * scale),
            showvalue=0,
            font=(self.FONT_FAMILY, int(10 * scale)),
            command=self._on_scale_change
        )
        scale_slider.pack(side=tk.LEFT)
        
        self.scale_label = tk.Label(scale_frame, text=f"{self.ui_scale.get()}%", font=(self.FONT_FAMILY, int(13 * scale), "bold"))
        self.scale_label.pack(side=tk.RIGHT, padx=(int(2 * scale), int(10 * scale)))
        row += 2

        def save_and_close():
            self._persist_config()
            self._apply_saved_values_to_ui()
            win.withdraw()
            self._rebuild_ui()

        tk.Button(win, text="保存", command=save_and_close, width=10, font=(self.FONT_FAMILY, int(12 * scale))).grid(
            row=row, column=0, columnspan=3, pady=int(15 * scale)
        )

        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        win.grid_columnconfigure(0, weight=0)
        win.grid_columnconfigure(1, weight=1)
        win.grid_columnconfigure(2, weight=0)

    # ==================== 开始/停止处理 ====================

    def start_processing(self):
        if self.is_running:
            self.log_warning("正在处理中，别急")
            return

        selected = []
        for var, path in zip(self.file_vars, self.file_paths):
            if var.get() == 1:
                selected.append(path)

        if not selected:
            self.log_warning("请至少勾选一个目录")
            return

        if not self.converter:
            self.log_error("请先搜索目录")
            return

        if not self.converter.ffmpeg_path:
            self.log_error("未找到 ffmpeg.exe，请将 ffmpeg.exe 放在程序目录或添加到系统 PATH")
            return

        out_dir = self.output_dir.get().strip()
        if not out_dir:
            self.log_warning("未设输出目录，将生成在缓存目录旁")
            out_dir = None
        else:
            os.makedirs(out_dir, exist_ok=True)

        self.converter.naming_template = self.naming_template.get()
        self.converter.duplicate_handling = self.duplicate_handling.get()
        self.converter.delete_original = self.delete_original.get()

        self._set_processing_state(True)
        self.total_label.config(text=f"视频总数：{len(selected)}")
        self._update_status_labels(completed=0, skipped=0)
        self.log_info(f"开始处理 {len(selected)} 个目录...")

        def run():
            try:
                success, skipped, failed, unprocessed = self.converter.process_selected(
                    selected,
                    out_dir,
                    self.log,
                    lambda idx, total: self.root.after(0, self._update_status_labels, idx),
                    lambda cnt: self.root.after(0, self._update_status_labels, None, cnt)
                )
                self.root.after(0, self._update_status_labels, success, skipped)

                total = len(selected)
                processed_count = success + failed
                message = f"处理统计：总数: {total}, 已处理: {processed_count}, 未处理: {unprocessed}, 跳过: {skipped}"
                self.log_info(message)
                self.root.after(0, lambda: show_notification("处理完成", message))
                
                if self.open_output_dir.get() and out_dir and os.path.exists(out_dir):
                    try:
                        os.startfile(out_dir)
                        self.log_info(f"已打开输出目录: {out_dir}")
                    except Exception as e:
                        self.log_warning(f"打开输出目录失败: {e}")
            except Exception as e:
                self.log_error(f"处理出错: {e}")
            finally:
                self.root.after(0, self._set_processing_state, False)

        threading.Thread(target=run, daemon=True).start()

    def stop_processing(self):
        if not self.is_running:
            return
        self.log_warning("正在停止处理...")
        self.btn_stop.config(state="disabled")
        if self.converter:
            self.converter.request_stop()

    # ==================== 关闭窗口 ====================

    def on_closing(self):
        if self.is_running:
            self.log_warning("正在处理，强制关闭可能丢失进度")
            if self.converter:
                self.converter.request_stop()
            self.root.after(1500, self._force_close)
            return
        self._force_close()

    def _force_close(self):
        self._persist_config()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    scale, _ = setup_dpi_awareness()
    app = BiliConverterGUI(root, scale)
    root.mainloop()
