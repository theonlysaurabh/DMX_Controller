"""
gui.py - Modern GUI Controller for DMX Music Light System.

Features:
  - Dynamic song selection from DMX folder (auto-updates when songs are added).
  - Individual curve detection and auto/batch analysis using analyze.py.
  - COM port selection with live refresh and quick light test.
  - Three playback modes: Loop Once, Loop Single, Loop All.
  - Instant Stop button with hardware blackout.
  - Live DMX stage light preview and RGB+Master dimmer visualizer.
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import time
import threading
from typing import Optional, List
import numpy as np
import serial
import serial.tools.list_ports
import librosa
import sounddevice as sd

import tkinter as tk
from tkinter import ttk, messagebox

from analyze import (
    analyze_song,
    find_curve_for_song,
    get_curve_path,
    FPS,
    HEADER,
    CURVES_DIR_NAME,
)

# ---- Windows High-DPI Support ----
try:
    from ctypes import windll
    windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

AUDIO_EXTENSIONS = (".mp3", ".wav", ".ogg", ".flac", ".m4a")
DEFAULT_BAUD = 115200
LIGHT_DELAY = 0.0


class DMXMusicControllerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("DMX Music Light Controller")
        self.root.geometry("860x740")
        self.root.minsize(780, 680)

        self.dmx_dir = os.path.dirname(os.path.abspath(__file__))
        self.curves_dir = os.path.join(self.dmx_dir, CURVES_DIR_NAME)
        os.makedirs(self.curves_dir, exist_ok=True)

        # ---- State variables ----
        self.songs_list: List[str] = []
        self.selected_song_var = tk.StringVar()
        self.port_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready. Select a song and COM port.")
        self.mode_badge_var = tk.StringVar(value="STOPPED")
        self.curve_status_var = tk.StringVar(value="")
        self.time_label_var = tk.StringVar(value="00:00 / 00:00")
        self.telemetry_var = tk.StringVar(value="R: 0  |  G: 0  |  B: 0  |  Dimmer: 0")

        # Playback control
        self.is_playing = False
        self.current_mode: Optional[str] = None   # "once", "single", "all"
        self.current_song_idx = -1
        self.stop_event = threading.Event()
        self.play_thread: Optional[threading.Thread] = None
        self.ser: Optional[serial.Serial] = None
        self.ser_lock = threading.Lock()

        # UI Color scheme
        self.c_bg = "#121216"
        self.c_panel = "#1b1b22"
        self.c_panel_border = "#2a2a36"
        self.c_text = "#f3f4f6"
        self.c_text_muted = "#9ca3af"
        self.c_green = "#10b981"
        self.c_blue = "#3b82f6"
        self.c_purple = "#8b5cf6"
        self.c_red = "#ef4444"
        self.c_amber = "#f59e0b"
        self.c_cyan = "#06b6d4"

        self.root.configure(bg=self.c_bg)
        self._setup_styles()
        self._build_ui()

        # Initial scans
        self.refresh_ports()
        self.refresh_songs()

        # Start periodic folder monitor (every 2.5s)
        self.root.after(2500, self._auto_poll_folder)

        # Handle window close cleanly
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")

        # Configure common ttk styles
        style.configure("TFrame", background=self.c_bg)
        style.configure("Panel.TFrame", background=self.c_panel, relief="solid", borderwidth=1)
        style.configure("TLabel", background=self.c_bg, foreground=self.c_text, font=("Segoe UI", 10))
        style.configure("Panel.TLabel", background=self.c_panel, foreground=self.c_text, font=("Segoe UI", 10))
        style.configure("Header.TLabel", background=self.c_panel, foreground="#ffffff", font=("Segoe UI", 11, "bold"))
        style.configure("Muted.TLabel", background=self.c_panel, foreground=self.c_text_muted, font=("Segoe UI", 9))
        style.configure("TProgressbar", thickness=12, troughcolor="#23232c", background=self.c_cyan)

        # Combobox style
        style.configure("TCombobox", fieldbackground="#24242e", background="#373747", foreground="#ffffff",
                        arrowcolor="#ffffff", bordercolor=self.c_panel_border, relief="flat")
        style.map("TCombobox", fieldbackground=[("readonly", "#24242e")], foreground=[("readonly", "#ffffff")])

    def _build_ui(self):
        main_frame = tk.Frame(self.root, bg=self.c_bg, padx=18, pady=14)
        main_frame.pack(fill="both", expand=True)

        # ---- HEADER BAR ----
        header_frame = tk.Frame(main_frame, bg=self.c_bg)
        header_frame.pack(fill="x", pady=(0, 12))

        title_lbl = tk.Label(header_frame, text="⚡ DMX MUSIC LIGHT CONTROLLER",
                             font=("Segoe UI", 16, "bold"), fg="#ffffff", bg=self.c_bg)
        title_lbl.pack(side="left")

        mode_badge = tk.Label(header_frame, textvariable=self.mode_badge_var,
                              font=("Segoe UI", 10, "bold"), fg=self.c_text_muted,
                              bg="#252530", padx=10, pady=4, relief="flat")
        mode_badge.pack(side="right")
        self.mode_badge_lbl = mode_badge

        # ---- PANEL 1: HARDWARE & COM PORT ----
        hw_panel = tk.LabelFrame(main_frame, text=" ESP32 DMX Interface ", font=("Segoe UI", 10, "bold"),
                                 fg=self.c_cyan, bg=self.c_panel, bd=1, relief="solid", padx=14, pady=10)
        hw_panel.pack(fill="x", pady=(0, 10))

        hw_grid = tk.Frame(hw_panel, bg=self.c_panel)
        hw_grid.pack(fill="x")

        tk.Label(hw_grid, text="Select Serial Port:", font=("Segoe UI", 10),
                 fg=self.c_text, bg=self.c_panel).pack(side="left", padx=(0, 8))

        self.port_combo = ttk.Combobox(hw_grid, textvariable=self.port_var, width=28, state="readonly")
        self.port_combo.pack(side="left", padx=(0, 10))
        self.port_combo.bind("<<ComboboxSelected>>", lambda e: self._on_port_changed())

        btn_refresh_ports = tk.Button(hw_grid, text="🔄 Refresh Ports", font=("Segoe UI", 9),
                                      bg="#2c2c38", fg="#ffffff", activebackground="#3d3d4d",
                                      activeforeground="#ffffff", bd=0, padx=10, pady=3,
                                      cursor="hand2", command=self.refresh_ports)
        btn_refresh_ports.pack(side="left", padx=(0, 10))

        btn_test_lights = tk.Button(hw_grid, text="💡 Test Lights (Sweeps)", font=("Segoe UI", 9, "bold"),
                                    bg="#374151", fg="#f9fafb", activebackground="#4b5563",
                                    activeforeground="#ffffff", bd=0, padx=12, pady=3,
                                    cursor="hand2", command=self.trigger_test_sweeps)
        btn_test_lights.pack(side="right")

        # ---- PANEL 2: SONG SELECTION & CURVE STATUS ----
        song_panel = tk.LabelFrame(main_frame, text=" Music Library & DMX Curves ",
                                   font=("Segoe UI", 10, "bold"), fg=self.c_purple,
                                   bg=self.c_panel, bd=1, relief="solid", padx=14, pady=12)
        song_panel.pack(fill="x", pady=(0, 10))

        # Row 1: Dropdown + Refresh
        row1 = tk.Frame(song_panel, bg=self.c_panel)
        row1.pack(fill="x", pady=(0, 8))

        tk.Label(row1, text="Selected Song:", font=("Segoe UI", 10, "bold"),
                 fg=self.c_text, bg=self.c_panel).pack(side="left", padx=(0, 8))

        self.song_combo = ttk.Combobox(row1, textvariable=self.selected_song_var,
                                       width=48, state="readonly", postcommand=self.refresh_songs)
        self.song_combo.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.song_combo.bind("<<ComboboxSelected>>", lambda e: self.on_song_selected())

        btn_refresh_songs = tk.Button(row1, text="🔄 Refresh Songs", font=("Segoe UI", 9),
                                      bg="#2c2c38", fg="#ffffff", activebackground="#3d3d4d",
                                      activeforeground="#ffffff", bd=0, padx=10, pady=3,
                                      cursor="hand2", command=self.refresh_songs)
        btn_refresh_songs.pack(side="left")

        # Row 2: Curve Status + Analyze Buttons
        row2 = tk.Frame(song_panel, bg=self.c_panel)
        row2.pack(fill="x", pady=(4, 0))

        self.curve_status_lbl = tk.Label(row2, textvariable=self.curve_status_var,
                                         font=("Segoe UI", 9, "bold"), fg=self.c_amber,
                                         bg=self.c_panel)
        self.curve_status_lbl.pack(side="left")

        btn_analyze_all = tk.Button(row2, text="⚡ Analyze All Missing", font=("Segoe UI", 9),
                                    bg="#2c2c38", fg=self.c_amber, activebackground="#3d3d4d",
                                    activeforeground="#ffffff", bd=0, padx=10, pady=3,
                                    cursor="hand2", command=self.trigger_analyze_all)
        btn_analyze_all.pack(side="right")

        self.btn_analyze = tk.Button(row2, text="⚡ Analyze Song", font=("Segoe UI", 9, "bold"),
                                     bg="#854d0e", fg="#fef08a", activebackground="#a16207",
                                     activeforeground="#ffffff", bd=0, padx=12, pady=3,
                                     cursor="hand2", command=self.trigger_analyze_selected)
        self.btn_analyze.pack(side="right", padx=(0, 8))

        # ---- PANEL 3: PLAYBACK CONTROLS (THE 3 MODE BUTTONS + STOP) ----
        ctrl_panel = tk.LabelFrame(main_frame, text=" Playback Controls ",
                                   font=("Segoe UI", 10, "bold"), fg=self.c_green,
                                   bg=self.c_panel, bd=1, relief="solid", padx=14, pady=14)
        ctrl_panel.pack(fill="x", pady=(0, 10))

        btn_grid = tk.Frame(ctrl_panel, bg=self.c_panel)
        btn_grid.pack(fill="x")

        # Button 1: Loop Once
        self.btn_loop_once = tk.Button(
            btn_grid, text="▶  Loop Once", font=("Segoe UI", 11, "bold"),
            bg="#059669", fg="#ffffff", activebackground="#10b981", activeforeground="#ffffff",
            bd=0, height=2, cursor="hand2", command=lambda: self.start_playback("once")
        )
        self.btn_loop_once.pack(side="left", fill="x", expand=True, padx=(0, 8))

        # Button 2: Loop Single
        self.btn_loop_single = tk.Button(
            btn_grid, text="🔁  Loop Single", font=("Segoe UI", 11, "bold"),
            bg="#2563eb", fg="#ffffff", activebackground="#3b82f6", activeforeground="#ffffff",
            bd=0, height=2, cursor="hand2", command=lambda: self.start_playback("single")
        )
        self.btn_loop_single.pack(side="left", fill="x", expand=True, padx=(0, 8))

        # Button 3: Loop All
        self.btn_loop_all = tk.Button(
            btn_grid, text="🔂  Loop All", font=("Segoe UI", 11, "bold"),
            bg="#7c3aed", fg="#ffffff", activebackground="#8b5cf6", activeforeground="#ffffff",
            bd=0, height=2, cursor="hand2", command=lambda: self.start_playback("all")
        )
        self.btn_loop_all.pack(side="left", fill="x", expand=True, padx=(0, 8))

        # Button 4: STOP
        self.btn_stop = tk.Button(
            btn_grid, text="⏹  STOP", font=("Segoe UI", 11, "bold"),
            bg="#dc2626", fg="#ffffff", activebackground="#ef4444", activeforeground="#ffffff",
            bd=0, height=2, cursor="hand2", command=self.stop_playback
        )
        self.btn_stop.pack(side="left", fill="x", expand=True)

        # ---- PANEL 4: LIVE DMX LIGHT MONITOR & STAGE PREVIEW ----
        vis_panel = tk.LabelFrame(main_frame, text=" Live DMX Light Monitor ",
                                  font=("Segoe UI", 10, "bold"), fg=self.c_cyan,
                                  bg=self.c_panel, bd=1, relief="solid", padx=14, pady=10)
        vis_panel.pack(fill="both", expand=True, pady=(0, 10))

        vis_container = tk.Frame(vis_panel, bg=self.c_panel)
        vis_container.pack(fill="both", expand=True)

        # Stage fixture canvas (shows simulated bulb)
        self.canvas_size = 130
        self.canvas = tk.Canvas(vis_container, width=self.canvas_size, height=self.canvas_size,
                                bg="#13131a", bd=1, highlightthickness=0)
        self.canvas.pack(side="left", padx=(0, 16), pady=4)
        self._init_canvas_fixture()

        # Telemetry & Progress
        tele_frame = tk.Frame(vis_container, bg=self.c_panel)
        tele_frame.pack(side="left", fill="both", expand=True)

        self.lbl_telemetry = tk.Label(tele_frame, textvariable=self.telemetry_var,
                                      font=("Consolas", 11, "bold"), fg=self.c_cyan,
                                      bg=self.c_panel, anchor="w")
        self.lbl_telemetry.pack(fill="x", pady=(4, 6))

        # Progress bar
        self.progress_bar = ttk.Progressbar(tele_frame, orient="horizontal", mode="determinate")
        self.progress_bar.pack(fill="x", pady=(0, 6))

        self.lbl_time = tk.Label(tele_frame, textvariable=self.time_label_var,
                                 font=("Segoe UI", 9), fg=self.c_text_muted,
                                 bg=self.c_panel, anchor="w")
        self.lbl_time.pack(fill="x")

        # Color pills display
        self.pills_frame = tk.Frame(tele_frame, bg=self.c_panel)
        self.pills_frame.pack(fill="x", pady=(8, 0))

        self.pill_r = tk.Label(self.pills_frame, text="R: 0", font=("Consolas", 9, "bold"),
                               bg="#371717", fg="#fca5a5", width=8, pady=2)
        self.pill_r.pack(side="left", padx=(0, 6))

        self.pill_g = tk.Label(self.pills_frame, text="G: 0", font=("Consolas", 9, "bold"),
                               bg="#14321f", fg="#86efac", width=8, pady=2)
        self.pill_g.pack(side="left", padx=(0, 6))

        self.pill_b = tk.Label(self.pills_frame, text="B: 0", font=("Consolas", 9, "bold"),
                               bg="#172554", fg="#93c5fd", width=8, pady=2)
        self.pill_b.pack(side="left", padx=(0, 6))

        self.pill_m = tk.Label(self.pills_frame, text="M: 0", font=("Consolas", 9, "bold"),
                               bg="#2d2d38", fg="#f3f4f6", width=12, pady=2)
        self.pill_m.pack(side="left")

        # ---- BOTTOM STATUS BAR ----
        status_frame = tk.Frame(main_frame, bg="#0d0d10", padx=10, pady=5)
        status_frame.pack(fill="x")

        self.status_lbl = tk.Label(status_frame, textvariable=self.status_var,
                                   font=("Segoe UI", 9), fg=self.c_text_muted, bg="#0d0d10")
        self.status_lbl.pack(side="left")

        tk.Label(status_frame, text="DMX: 40 FPS | [0xFF, R, G, B, M]",
                 font=("Segoe UI", 8), fg="#64748b", bg="#0d0d10").pack(side="right")

    def _init_canvas_fixture(self):
        cx, cy = self.canvas_size // 2, self.canvas_size // 2
        r_outer = 56
        r_inner = 42

        # Outer chassis
        self.canvas.create_oval(cx - r_outer, cy - r_outer, cx + r_outer, cy + r_outer,
                                fill="#20202a", outline="#3b3b4f", width=2, tags="chassis")
        # Glow halo
        self.glow_item = self.canvas.create_oval(cx - r_inner - 4, cy - r_inner - 4,
                                                cx + r_inner + 4, cy + r_inner + 4,
                                                fill="#000000", outline="", tags="glow")
        # Lens
        self.lens_item = self.canvas.create_oval(cx - r_inner, cy - r_inner,
                                                cx + r_inner, cy + r_inner,
                                                fill="#101018", outline="#525266", width=2, tags="lens")
        # Center LED text
        self.led_text_item = self.canvas.create_text(cx, cy, text="OFF",
                                                    fill="#64748b", font=("Segoe UI", 9, "bold"))

    def update_visualizer(self, r: int, g: int, b: int, m: int, frame_idx: int = 0, total_frames: int = 1):
        """Update live DMX preview bulb and telemetry."""
        # Scale color by master dimmer for realistic optical preview
        dim_factor = m / 254.0
        pr = int(r * dim_factor)
        pg = int(g * dim_factor)
        pb = int(b * dim_factor)
        hex_color = f"#{pr:02x}{pg:02x}{pb:02x}"

        # Update stage light bulb
        if m > 5:
            self.canvas.itemconfig(self.lens_item, fill=hex_color, outline="#ffffff")
            self.canvas.itemconfig(self.glow_item, fill=hex_color)
            self.canvas.itemconfig(self.led_text_item, text=f"{int(dim_factor * 100)}%",
                                   fill="#ffffff" if (pr + pg + pb) < 400 else "#000000")
        else:
            self.canvas.itemconfig(self.lens_item, fill="#0d0d12", outline="#3f3f4e")
            self.canvas.itemconfig(self.glow_item, fill="#000000")
            self.canvas.itemconfig(self.led_text_item, text="OFF", fill="#525266")

        # Update telemetry labels
        self.telemetry_var.set(f"R: {r:3d}  |  G: {g:3d}  |  B: {b:3d}  |  Dimmer: {m:3d} ({int(dim_factor * 100)}%)")
        self.pill_r.config(text=f"R: {r}")
        self.pill_g.config(text=f"G: {g}")
        self.pill_b.config(text=f"B: {b}")
        self.pill_m.config(text=f"M: {m} ({int(dim_factor * 100)}%)")

        # Update progress bar
        if total_frames > 0:
            pct = min(100.0, (frame_idx / total_frames) * 100.0)
            self.progress_bar["value"] = pct
            cur_sec = frame_idx / FPS
            tot_sec = total_frames / FPS
            self.time_label_var.set(
                f"{int(cur_sec // 60):02d}:{int(cur_sec % 60):02d} / "
                f"{int(tot_sec // 60):02d}:{int(tot_sec % 60):02d}  ({pct:.1f}%)"
            )

    # ---- SONG & FOLDER DISCOVERY ----
    def refresh_songs(self, keep_selection: bool = True):
        """Scans the DMX directory for audio files and updates the combobox."""
        try:
            files = [
                f for f in sorted(os.listdir(self.dmx_dir), key=str.lower)
                if f.lower().endswith(AUDIO_EXTENSIONS) and os.path.isfile(os.path.join(self.dmx_dir, f))
            ]
        except Exception as e:
            self.set_status(f"Error reading folder: {e}", is_error=True)
            return

        current = self.selected_song_var.get()
        self.songs_list = files
        self.song_combo["values"] = files

        if files:
            if keep_selection and current in files:
                self.selected_song_var.set(current)
            else:
                self.selected_song_var.set(files[0])
            self.on_song_selected()
        else:
            self.selected_song_var.set("")
            self.curve_status_var.set("No audio files found in DMX folder.")
            self.curve_status_lbl.config(fg=self.c_amber)

    def _auto_poll_folder(self):
        """Silently checks if audio files in the folder changed, updating combobox if so."""
        try:
            current_files = [
                f for f in sorted(os.listdir(self.dmx_dir), key=str.lower)
                if f.lower().endswith(AUDIO_EXTENSIONS) and os.path.isfile(os.path.join(self.dmx_dir, f))
            ]
            if current_files != self.songs_list:
                self.refresh_songs(keep_selection=True)
        except Exception:
            pass
        self.root.after(2500, self._auto_poll_folder)

    def on_song_selected(self):
        song = self.selected_song_var.get()
        if not song:
            return
        song_path = os.path.join(self.dmx_dir, song)
        curve_file = find_curve_for_song(song_path)
        if curve_file and os.path.isfile(curve_file):
            rel = os.path.relpath(curve_file, self.dmx_dir)
            self.curve_status_var.set(f"✓ Curve ready ({rel})")
            self.curve_status_lbl.config(fg=self.c_green)
            self.btn_analyze.config(text="⚡ Re-Analyze")
        else:
            self.curve_status_var.set("⚡ Curve not generated yet (Click Analyze or Play to create)")
            self.curve_status_lbl.config(fg=self.c_amber)
            self.btn_analyze.config(text="⚡ Analyze Song")

    # ---- COM PORT DISCOVERY & HARDWARE ----
    def refresh_ports(self):
        """Scans system for available COM ports."""
        ports = serial.tools.list_ports.comports()
        port_list = [p.device for p in ports]
        self.port_combo["values"] = port_list

        current = self.port_var.get()
        if "COM10" in port_list:
            self.port_var.set("COM10")
        elif current in port_list:
            self.port_var.set(current)
        elif port_list:
            self.port_var.set(port_list[0])
        else:
            self.port_var.set("")
            self.set_status("No COM ports detected! Connect ESP32 and click Refresh Ports.", is_error=True)

    def _on_port_changed(self):
        # Close old port if open so new port can be used
        with self.ser_lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.close()
                except Exception:
                    pass
                self.ser = None
        self.set_status(f"Selected port: {self.port_var.get()}")

    def _get_serial_connection(self) -> serial.Serial:
        """Opens or reuses the serial port connection."""
        port = self.port_var.get().strip()
        if not port:
            raise ValueError("No COM port selected! Please select an ESP32 port.")

        with self.ser_lock:
            if self.ser and self.ser.is_open and self.ser.port == port:
                return self.ser

            if self.ser and self.ser.is_open:
                try:
                    self.ser.close()
                except Exception:
                    pass

            self.set_status(f"Connecting to {port}... (waiting 2s for ESP32 boot)")
            self.root.update_idletasks()
            ser = serial.Serial(port, DEFAULT_BAUD, timeout=1)
            time.sleep(2.0)  # ESP32 resets on DTR/RTS assertion
            self.ser = ser
            return self.ser

    def _send_dmx_frame(self, r: int, g: int, b: int, m: int):
        with self.ser_lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.write(bytes([HEADER, int(r), int(g), int(b), int(m)]))
                except Exception:
                    pass

    def trigger_test_sweeps(self):
        """Runs test color sweeps in background to verify hardware wiring."""
        if self.is_playing:
            messagebox.showinfo("Playback Active", "Please stop music playback before testing lights.")
            return

        def run_test():
            try:
                self.set_mode_badge("TESTING LIGHTS", self.c_amber)
                self.set_status("Running light sweep test...")
                ser = self._get_serial_connection()
                test_colors = [
                    (254, 254, 254, "WHITE"),
                    (254, 0, 0, "RED"),
                    (0, 254, 0, "GREEN"),
                    (0, 0, 254, "BLUE"),
                ]
                for r, g, b, name in test_colors:
                    if self.stop_event.is_set():
                        break
                    for m in list(range(0, 255, 6)) + list(range(254, -1, -6)):
                        if self.stop_event.is_set():
                            break
                        self._send_dmx_frame(r, g, b, m)
                        self.root.after(0, self.update_visualizer, r, g, b, m, m, 254)
                        time.sleep(0.015)
                self._send_dmx_frame(0, 0, 0, 0)
                self.root.after(0, self.update_visualizer, 0, 0, 0, 0, 0, 1)
                self.set_mode_badge("STOPPED", self.c_text_muted)
                self.set_status("Light sweep test completed.")
            except Exception as e:
                self.set_status(f"Test failed: {e}", is_error=True)
                self.set_mode_badge("ERROR", self.c_red)

        self.stop_event.clear()
        threading.Thread(target=run_test, daemon=True).start()

    # ---- ANALYSIS WORKERS ----
    def trigger_analyze_selected(self):
        song = self.selected_song_var.get()
        if not song:
            messagebox.showwarning("No Song Selected", "Please select a song first.")
            return
        song_path = os.path.join(self.dmx_dir, song)
        self._run_analysis_async([song_path])

    def trigger_analyze_all(self):
        un_analyzed = []
        for song in self.songs_list:
            p = os.path.join(self.dmx_dir, song)
            if not find_curve_for_song(p):
                un_analyzed.append(p)

        if not un_analyzed:
            messagebox.showinfo("All Analyzed", "All songs in the DMX folder already have curves!")
            return

        if messagebox.askyesno("Analyze All", f"Found {len(un_analyzed)} songs without curves. Analyze them now?"):
            self._run_analysis_async(un_analyzed)

    def _run_analysis_async(self, file_paths: List[str]):
        def worker():
            total = len(file_paths)
            self.btn_analyze.config(state="disabled")
            for idx, path in enumerate(file_paths, start=1):
                base = os.path.basename(path)
                self.set_status(f"[{idx}/{total}] Analyzing {base}...")

                def cb(msg):
                    self.set_status(f"[{idx}/{total}] {base}: {msg}")

                try:
                    analyze_song(path, status_cb=cb)
                except Exception as e:
                    self.set_status(f"Failed to analyze {base}: {e}", is_error=True)
            self.btn_analyze.config(state="normal")
            self.root.after(0, self.on_song_selected)
            self.set_status(f"Analysis completed ({total} files).")

        threading.Thread(target=worker, daemon=True).start()

    # ---- PLAYBACK WORKER & MODES ----
    def start_playback(self, mode: str):
        """Starts playback in one of the three modes: 'once', 'single', 'all'."""
        if not self.selected_song_var.get():
            messagebox.showwarning("No Song", "Please select a song first.")
            return
        if not self.port_var.get():
            messagebox.showwarning("No COM Port", "Please select an ESP32 COM port.")
            return

        # If already playing, stop current playback first
        if self.is_playing:
            self.stop_playback(silent=True)
            time.sleep(0.15)

        self.current_mode = mode
        self.stop_event.clear()
        self.is_playing = True

        mode_titles = {
            "once": ("PLAYING ONCE", self.c_green),
            "single": ("LOOPING SINGLE", self.c_blue),
            "all": ("LOOPING ALL", self.c_purple),
        }
        badge_text, badge_color = mode_titles.get(mode, ("PLAYING", self.c_cyan))
        self.set_mode_badge(badge_text, badge_color)

        self.play_thread = threading.Thread(target=self._playback_loop_thread, daemon=True)
        self.play_thread.start()

    def stop_playback(self, silent: bool = False):
        """Immediately stops audio and blacks out DMX lights."""
        self.stop_event.set()
        try:
            sd.stop()
        except Exception:
            pass

        self._send_dmx_frame(0, 0, 0, 0)
        self.is_playing = False
        self.current_mode = None
        self.set_mode_badge("STOPPED", self.c_text_muted)
        self.root.after(0, self.update_visualizer, 0, 0, 0, 0, 0, 1)

        if not silent:
            self.set_status("Playback stopped. Lights blacked out.")

    def _playback_loop_thread(self):
        """Background thread handling continuous execution according to the selected mode."""
        try:
            ser = self._get_serial_connection()
        except Exception as e:
            self.set_status(f"Serial error: {e}", is_error=True)
            self.stop_playback(silent=True)
            return

        # Determine playlist
        start_song = self.selected_song_var.get()
        if start_song in self.songs_list:
            idx = self.songs_list.index(start_song)
        else:
            idx = 0

        # Outer loop across songs / cycles
        while not self.stop_event.is_set():
            if not self.songs_list:
                break

            current_song_name = self.songs_list[idx]
            song_path = os.path.join(self.dmx_dir, current_song_name)
            self.root.after(0, self.selected_song_var.set, current_song_name)
            self.root.after(0, self.on_song_selected)

            # Ensure curve exists; auto-generate if missing
            curve_file = find_curve_for_song(song_path)
            if not curve_file or not os.path.isfile(curve_file):
                self.set_status(f"Generating curve for {current_song_name}...")
                try:
                    _, curve_file = analyze_song(song_path)
                    self.root.after(0, self.on_song_selected)
                except Exception as e:
                    self.set_status(f"Error analyzing {current_song_name}: {e}", is_error=True)
                    break

            try:
                curve = np.load(curve_file)
                self.set_status(f"Loading audio: {current_song_name}...")
                data, sr = librosa.load(song_path, sr=None, mono=False)
                if data.ndim == 2:
                    data = data.T
            except Exception as e:
                self.set_status(f"Failed to load {current_song_name}: {e}", is_error=True)
                break

            total_frames = len(curve)
            self.set_status(f"Streaming {current_song_name} ({total_frames} frames) to {ser.port}")

            # Sub-loop for "single" mode repeats without re-reading from disk
            while not self.stop_event.is_set():
                self._send_dmx_frame(0, 0, 0, 0)
                try:
                    sd.play(data, sr)
                except Exception as e:
                    self.set_status(f"Audio playback error: {e}", is_error=True)
                    return

                t0 = time.perf_counter()
                last_frame = -1

                # Frame-by-frame streaming loop at 40 FPS
                while not self.stop_event.is_set():
                    i = int((time.perf_counter() - t0 - LIGHT_DELAY) * FPS)
                    if i >= total_frames:
                        break
                    if i != last_frame and i >= 0:
                        r, g, b, m = curve[i]
                        self._send_dmx_frame(r, g, b, m)
                        self.root.after(0, self.update_visualizer, int(r), int(g), int(b), int(m), i, total_frames)
                        last_frame = i
                    time.sleep(0.002)

                if self.stop_event.is_set():
                    break

                # Wait for any tail audio
                try:
                    sd.wait()
                except Exception:
                    pass

                # If mode is "once", stop after this single iteration
                if self.current_mode == "once":
                    break
                # If mode is "all", advance to next song in playlist
                elif self.current_mode == "all":
                    break
                # If mode is "single", repeats the while loop

            if self.stop_event.is_set() or self.current_mode == "once":
                break

            if self.current_mode == "all":
                idx = (idx + 1) % len(self.songs_list)

        # Finished or stopped
        self._send_dmx_frame(0, 0, 0, 0)
        self.root.after(0, self.update_visualizer, 0, 0, 0, 0, 0, 1)
        self.is_playing = False
        self.set_mode_badge("STOPPED", self.c_text_muted)
        self.set_status("Playback finished.")

    # ---- HELPERS & CLEANUP ----
    def set_status(self, text: str, is_error: bool = False):
        color = self.c_red if is_error else self.c_text_muted
        self.status_var.set(text)
        try:
            self.status_lbl.config(fg=color)
        except Exception:
            pass

    def set_mode_badge(self, text: str, color: str):
        self.mode_badge_var.set(text)
        try:
            self.mode_badge_lbl.config(fg=color)
        except Exception:
            pass

    def on_closing(self):
        """Cleanup upon closing window."""
        self.stop_playback(silent=True)
        with self.ser_lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.close()
                except Exception:
                    pass
        self.root.destroy()


def main():
    root = tk.Tk()
    app = DMXMusicControllerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
