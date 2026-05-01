#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Performance Monitor Windows Service
"""

import os
import sys
import json
import time
import threading
import logging
import traceback
import subprocess
import shutil
from pathlib import Path
import ctypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import win32service
import win32serviceutil
import win32event
import winreg
import servicemanager
import psutil

# Define data directory in ProgramData
PROGRAM_DATA_DIR = Path(os.environ.get("ProgramData", "C:\\ProgramData")) / "PerformanceMonitor"
SERVICE_RUN_ARGUMENT = "--run-service"
NVIDIA_SMI_PATH = shutil.which("nvidia-smi")
GPU_AVAILABLE = NVIDIA_SMI_PATH is not None
DELETE_ACCESS = 0x00010000

# Logging configuration
LOG_DIR = Path(os.path.expandvars(r'%PROGRAMDATA%\PerformanceMonitor'))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / 'performance_monitor.log'

logger = logging.getLogger('PerformanceMonitor')
logger.setLevel(logging.INFO)

# console log and log file handler (INFO and above)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)


class PerformanceMonitorService(win32serviceutil.ServiceFramework):
    _svc_name_ = "PerformanceMonitor"
    _svc_display_name_ = "Performance Monitor Service"
    _svc_description_ = "System performance monitoring service for Wallpaper Engine"
    
    def __init__(self, args):
        win32serviceutil.ServiceFramework.__init__(self, args)
        self.hWaitStop = win32event.CreateEvent(None, 0, 0, None)
        self.http_server = None
        self.server_thread = None
        self.monitor_thread = None
        self.running = False
        self.performance_data = {}
        self.data_file = LOG_DIR / 'performance.json'
        self.port, self.collect_config, self.user_sid = self.load_config()
        self.last_net_io = psutil.net_io_counters()
        self.last_net_time = time.time()
        self.psutil_was_enabled = False
        
        logger.warning(f"Performance Monitor Service initialized (Port: {self.port})")
    
    def load_config(self):
        """Load configuration"""
        try:
            config_file = PROGRAM_DATA_DIR / 'config.json'

            port = 5000
            collect = {
                "psutil": True,
                "hwinfo": True
            }
            user_sid = None

            if config_file.exists():
                with open(config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)

                port = config.get("port", port)

                collect_cfg = config.get("collect", {})
                collect["psutil"] = collect_cfg.get("psutil", True)
                collect["hwinfo"] = collect_cfg.get("hwinfo", True)

                user_sid = config.get("user_sid", None)
                if user_sid:
                    user_sid = user_sid.strip()
                    if not user_sid:
                        user_sid = None

                logger.warning(f"Config loaded: port={port}, collect={collect}, user_sid={'set' if user_sid else 'not set'}")

            return port, collect, user_sid
        except Exception as e:
            logger.warning(f"Error loading config: {e}")
            return 5000, {"psutil": True, "hwinfo": True}, None

    def get_gpu_stats(self):
        """Get GPU usage, VRAM usage, and temperature via nvidia-smi."""
        if not NVIDIA_SMI_PATH:
            return None

        try:
            command = [
                NVIDIA_SMI_PATH,
                "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ]
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            first_line = result.stdout.strip().splitlines()[0]
            gpu_util, memory_used, memory_total, temperature = [
                part.strip() for part in first_line.split(",")
            ]
            memory_used = float(memory_used)
            memory_total = float(memory_total)
            return {
                "gpu_usage": round(float(gpu_util), 1),
                "vram_usage": round((memory_used / memory_total) * 100, 1) if memory_total else 0.0,
                "vram_used_gb": round(memory_used / 1024, 1),
                "vram_total_gb": round(memory_total / 1024, 1),
                "gpu_temp": round(float(temperature), 1),
            }
        except Exception as e:
            logger.debug(f"GPU data unavailable: {e}")

        return None

    def SvcStop(self):
        logger.info("Service stop requested")
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.hWaitStop)
        self.running = False
        
        if self.http_server:
            logger.info("Stopping local HTTP server...")
            try:
                self.http_server.shutdown()
                self.http_server.server_close()
            except Exception as e:
                logger.warning(f"HTTP server shutdown warning: {e}")
        
        logger.info("Service stopped")

    def SvcDoRun(self):
        logger.info("Performance Monitor Service starting...")
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, '')
        )
        
        try:
            self.running = True
            self.main()
        except Exception as e:
            logger.error(f"Service error: {e}")
            logger.error(traceback.format_exc())
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_ERROR_TYPE,
                servicemanager.PYS_SERVICE_STOPPED,
                (self._svc_name_, str(e))
            )

    def create_http_handler(self):
        """Create a lightweight local-only HTTP handler."""
        service = self

        class PerformanceMonitorHandler(BaseHTTPRequestHandler):
            server_version = "PerformanceMonitor/1.0"
            sys_version = ""

            def log_message(self, format, *args):
                logger.debug("HTTP %s - %s", self.address_string(), format % args)

            def _send_json(self, payload, status_code=200):
                body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(status_code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')

                origin = self.headers.get('Origin')
                if origin in (None, "null"):
                    self.send_header('Access-Control-Allow-Origin', 'null' if origin == "null" else '*')
                elif origin.startswith('http://127.0.0.1') or origin.startswith('http://localhost'):
                    self.send_header('Access-Control-Allow-Origin', origin)
                    self.send_header('Vary', 'Origin')

                self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type')
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(204)
                self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type')
                origin = self.headers.get('Origin')
                if origin in (None, "null"):
                    self.send_header('Access-Control-Allow-Origin', 'null' if origin == "null" else '*')
                elif origin.startswith('http://127.0.0.1') or origin.startswith('http://localhost'):
                    self.send_header('Access-Control-Allow-Origin', origin)
                    self.send_header('Vary', 'Origin')
                self.end_headers()

            def do_GET(self):
                try:
                    if self.path == '/performance':
                        if service.data_file.exists():
                            with open(service.data_file, 'r', encoding='utf-8') as f:
                                payload = json.load(f)
                        else:
                            payload = service.get_default_data()
                        self._send_json(payload)
                        return

                    if self.path == '/status':
                        self._send_json({
                            'status': 'running',
                            'service': service._svc_display_name_,
                            'port': service.port,
                            'gpu_available': GPU_AVAILABLE,
                            'timestamp': time.time()
                        })
                        return

                    self._send_json({'error': 'Not found'}, 404)
                except Exception as e:
                    logger.error(f"Error serving request {self.path}: {e}")
                    self._send_json({'error': str(e)}, 500)

        return PerformanceMonitorHandler

    def get_default_data(self):
        """Return default performance data"""
        return {
            "timestamp": time.time(),
            "psutil": {
                'cpu': 0,
                'cpu_percore': [],
                'memory': 0,
                'memory_gb': None,
                'gpu_usage': 0,
                'vram_usage': 0,
                'vram_gb': None,
                'gpu_temp': None,
                'upload_speed': 0,
                'download_speed': 0
            },
            "hwinfo": {
                "available": False,
                "sensors": []
            }
        }

    def _read_hwinfo_key(self, root, path):
        sensors = []
        try:
            with winreg.OpenKey(root, path) as key:
                i = 0
                while True:
                    try:
                        entry = {"id": i}

                        for name in (
                            "Sensor",
                            "Label",
                            "Value",
                            "ValueRaw",
                            "Color"
                        ):
                            try:
                                entry[name.lower()] = winreg.QueryValueEx(
                                    key, f"{name}{i}"
                                )[0]
                            except FileNotFoundError:
                                entry[name.lower()] = None

                        if entry["label"] is None:
                            break

                        sensors.append(entry)
                        i += 1

                    except FileNotFoundError:
                        break
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug(f"HWiNFO read error ({path}): {e}")

        return sensors

    def get_hwinfo_sensors(self):
        """Get HWiNFO Gadget sensors"""
        hwinfo_reg_path = r"SOFTWARE\HWiNFO64\VSB"

        # fallback: HKEY_USERS\<SID>
        if self.user_sid:
            logger.debug(f"Using user-specified SID: {self.user_sid}")
            sensors = self._read_hwinfo_key(
                winreg.HKEY_USERS,
                fr"{self.user_sid}\{hwinfo_reg_path}"
            )
            if sensors:
                logger.debug(f"HWiNFO sensors loaded from HKEY_USERS\\{self.user_sid}")
                return sensors
            else:
                logger.warning(f"No HWiNFO sensors found for SID: {self.user_sid}, falling back to HKEY_LOCAL_MACHINE")
        
        # default: HKEY_LOCAL_MACHINE
        sensors = self._read_hwinfo_key(
            winreg.HKEY_LOCAL_MACHINE,
            hwinfo_reg_path
        )
        if sensors:
            logger.debug("HWiNFO sensors loaded from HKEY_LOCAL_MACHINE")
            return sensors

        return []

    def get_performance_data(self):
        """Collect performance data"""
        try:
            result = {"timestamp": time.time()}
            is_psutil_enabled = self.collect_config.get("psutil", True)

            # ===== psutil backend =====
            if is_psutil_enabled:
                # --- GPU (nvidia-smi) ---
                gpu_usage, vram_usage = 0, 0
                vram_used_gb, vram_total_gb = None, None
                gpu_temp = None
                gpu_stats = self.get_gpu_stats()
                if gpu_stats:
                    gpu_usage = gpu_stats["gpu_usage"]
                    vram_usage = gpu_stats["vram_usage"]
                    vram_used_gb = gpu_stats["vram_used_gb"]
                    vram_total_gb = gpu_stats["vram_total_gb"]
                    gpu_temp = gpu_stats["gpu_temp"]

                # --- Memory ---
                mem = psutil.virtual_memory()

                # --- Network (upload/download speed in Kpbs) ---
                net_io_now = psutil.net_io_counters()
                time_now = time.time()

                if not self.psutil_was_enabled:
                    upload_speed = 0.0
                    download_speed = 0.0
                    self.psutil_was_enabled = True
                else:
                    elapsed = time_now - self.last_net_time
                    if elapsed > 0:
                        upload_speed = round(
                            ((net_io_now.bytes_sent - self.last_net_io.bytes_sent) * 8 / 1000) / elapsed,
                            1
                        )
                        download_speed = round(
                            ((net_io_now.bytes_recv - self.last_net_io.bytes_recv) * 8 / 1000) / elapsed,
                            1
                        )

                self.last_net_io = net_io_now
                self.last_net_time = time_now

                data = {
                    # CPU
                    'cpu': round(psutil.cpu_percent(), 1),
                    'cpu_percore': psutil.cpu_percent(percpu=True),

                    # Memory
                    'memory': round(mem.percent, 1),
                    'memory_gb': f"{mem.used / (1024**3):.1f} GB/{mem.total / (1024**3):.1f} GB",

                    # GPU
                    'gpu_usage': gpu_usage,
                    'vram_usage': vram_usage,
                    'vram_gb': (
                        f"{vram_used_gb:.1f} GB/{vram_total_gb:.1f} GB"
                        if vram_used_gb is not None else None
                    ),
                    'gpu_temp': gpu_temp,

                    # Network
                    'upload_speed': upload_speed,
                    'download_speed': download_speed,

                    'timestamp': time.time()
                }

                # --- Disk capacity per drive ---
                try:
                    for disk in psutil.disk_partitions():
                        if 'cdrom' in disk.opts or disk.fstype == '':
                            continue
                        try:
                            usage = psutil.disk_usage(disk.mountpoint)
                            drive_letter = disk.device[0].lower()
                            data[f'{drive_letter}_disk'] = (
                                f"{usage.used / (1024**3):.1f} GB/"
                                f"{usage.total / (1024**3):.1f} GB"
                            )
                        except PermissionError:
                            continue
                except Exception:
                    pass

                result["psutil"] = data
            else:
                self.psutil_was_enabled = False
                result["psutil"] = None

            # ===== hwinfo backend =====
            if self.collect_config.get("hwinfo", True):
                result["hwinfo"] = self.get_hwinfo_sensors()
            else:
                result["hwinfo"] = None

            return result

        except Exception as e:
            logger.error(f"Error getting performance data: {e}")
            return self.get_default_data()

    def update_performance_loop(self):
        """Loop to update performance data"""
        logger.info("Performance monitoring started")

        while self.running:
            try:
                data = self.get_performance_data()

                with open(self.data_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

                self.performance_data = data

                #  logging (psutil aware)
                log_parts = []

                psutil_data = data.get("psutil")
                if psutil_data:
                    log_parts.append(f"CPU={psutil_data.get('cpu')}%")
                    log_parts.append(f"Memory={psutil_data.get('memory')}%")

                    if psutil_data.get("gpu_temp") is not None:
                        log_parts.append(f"GPU Temp={psutil_data['gpu_temp']}°C")

                hwinfo_data = data.get("hwinfo")
                if isinstance(hwinfo_data, list) and hwinfo_data:
                    log_parts.append(f"HWiNFO={len(hwinfo_data)} sensors")

                if log_parts:
                    logger.debug("Performance data updated: " + ", ".join(log_parts))
                else:
                    logger.debug("Performance data updated (no active backends)")

            except Exception as e:
                logger.error(f"Error updating performance data: {e}")
                logger.error(traceback.format_exc())

            time.sleep(1)

        logger.info("Performance monitoring stopped")


    def run_http_server(self):
        """Run the local HTTP server."""
        try:
            handler = self.create_http_handler()
            self.http_server = ThreadingHTTPServer(('127.0.0.1', self.port), handler)
            self.http_server.daemon_threads = True
            logger.info(f"Starting local HTTP server on http://127.0.0.1:{self.port}")
            self.http_server.serve_forever(poll_interval=0.5)
        except Exception as e:
            logger.error(f"HTTP server error: {e}")
            logger.error(traceback.format_exc())
        finally:
            self.http_server = None

    def main(self):
        """Main processing"""
        try:
            self.monitor_thread = threading.Thread(target=self.update_performance_loop, daemon=True)
            self.monitor_thread.start()
            logger.info("Performance monitoring thread started")
            
            self.server_thread = threading.Thread(target=self.run_http_server, daemon=True)
            self.server_thread.start()
            logger.info("HTTP server thread started")
            
            logger.info("Performance Monitor Service is running")
            win32event.WaitForSingleObject(self.hWaitStop, win32event.INFINITE)
            
        except Exception as e:
            logger.error(f"Main thread error: {e}")
            logger.error(traceback.format_exc())


def check_admin_rights():
    """Check for admin rights"""
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False


def request_admin_rights():
    """Request admin rights"""
    if check_admin_rights():
        return True
    
    try:
        parameters = subprocess.list2cmdline(sys.argv[1:])
        result = ctypes.windll.shell32.ShellExecuteW(
            None, 
            "runas", 
            sys.executable, 
            parameters,
            None, 
            1
        )
        return result > 32
    except Exception as e:
        logger.error(f"Failed to request admin rights: {e}")
        return False

def install_service():
    """Install the Windows service"""
    try:
        logger.info("Installing Performance Monitor Service...")
        exe_path = sys.executable if getattr(sys, 'frozen', False) else os.path.abspath(__file__)
        logger.info(f"Service executable path: {exe_path}")
        
        svc_name = PerformanceMonitorService._svc_name_

        try:
            logger.info("Attempting to stop existing service...")
            win32serviceutil.StopService(svc_name)
            logger.info("Existing service stopped.")
        except Exception as e:
            logger.debug(f"Service was not running or stop failed: {e}")

        try:
            logger.info("Attempting to remove existing service...")
            win32serviceutil.RemoveService(svc_name)
            logger.info("Existing service marked for deletion.")

            timeout = 30
            start_time = time.time()
            while time.time() - start_time < timeout:
                try:
                    win32serviceutil.QueryServiceStatus(svc_name)
                    logger.debug("Service still exists. Waiting...")
                    time.sleep(1)
                except Exception:
                    logger.info("Existing service removed successfully.")
                    break
            else:
                logger.error("Timeout waiting for service deletion to complete.")
                return False

        except Exception as e:
            logger.debug(f"No existing service to remove or removal failed: {e}")

        hscm = win32service.OpenSCManager(
            None,
            None,
            win32service.SC_MANAGER_CONNECT | win32service.SC_MANAGER_CREATE_SERVICE
        )
        try:
            service_cmd = subprocess.list2cmdline([exe_path, SERVICE_RUN_ARGUMENT])
            hs = win32service.CreateService(
                hscm,
                svc_name,
                PerformanceMonitorService._svc_display_name_,
                win32service.SERVICE_START | win32service.SERVICE_STOP | DELETE_ACCESS | win32service.SERVICE_QUERY_STATUS | win32service.SERVICE_CHANGE_CONFIG,
                win32service.SERVICE_WIN32_OWN_PROCESS,
                win32service.SERVICE_AUTO_START,
                win32service.SERVICE_ERROR_NORMAL,
                service_cmd,
                None,
                0,
                None,
                None,
                None
            )

            try:
                win32service.ChangeServiceConfig2(
                    hs,
                    win32service.SERVICE_CONFIG_DESCRIPTION,
                    PerformanceMonitorService._svc_description_
                )
            except Exception as e:
                logger.warning(f"Failed to set service description: {e}")
            
            win32service.CloseServiceHandle(hs)
            logger.info("Service installed successfully")
            return True

        finally:
            win32service.CloseServiceHandle(hscm)

    except Exception as e:
        logger.error(f"Service installation failed: {e}")
        logger.error(traceback.format_exc())
        return False


def start_service():
    """Start the service"""
    try:
        logger.info("Starting Performance Monitor Service...")
        hscm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        try:
            hs = win32service.OpenService(
                hscm,
                PerformanceMonitorService._svc_name_,
                win32service.SERVICE_START | win32service.SERVICE_QUERY_STATUS
            )
            try:
                win32service.StartService(hs, None)
                logger.info("Service started successfully")
                return True
            finally:
                win32service.CloseServiceHandle(hs)
        finally:
            win32service.CloseServiceHandle(hscm)
            
    except Exception as e:
        logger.error(f"Service start failed: {e}")
        logger.error(traceback.format_exc())
        return False


def get_service_port():
    try:
        config_file = PROGRAM_DATA_DIR / 'config.json'
        if config_file.exists():
            with open(config_file, 'r', encoding='utf-8') as f:
                return json.load(f).get('port', 5000)
        else:
            return 5000
    except Exception as e:
        logger.warning(f"Error loading port config: {e}, using default port 5000")
        return 5000

def main():
    """Main function"""
    LOG_DIR.mkdir(exist_ok=True)
    
    if getattr(sys, 'frozen', False):
        if len(sys.argv) == 1:
            print("Performance Monitor Service Installer")
            print("====================================")
            
            if not check_admin_rights():
                print("Admin rights are required. Restarting as admin...")
                if not request_admin_rights():
                    input("Failed to obtain admin rights. Press Enter to exit...")
                    return
                else:
                    return
            
            print("Running with admin rights...")
            
            if install_service():
                print("Service installation completed.")
                time.sleep(3)
                if start_service():
                    port = get_service_port()
                    print("\n============================================================")
                    print("Service started successfully.")
                    print(f" > Log file: {LOG_FILE}")
                    print(f" > Performance data: http://127.0.0.1:{port}/performance")
                    print(f" > Service status: http://127.0.0.1:{port}/status")
                    print("============================================================")
                    print("\nFor more detailed usage instructions, please refer to the README:")
                    print(" > https://github.com/sheetau/PerformanceMonitor/tree/main")
                    print("\nYou can change the port number and data sources by placing a config.json file")
                    print(f"in {PROGRAM_DATA_DIR}:")
                    print(" > https://github.com/sheetau/PerformanceMonitor/blob/main/config.json")
                    print("\nTo uninstall, download the uninstaller from:")
                    print(" > https://github.com/sheetau/PerformanceMonitor/releases/latest")
                        
                else:
                    print(f"Failed to start the service. Check logs at {LOG_FILE}")
            else:
                print(f"Failed to install the service. Check logs at {LOG_FILE}")
            
            input("\nPress Enter to exit...")
            return
        else:
            arg = sys.argv[1].lower()
            
            if not check_admin_rights():
                print("Admin rights are required.")
                input("Press Enter to exit...")
                return
                
            if arg == 'install':
                if install_service():
                    print("Service installation completed.")
                    if start_service():
                        print("Service started successfully.")
                return
            elif arg == 'start':
                if start_service():
                    print("Service started successfully.")
                return
            elif arg == 'stop':
                try:
                    win32serviceutil.StopService(PerformanceMonitorService._svc_name_)
                    print("Service stopped.")
                except Exception as e:
                    print(f"Service stop error: {e}")
                return
            elif arg == 'remove':
                try:
                    win32serviceutil.StopService(PerformanceMonitorService._svc_name_)
                    time.sleep(2)
                except:
                    pass
                try:
                    win32serviceutil.RemoveService(PerformanceMonitorService._svc_name_)
                    print("Service removed.")
                except Exception as e:
                    print(f"Service removal error: {e}")
                return
            elif arg in [SERVICE_RUN_ARGUMENT, 'debug', '--debug']:
                logger.info("Running in service host mode")
                servicemanager.Initialize()
                servicemanager.PrepareToHostSingle(PerformanceMonitorService)
                servicemanager.StartServiceCtrlDispatcher()
                return
            else:
                try:
                    win32serviceutil.HandleCommandLine(PerformanceMonitorService)
                except SystemExit:
                    pass
                return
    else:
        if len(sys.argv) > 1:
            win32serviceutil.HandleCommandLine(PerformanceMonitorService)
        else:
            print("Development mode - running service directly")
            service = PerformanceMonitorService([''])
            service.main()
    
    logger.info("Starting as Windows Service")
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(PerformanceMonitorService)
    servicemanager.StartServiceCtrlDispatcher()


if __name__ == '__main__':
    main()
