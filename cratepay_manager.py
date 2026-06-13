"""
CratePay Manager

A professional Tkinter desktop application for farm crate tracking and payroll.

Core technologies:
- Tkinter / ttk for the desktop interface
- SQLite for local storage
- OpenCV + Tesseract OCR for record-sheet scanning
- Pandas for tabular report exports

Run:
    python cratepay_manager.py
"""

from __future__ import annotations

import csv
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
import pytesseract
from PIL import Image, ImageTk
from tkinter import (
    BOTH,
    BOTTOM,
    END,
    LEFT,
    RIGHT,
    TOP,
    X,
    Y,
    BooleanVar,
    IntVar,
    StringVar,
    Tk,
    Toplevel,
    filedialog,
    messagebox,
)
from tkinter import ttk

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False


APP_NAME = "CratePay Manager"
RATE_PER_CRATE = 15
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
DAY_COLUMNS = {
    "Monday": "monday",
    "Tuesday": "tuesday",
    "Wednesday": "wednesday",
    "Thursday": "thursday",
    "Friday": "friday",
}

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
BACKUP_DIR = BASE_DIR / "backups"
EXPORT_DIR = BASE_DIR / "exports"
DB_PATH = DATA_DIR / "cratepay.db"


def money(value: float) -> str:
    return f"R{value:,.2f}"


def today_day_name() -> str:
    name = date.today().strftime("%A")
    return name if name in DAYS else "Monday"


@dataclass
class Worker:
    id: int
    name: str
    debt_amount: float
    date_added: str
    active: int


class Database:
    def __init__(self, db_path: Path = DB_PATH) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        BACKUP_DIR.mkdir(exist_ok=True)
        EXPORT_DIR.mkdir(exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.setup()

    def setup(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA foreign_keys = ON")
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS Workers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                debt_amount REAL NOT NULL DEFAULT 0,
                date_added TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS DailyCrates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id INTEGER NOT NULL,
                week_start TEXT NOT NULL,
                monday INTEGER NOT NULL DEFAULT 0,
                tuesday INTEGER NOT NULL DEFAULT 0,
                wednesday INTEGER NOT NULL DEFAULT 0,
                thursday INTEGER NOT NULL DEFAULT 0,
                friday INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                UNIQUE(worker_id, week_start),
                FOREIGN KEY(worker_id) REFERENCES Workers(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS Payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                worker_id INTEGER NOT NULL,
                week_start TEXT NOT NULL,
                total_crates INTEGER NOT NULL,
                gross_pay REAL NOT NULL,
                debt_deducted REAL NOT NULL,
                net_pay REAL NOT NULL,
                paid_at TEXT NOT NULL,
                FOREIGN KEY(worker_id) REFERENCES Workers(id) ON DELETE CASCADE
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS AuditLog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                details TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS Settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.conn.commit()
        self.set_default_settings()

    def set_default_settings(self) -> None:
        defaults = {
            "dark_mode": "0",
            "tesseract_path": "",
            "last_week_start": self.current_week_start(),
        }
        for key, value in defaults.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO Settings(key, value) VALUES (?, ?)", (key, value)
            )
        self.conn.commit()

    @staticmethod
    def current_week_start() -> str:
        today = date.today()
        monday = today.fromordinal(today.toordinal() - today.weekday())
        return monday.isoformat()

    def backup(self) -> Optional[Path]:
        if not self.db_path.exists():
            return None
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = BACKUP_DIR / f"cratepay_backup_{stamp}.db"
        shutil.copy2(self.db_path, target)
        return target

    def log(self, action: str, details: str) -> None:
        self.conn.execute(
            "INSERT INTO AuditLog(action, details, created_at) VALUES (?, ?, ?)",
            (action, details, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.conn.execute("SELECT value FROM Settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO Settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.conn.commit()

    def add_worker(self, name: str, debt: float) -> None:
        self.conn.execute(
            "INSERT INTO Workers(name, debt_amount, date_added, active) VALUES (?, ?, ?, 1)",
            (name.strip(), debt, date.today().isoformat()),
        )
        self.conn.commit()
        self.log("ADD_WORKER", f"Added worker {name} with debt {debt}")

    def update_worker(self, worker_id: int, name: str, debt: float) -> None:
        self.conn.execute(
            "UPDATE Workers SET name = ?, debt_amount = ? WHERE id = ?",
            (name.strip(), debt, worker_id),
        )
        self.conn.commit()
        self.log("EDIT_WORKER", f"Edited worker #{worker_id}: {name}, debt {debt}")

    def delete_worker(self, worker_id: int) -> None:
        row = self.conn.execute("SELECT name FROM Workers WHERE id = ?", (worker_id,)).fetchone()
        self.conn.execute("DELETE FROM Workers WHERE id = ?", (worker_id,))
        self.conn.commit()
        self.log("DELETE_WORKER", f"Deleted worker {row['name'] if row else worker_id}")

    def workers(self, search: str = "") -> List[sqlite3.Row]:
        if search:
            return self.conn.execute(
                """
                SELECT * FROM Workers
                WHERE name LIKE ?
                ORDER BY name
                """,
                (f"%{search.strip()}%",),
            ).fetchall()
        return self.conn.execute("SELECT * FROM Workers ORDER BY name").fetchall()

    def worker_by_name(self, name: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM Workers WHERE lower(name) = lower(?)", (name.strip(),)
        ).fetchone()

    def ensure_crate_row(self, worker_id: int, week_start: str) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO DailyCrates(worker_id, week_start, updated_at)
            VALUES (?, ?, ?)
            """,
            (worker_id, week_start, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def set_crates(self, worker_id: int, week_start: str, day: str, crates: int) -> None:
        if day not in DAY_COLUMNS:
            raise ValueError("Invalid day selected.")
        column = DAY_COLUMNS[day]
        self.ensure_crate_row(worker_id, week_start)
        self.conn.execute(
            f"UPDATE DailyCrates SET {column} = ?, updated_at = ? WHERE worker_id = ? AND week_start = ?",
            (int(crates), datetime.now().isoformat(timespec="seconds"), worker_id, week_start),
        )
        self.conn.commit()
        self.log("SET_CRATES", f"Worker #{worker_id}: {day} = {crates} for week {week_start}")

    def add_crates(self, worker_id: int, week_start: str, day: str, crates: int) -> None:
        if day not in DAY_COLUMNS:
            raise ValueError("Invalid day selected.")
        column = DAY_COLUMNS[day]
        self.ensure_crate_row(worker_id, week_start)
        self.conn.execute(
            f"""
            UPDATE DailyCrates
            SET {column} = {column} + ?, updated_at = ?
            WHERE worker_id = ? AND week_start = ?
            """,
            (int(crates), datetime.now().isoformat(timespec="seconds"), worker_id, week_start),
        )
        self.conn.commit()
        self.log("ADD_CRATES", f"Worker #{worker_id}: added {crates} to {day} for week {week_start}")

    def weekly_rows(self, week_start: str, search: str = "") -> List[sqlite3.Row]:
        params: List[object] = [week_start]
        where = ""
        if search:
            where = "WHERE w.name LIKE ?"
            params.append(f"%{search.strip()}%")
        return self.conn.execute(
            f"""
            SELECT
                w.id AS worker_id,
                w.name,
                w.debt_amount,
                w.date_added,
                COALESCE(d.monday, 0) AS monday,
                COALESCE(d.tuesday, 0) AS tuesday,
                COALESCE(d.wednesday, 0) AS wednesday,
                COALESCE(d.thursday, 0) AS thursday,
                COALESCE(d.friday, 0) AS friday
            FROM Workers w
            LEFT JOIN DailyCrates d
                ON d.worker_id = w.id AND d.week_start = ?
            {where}
            ORDER BY w.name
            """,
            params,
        ).fetchall()

    def payment_for_worker(self, worker_id: int, week_start: str) -> Dict[str, float]:
        row = self.conn.execute(
            """
            SELECT w.debt_amount,
                   COALESCE(d.monday, 0) AS monday,
                   COALESCE(d.tuesday, 0) AS tuesday,
                   COALESCE(d.wednesday, 0) AS wednesday,
                   COALESCE(d.thursday, 0) AS thursday,
                   COALESCE(d.friday, 0) AS friday
            FROM Workers w
            LEFT JOIN DailyCrates d ON d.worker_id = w.id AND d.week_start = ?
            WHERE w.id = ?
            """,
            (week_start, worker_id),
        ).fetchone()
        if not row:
            return {"total": 0, "gross": 0, "debt": 0, "net": 0}
        total = sum(int(row[col]) for col in DAY_COLUMNS.values())
        gross = total * RATE_PER_CRATE
        debt = float(row["debt_amount"])
        return {"total": total, "gross": gross, "debt": debt, "net": gross - debt}

    def save_payment(self, worker_id: int, week_start: str) -> None:
        pay = self.payment_for_worker(worker_id, week_start)
        self.conn.execute(
            """
            INSERT INTO Payments(worker_id, week_start, total_crates, gross_pay, debt_deducted, net_pay, paid_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                worker_id,
                week_start,
                int(pay["total"]),
                pay["gross"],
                pay["debt"],
                pay["net"],
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()
        self.log("SAVE_PAYMENT", f"Saved payment for worker #{worker_id}, week {week_start}")

    def audit_rows(self, limit: int = 200) -> List[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM AuditLog ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


class OCRScanner:
    def __init__(self, db: Database) -> None:
        self.db = db

    def configure_tesseract(self) -> None:
        configured = self.db.get_setting("tesseract_path", "")
        if configured and Path(configured).exists():
            pytesseract.pytesseract.tesseract_cmd = configured

    def preprocess(self, image_path: Path) -> Tuple[object, object]:
        image = cv2.imread(str(image_path))
        if image is None:
            raise ValueError("The selected image could not be opened.")
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return image, thresh

    def read_text(self, image_path: Path) -> str:
        self.configure_tesseract()
        _, processed = self.preprocess(image_path)
        config = "--psm 6"
        return pytesseract.image_to_string(processed, config=config)

    def detect_day(self, text: str, default_day: str) -> str:
        text_lower = text.lower()
        for day in DAYS:
            if day.lower() in text_lower:
                return day
        return default_day

    def count_crosses_in_line(self, line: str) -> int:
        cleaned = line.upper()
        direct = len(re.findall(r"X", cleaned))
        # Tesseract often reads quick crosses as x, +, *, or multiplication signs.
        substitutes = len(re.findall(r"[+*×]", cleaned))
        grouped_marks = re.findall(r"\b[IXx+*×]{2,}\b", line)
        grouped_total = sum(sum(1 for ch in group.upper() if ch in "X+*×") for group in grouped_marks)
        return max(direct + substitutes, grouped_total)

    def parse(self, image_path: Path, default_day: str) -> Tuple[str, List[Tuple[str, int, str]]]:
        text = self.read_text(image_path)
        if not text.strip():
            raise ValueError("No readable text was found in the image.")
        day = self.detect_day(text, default_day)
        known_workers = self.db.workers()
        results: List[Tuple[str, int, str]] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            crosses = self.count_crosses_in_line(line)
            if crosses <= 0:
                continue
            best_name = self.match_worker_name(line, known_workers)
            if best_name:
                results.append((best_name, crosses, line))
            else:
                guessed = re.sub(r"[Xx+*×:;|0-9]+", " ", line).strip()
                if guessed:
                    results.append((guessed, crosses, line))
        if not results:
            raise ValueError("No worker lines with X marks were found.")
        return day, results

    def match_worker_name(self, line: str, workers: Iterable[sqlite3.Row]) -> Optional[str]:
        line_lower = line.lower()
        best: Optional[str] = None
        best_len = 0
        for worker in workers:
            name = worker["name"]
            if name.lower() in line_lower and len(name) > best_len:
                best = name
                best_len = len(name)
        return best

    def capture_from_webcam(self, target_path: Path) -> Path:
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            raise ValueError("No webcam could be opened.")
        messagebox.showinfo(
            "Camera Capture",
            "Camera preview is opening. Press SPACE to capture or ESC to cancel.",
        )
        captured = False
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            cv2.imshow("CratePay Camera - SPACE to capture, ESC to cancel", frame)
            key = cv2.waitKey(1) & 0xFF
            if key == 27:
                break
            if key == 32:
                cv2.imwrite(str(target_path), frame)
                captured = True
                break
        cap.release()
        cv2.destroyAllWindows()
        if not captured:
            raise ValueError("Camera capture was cancelled.")
        return target_path


class CratePayApp(Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1220x760")
        self.minsize(1040, 680)
        self.db = Database()
        self.scanner = OCRScanner(self.db)
        self.week_start = StringVar(value=self.db.current_week_start())
        self.selected_worker_id: Optional[int] = None
        self.dark_mode = BooleanVar(value=self.db.get_setting("dark_mode") == "1")
        self.status_text = StringVar(value="Ready")
        self._image_preview_ref: Optional[ImageTk.PhotoImage] = None

        self.setup_style()
        self.create_layout()
        self.db.backup()
        self.refresh_all()

    def setup_style(self) -> None:
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self.apply_theme()

    def apply_theme(self) -> None:
        dark = self.dark_mode.get()
        bg = "#111827" if dark else "#f4f6f8"
        panel = "#1f2937" if dark else "#ffffff"
        text = "#f9fafb" if dark else "#111827"
        muted = "#9ca3af" if dark else "#4b5563"
        accent = "#1d9a8a"
        danger = "#c2410c"

        self.configure(bg=bg)
        self.style.configure(".", background=bg, foreground=text, fieldbackground=panel)
        self.style.configure("TFrame", background=bg)
        self.style.configure("Panel.TFrame", background=panel)
        self.style.configure("TLabel", background=bg, foreground=text, font=("Segoe UI", 10))
        self.style.configure("Panel.TLabel", background=panel, foreground=text)
        self.style.configure("Muted.TLabel", background=bg, foreground=muted)
        self.style.configure("Title.TLabel", font=("Segoe UI Semibold", 18), background=bg, foreground=text)
        self.style.configure("Metric.TLabel", font=("Segoe UI Semibold", 20), background=panel, foreground=text)
        self.style.configure("MetricName.TLabel", font=("Segoe UI", 9), background=panel, foreground=muted)
        self.style.configure("TButton", font=("Segoe UI Semibold", 10), padding=(12, 8))
        self.style.configure("Accent.TButton", background=accent, foreground="white")
        self.style.map("Accent.TButton", background=[("active", "#157c70")])
        self.style.configure("Danger.TButton", background=danger, foreground="white")
        self.style.map("Danger.TButton", background=[("active", "#9a3412")])
        self.style.configure("Treeview", rowheight=30, font=("Segoe UI", 10), background=panel, fieldbackground=panel, foreground=text)
        self.style.configure("Treeview.Heading", font=("Segoe UI Semibold", 10))
        self.style.map("Treeview", background=[("selected", accent)], foreground=[("selected", "white")])

    def create_layout(self) -> None:
        root = ttk.Frame(self)
        root.pack(fill=BOTH, expand=True)

        sidebar = ttk.Frame(root, style="Panel.TFrame", width=210)
        sidebar.pack(side=LEFT, fill=Y)
        sidebar.pack_propagate(False)

        ttk.Label(sidebar, text="CratePay", style="Panel.TLabel", font=("Segoe UI Semibold", 22)).pack(
            anchor="w", padx=18, pady=(20, 2)
        )
        ttk.Label(sidebar, text="Farm payroll manager", style="Panel.TLabel").pack(
            anchor="w", padx=18, pady=(0, 22)
        )

        self.nav_buttons: Dict[str, ttk.Button] = {}
        for page in ["Dashboard", "Workers", "Daily Records", "Scan Sheet", "Reports", "Settings"]:
            btn = ttk.Button(sidebar, text=page, command=lambda p=page: self.show_page(p))
            btn.pack(fill=X, padx=14, pady=5)
            self.nav_buttons[page] = btn

        ttk.Separator(sidebar).pack(fill=X, padx=14, pady=18)
        ttk.Label(sidebar, text="Week Start", style="Panel.TLabel").pack(anchor="w", padx=18)
        week_entry = ttk.Entry(sidebar, textvariable=self.week_start)
        week_entry.pack(fill=X, padx=14, pady=(5, 8))
        ttk.Button(sidebar, text="Refresh Week", command=self.refresh_all).pack(fill=X, padx=14)

        main = ttk.Frame(root)
        main.pack(side=RIGHT, fill=BOTH, expand=True)

        status = ttk.Label(main, textvariable=self.status_text, anchor="w")
        status.pack(side=BOTTOM, fill=X, padx=16, pady=(0, 8))

        self.pages_container = ttk.Frame(main)
        self.pages_container.pack(side=TOP, fill=BOTH, expand=True, padx=18, pady=18)

        self.pages: Dict[str, ttk.Frame] = {}
        self.create_dashboard_page()
        self.create_workers_page()
        self.create_daily_page()
        self.create_scan_page()
        self.create_reports_page()
        self.create_settings_page()
        self.show_page("Dashboard")

    def page(self, name: str) -> ttk.Frame:
        frame = ttk.Frame(self.pages_container)
        frame.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.pages[name] = frame
        return frame

    def show_page(self, name: str) -> None:
        self.pages[name].tkraise()
        self.status_text.set(f"{name} page")

    def metric_card(self, parent: ttk.Frame, title: str, value_var: StringVar) -> ttk.Frame:
        card = ttk.Frame(parent, style="Panel.TFrame", padding=16)
        ttk.Label(card, text=title, style="MetricName.TLabel").pack(anchor="w")
        ttk.Label(card, textvariable=value_var, style="Metric.TLabel").pack(anchor="w", pady=(8, 0))
        return card

    def create_dashboard_page(self) -> None:
        frame = self.page("Dashboard")
        ttk.Label(frame, text="Dashboard", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frame, text="Today's and weekly farm payroll summary", style="Muted.TLabel").pack(anchor="w", pady=(2, 14))

        self.metric_vars = {
            "workers": StringVar(),
            "crates": StringVar(),
            "gross": StringVar(),
            "debt": StringVar(),
            "net": StringVar(),
            "weekly_crates": StringVar(),
            "weekly_payable": StringVar(),
        }
        grid = ttk.Frame(frame)
        grid.pack(fill=X)
        for i in range(5):
            grid.columnconfigure(i, weight=1, uniform="metric")
        cards = [
            ("Total Workers", self.metric_vars["workers"]),
            ("Total Crates", self.metric_vars["crates"]),
            ("Gross Pay", self.metric_vars["gross"]),
            ("Total Debt", self.metric_vars["debt"]),
            ("Net Pay", self.metric_vars["net"]),
        ]
        for i, (title, var) in enumerate(cards):
            self.metric_card(grid, title, var).grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0), pady=4)

        weekly = ttk.Frame(frame, style="Panel.TFrame", padding=16)
        weekly.pack(fill=X, pady=18)
        ttk.Label(weekly, text="Weekly Summary", style="Panel.TLabel", font=("Segoe UI Semibold", 13)).pack(anchor="w")
        ttk.Label(weekly, textvariable=self.metric_vars["weekly_crates"], style="Panel.TLabel").pack(anchor="w", pady=(10, 2))
        ttk.Label(weekly, textvariable=self.metric_vars["weekly_payable"], style="Panel.TLabel").pack(anchor="w")

        self.dashboard_tree = self.make_tree(
            frame,
            ("Worker", "Mon", "Tue", "Wed", "Thu", "Fri", "Total", "Gross", "Debt", "Pay"),
            stretch_first=True,
        )
        self.dashboard_tree.pack(fill=BOTH, expand=True, pady=(4, 0))

    def create_workers_page(self) -> None:
        frame = self.page("Workers")
        ttk.Label(frame, text="Workers", style="Title.TLabel").pack(anchor="w")
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill=X, pady=(10, 12))
        self.worker_search = StringVar()
        search = ttk.Entry(toolbar, textvariable=self.worker_search)
        search.pack(side=LEFT, fill=X, expand=True, padx=(0, 8))
        search.bind("<KeyRelease>", lambda _event: self.refresh_workers())
        ttk.Button(toolbar, text="Search", command=self.refresh_workers).pack(side=LEFT, padx=4)
        ttk.Button(toolbar, text="Add Worker", style="Accent.TButton", command=self.add_worker_dialog).pack(side=LEFT, padx=4)
        ttk.Button(toolbar, text="Edit", command=self.edit_worker_dialog).pack(side=LEFT, padx=4)
        ttk.Button(toolbar, text="Delete", style="Danger.TButton", command=self.delete_worker).pack(side=LEFT, padx=4)
        ttk.Button(toolbar, text="History", command=self.worker_history_dialog).pack(side=LEFT, padx=4)

        self.workers_tree = self.make_tree(frame, ("ID", "Worker Name", "Debt", "Date Added"), stretch_first=False)
        self.workers_tree.pack(fill=BOTH, expand=True)
        self.workers_tree.column("ID", width=70, anchor="center")
        self.workers_tree.column("Debt", width=120, anchor="e")

    def create_daily_page(self) -> None:
        frame = self.page("Daily Records")
        ttk.Label(frame, text="Daily Records", style="Title.TLabel").pack(anchor="w")
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill=X, pady=(10, 12))
        self.daily_search = StringVar()
        daily_search = ttk.Entry(toolbar, textvariable=self.daily_search)
        daily_search.pack(side=LEFT, fill=X, expand=True, padx=(0, 8))
        daily_search.bind("<KeyRelease>", lambda _event: self.refresh_daily())
        ttk.Button(toolbar, text="Manual Entry", style="Accent.TButton", command=self.manual_entry_dialog).pack(side=LEFT, padx=4)
        ttk.Button(toolbar, text="Save Payment", command=self.save_selected_payment).pack(side=LEFT, padx=4)
        ttk.Button(toolbar, text="Refresh", command=self.refresh_daily).pack(side=LEFT, padx=4)

        self.daily_tree = self.make_tree(
            frame,
            ("Worker", "Mon", "Tue", "Wed", "Thu", "Fri", "Total", "Gross Pay", "Debt", "Amount To Pay"),
            stretch_first=True,
        )
        self.daily_tree.pack(fill=BOTH, expand=True)
        self.daily_tree.tag_configure("debt", background="#fff1e6")

    def create_scan_page(self) -> None:
        frame = self.page("Scan Sheet")
        ttk.Label(frame, text="Scan Record Sheet", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            frame,
            text="Upload a record-sheet image or capture one with the webcam, then review the results before saving.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(2, 14))

        toolbar = ttk.Frame(frame)
        toolbar.pack(fill=X, pady=(0, 12))
        self.scan_day = StringVar(value=today_day_name())
        ttk.Label(toolbar, text="Default Day").pack(side=LEFT)
        ttk.Combobox(toolbar, textvariable=self.scan_day, values=DAYS, state="readonly", width=14).pack(side=LEFT, padx=(8, 14))
        ttk.Button(toolbar, text="Upload Image", style="Accent.TButton", command=self.scan_upload_image).pack(side=LEFT, padx=4)
        ttk.Button(toolbar, text="Use Webcam", command=self.scan_webcam).pack(side=LEFT, padx=4)
        ttk.Button(toolbar, text="Manual Entry", command=self.manual_entry_dialog).pack(side=LEFT, padx=4)
        ttk.Button(toolbar, text="Save Preview", style="Accent.TButton", command=self.save_scan_preview).pack(side=LEFT, padx=4)

        split = ttk.PanedWindow(frame, orient="horizontal")
        split.pack(fill=BOTH, expand=True)
        left = ttk.Frame(split, style="Panel.TFrame", padding=10)
        right = ttk.Frame(split, padding=0)
        split.add(left, weight=1)
        split.add(right, weight=2)

        self.preview_label = ttk.Label(left, text="No image selected", style="Panel.TLabel", anchor="center")
        self.preview_label.pack(fill=BOTH, expand=True)

        self.scan_tree = self.make_tree(right, ("Worker", "Crates", "Day", "Source Line"), stretch_first=True)
        self.scan_tree.pack(fill=BOTH, expand=True)
        self.scan_tree.bind("<Double-1>", lambda _event: self.edit_scan_row_dialog())

    def create_reports_page(self) -> None:
        frame = self.page("Reports")
        ttk.Label(frame, text="Reports", style="Title.TLabel").pack(anchor="w")
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill=X, pady=(10, 12))
        ttk.Button(toolbar, text="Individual Worker Report", command=self.individual_report_dialog).pack(side=LEFT, padx=4)
        ttk.Button(toolbar, text="Weekly Farm Report", command=self.show_weekly_report).pack(side=LEFT, padx=4)
        ttk.Button(toolbar, text="Export CSV", command=lambda: self.export_report("csv")).pack(side=LEFT, padx=4)
        ttk.Button(toolbar, text="Export Excel", command=lambda: self.export_report("xlsx")).pack(side=LEFT, padx=4)
        ttk.Button(toolbar, text="Export PDF", command=lambda: self.export_report("pdf")).pack(side=LEFT, padx=4)
        ttk.Button(toolbar, text="Payment Slips", style="Accent.TButton", command=self.export_payment_slips).pack(side=LEFT, padx=4)

        self.report_tree = self.make_tree(
            frame,
            ("Worker", "Mon", "Tue", "Wed", "Thu", "Fri", "Total", "Gross", "Debt", "Final Payment"),
            stretch_first=True,
        )
        self.report_tree.pack(fill=BOTH, expand=True)

    def create_settings_page(self) -> None:
        frame = self.page("Settings")
        ttk.Label(frame, text="Settings", style="Title.TLabel").pack(anchor="w")

        panel = ttk.Frame(frame, style="Panel.TFrame", padding=18)
        panel.pack(fill=X, pady=(12, 16))
        self.tesseract_path = StringVar(value=self.db.get_setting("tesseract_path", ""))
        ttk.Label(panel, text="Tesseract executable path", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(panel, textvariable=self.tesseract_path).grid(row=1, column=0, sticky="ew", pady=(6, 12))
        panel.columnconfigure(0, weight=1)
        ttk.Button(panel, text="Browse", command=self.browse_tesseract).grid(row=1, column=1, padx=(8, 0), pady=(6, 12))
        ttk.Checkbutton(panel, text="Dark mode", variable=self.dark_mode, command=self.toggle_dark_mode).grid(row=2, column=0, sticky="w")
        ttk.Button(panel, text="Save Settings", style="Accent.TButton", command=self.save_settings).grid(row=3, column=0, sticky="w", pady=(14, 0))
        ttk.Button(panel, text="Backup Database Now", command=self.backup_now).grid(row=3, column=1, sticky="e", pady=(14, 0))

        ttk.Label(frame, text="Audit Log", style="Title.TLabel").pack(anchor="w", pady=(12, 8))
        self.audit_tree = self.make_tree(frame, ("Time", "Action", "Details"), stretch_first=False)
        self.audit_tree.pack(fill=BOTH, expand=True)
        self.audit_tree.column("Time", width=170)
        self.audit_tree.column("Action", width=150)

    def make_tree(self, parent: ttk.Frame, columns: Tuple[str, ...], stretch_first: bool = False) -> ttk.Treeview:
        tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="browse")
        for i, col in enumerate(columns):
            tree.heading(col, text=col)
            tree.column(col, width=160 if i == 0 else 95, anchor="w" if i == 0 else "center", stretch=stretch_first and i == 0)
        ybar = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        xbar = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        tree.pack(side=TOP, fill=BOTH, expand=True)
        ybar.pack(side=RIGHT, fill=Y)
        xbar.pack(side=BOTTOM, fill=X)
        return tree

    def refresh_all(self) -> None:
        self.refresh_dashboard()
        self.refresh_workers()
        self.refresh_daily()
        self.show_weekly_report()
        self.refresh_audit()

    def weekly_dataframe(self) -> pd.DataFrame:
        rows = self.db.weekly_rows(self.week_start.get())
        data = []
        for row in rows:
            total = sum(int(row[col]) for col in DAY_COLUMNS.values())
            gross = total * RATE_PER_CRATE
            debt = float(row["debt_amount"])
            data.append(
                {
                    "Worker": row["name"],
                    "Monday": int(row["monday"]),
                    "Tuesday": int(row["tuesday"]),
                    "Wednesday": int(row["wednesday"]),
                    "Thursday": int(row["thursday"]),
                    "Friday": int(row["friday"]),
                    "Total Crates": total,
                    "Gross Pay": gross,
                    "Debt": debt,
                    "Amount To Pay": gross - debt,
                }
            )
        return pd.DataFrame(data)

    def refresh_dashboard(self) -> None:
        df = self.weekly_dataframe()
        workers = len(df)
        total_crates = int(df["Total Crates"].sum()) if not df.empty else 0
        gross = float(df["Gross Pay"].sum()) if not df.empty else 0
        debt = float(df["Debt"].sum()) if not df.empty else 0
        net = float(df["Amount To Pay"].sum()) if not df.empty else 0
        self.metric_vars["workers"].set(str(workers))
        self.metric_vars["crates"].set(str(total_crates))
        self.metric_vars["gross"].set(money(gross))
        self.metric_vars["debt"].set(money(debt))
        self.metric_vars["net"].set(money(net))
        self.metric_vars["weekly_crates"].set(f"Total crates Monday-Friday: {total_crates}")
        self.metric_vars["weekly_payable"].set(f"Total amount payable: {money(net)}")
        self.fill_pay_tree(self.dashboard_tree, self.db.weekly_rows(self.week_start.get()))

    def refresh_workers(self) -> None:
        self.clear_tree(self.workers_tree)
        for row in self.db.workers(self.worker_search.get() if hasattr(self, "worker_search") else ""):
            self.workers_tree.insert("", END, values=(row["id"], row["name"], money(row["debt_amount"]), row["date_added"]))

    def refresh_daily(self) -> None:
        self.fill_pay_tree(self.daily_tree, self.db.weekly_rows(self.week_start.get(), self.daily_search.get() if hasattr(self, "daily_search") else ""))

    def refresh_audit(self) -> None:
        self.clear_tree(self.audit_tree)
        for row in self.db.audit_rows():
            self.audit_tree.insert("", END, values=(row["created_at"], row["action"], row["details"]))

    def fill_pay_tree(self, tree: ttk.Treeview, rows: List[sqlite3.Row]) -> None:
        self.clear_tree(tree)
        for row in rows:
            total = sum(int(row[col]) for col in DAY_COLUMNS.values())
            gross = total * RATE_PER_CRATE
            debt = float(row["debt_amount"])
            net = gross - debt
            values = (
                row["name"],
                row["monday"],
                row["tuesday"],
                row["wednesday"],
                row["thursday"],
                row["friday"],
                total,
                money(gross),
                money(debt),
                money(net),
            )
            tags = ("debt",) if debt > 0 else ()
            tree.insert("", END, iid=str(row["worker_id"]), values=values, tags=tags)

    def clear_tree(self, tree: ttk.Treeview) -> None:
        for item in tree.get_children():
            tree.delete(item)

    def selected_worker_from_tree(self, tree: ttk.Treeview) -> Optional[int]:
        selected = tree.selection()
        if not selected:
            return None
        try:
            return int(selected[0])
        except ValueError:
            values = tree.item(selected[0], "values")
            return int(values[0]) if values else None

    def add_worker_dialog(self) -> None:
        self.worker_form_dialog("Add Worker")

    def edit_worker_dialog(self) -> None:
        selected = self.workers_tree.selection()
        if not selected:
            messagebox.showwarning("No worker selected", "Select a worker first.")
            return
        values = self.workers_tree.item(selected[0], "values")
        worker_id = int(values[0])
        row = self.db.conn.execute("SELECT * FROM Workers WHERE id = ?", (worker_id,)).fetchone()
        if row:
            self.worker_form_dialog("Edit Worker", row)

    def worker_form_dialog(self, title: str, worker: Optional[sqlite3.Row] = None) -> None:
        win = Toplevel(self)
        win.title(title)
        win.geometry("420x250")
        win.transient(self)
        win.grab_set()
        frame = ttk.Frame(win, padding=18)
        frame.pack(fill=BOTH, expand=True)
        name_var = StringVar(value=worker["name"] if worker else "")
        debt_var = StringVar(value=str(worker["debt_amount"]) if worker else "0")
        ttk.Label(frame, text="Worker Name").pack(anchor="w")
        ttk.Entry(frame, textvariable=name_var).pack(fill=X, pady=(4, 12))
        ttk.Label(frame, text="Debt Amount").pack(anchor="w")
        ttk.Entry(frame, textvariable=debt_var).pack(fill=X, pady=(4, 16))

        def save() -> None:
            name = name_var.get().strip()
            if not name:
                messagebox.showerror("Missing name", "Worker name is required.")
                return
            try:
                debt = float(debt_var.get())
                if debt < 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror("Invalid debt", "Debt must be a number of 0 or more.")
                return
            try:
                if worker:
                    self.db.update_worker(worker["id"], name, debt)
                else:
                    self.db.add_worker(name, debt)
            except sqlite3.IntegrityError:
                messagebox.showerror("Duplicate worker", "A worker with this name already exists.")
                return
            self.refresh_all()
            win.destroy()

        ttk.Button(frame, text="Save", style="Accent.TButton", command=save).pack(side=RIGHT)
        ttk.Button(frame, text="Cancel", command=win.destroy).pack(side=RIGHT, padx=8)

    def delete_worker(self) -> None:
        selected = self.workers_tree.selection()
        if not selected:
            messagebox.showwarning("No worker selected", "Select a worker first.")
            return
        values = self.workers_tree.item(selected[0], "values")
        worker_id = int(values[0])
        name = values[1]
        if not messagebox.askyesno("Delete Worker", f"Delete {name} and all related records?"):
            return
        self.db.delete_worker(worker_id)
        self.refresh_all()

    def worker_history_dialog(self) -> None:
        selected = self.workers_tree.selection()
        if not selected:
            messagebox.showwarning("No worker selected", "Select a worker first.")
            return
        worker_id = int(self.workers_tree.item(selected[0], "values")[0])
        worker = self.db.conn.execute("SELECT * FROM Workers WHERE id = ?", (worker_id,)).fetchone()
        rows = self.db.conn.execute(
            """
            SELECT * FROM DailyCrates
            WHERE worker_id = ?
            ORDER BY week_start DESC
            """,
            (worker_id,),
        ).fetchall()
        win = Toplevel(self)
        win.title(f"History - {worker['name']}")
        win.geometry("850x420")
        frame = ttk.Frame(win, padding=14)
        frame.pack(fill=BOTH, expand=True)
        tree = self.make_tree(frame, ("Week", "Mon", "Tue", "Wed", "Thu", "Fri", "Total"), stretch_first=False)
        for row in rows:
            total = sum(int(row[col]) for col in DAY_COLUMNS.values())
            tree.insert("", END, values=(row["week_start"], row["monday"], row["tuesday"], row["wednesday"], row["thursday"], row["friday"], total))

    def manual_entry_dialog(self) -> None:
        win = Toplevel(self)
        win.title("Manual Crate Entry")
        win.geometry("460x330")
        win.transient(self)
        win.grab_set()
        frame = ttk.Frame(win, padding=18)
        frame.pack(fill=BOTH, expand=True)
        workers = self.db.workers()
        if not workers:
            messagebox.showwarning("No workers", "Add workers before entering crates.")
            win.destroy()
            return
        worker_names = [w["name"] for w in workers]
        worker_var = StringVar(value=worker_names[0])
        day_var = StringVar(value=today_day_name())
        crates_var = IntVar(value=0)
        mode_var = StringVar(value="Set")
        ttk.Label(frame, text="Worker").pack(anchor="w")
        ttk.Combobox(frame, values=worker_names, textvariable=worker_var, state="readonly").pack(fill=X, pady=(4, 12))
        ttk.Label(frame, text="Day").pack(anchor="w")
        ttk.Combobox(frame, values=DAYS, textvariable=day_var, state="readonly").pack(fill=X, pady=(4, 12))
        ttk.Label(frame, text="Crates").pack(anchor="w")
        ttk.Spinbox(frame, from_=0, to=10000, textvariable=crates_var).pack(fill=X, pady=(4, 12))
        ttk.Label(frame, text="Mode").pack(anchor="w")
        ttk.Combobox(frame, values=["Set", "Add"], textvariable=mode_var, state="readonly").pack(fill=X, pady=(4, 18))

        def save() -> None:
            worker = self.db.worker_by_name(worker_var.get())
            if not worker:
                messagebox.showerror("Worker not found", "The selected worker no longer exists.")
                return
            crates = int(crates_var.get())
            if mode_var.get() == "Add":
                self.db.add_crates(worker["id"], self.week_start.get(), day_var.get(), crates)
            else:
                self.db.set_crates(worker["id"], self.week_start.get(), day_var.get(), crates)
            self.refresh_all()
            win.destroy()

        ttk.Button(frame, text="Save", style="Accent.TButton", command=save).pack(side=RIGHT)
        ttk.Button(frame, text="Cancel", command=win.destroy).pack(side=RIGHT, padx=8)

    def save_selected_payment(self) -> None:
        worker_id = self.selected_worker_from_tree(self.daily_tree)
        if not worker_id:
            messagebox.showwarning("No worker selected", "Select a worker in the daily records table.")
            return
        self.db.save_payment(worker_id, self.week_start.get())
        self.refresh_all()
        messagebox.showinfo("Payment saved", "Payment record has been saved.")

    def scan_upload_image(self) -> None:
        filename = filedialog.askopenfilename(
            title="Select record sheet image",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff"), ("All files", "*.*")],
        )
        if filename:
            self.process_scan(Path(filename))

    def scan_webcam(self) -> None:
        target = DATA_DIR / f"webcam_capture_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
        try:
            image_path = self.scanner.capture_from_webcam(target)
            self.process_scan(image_path)
        except Exception as exc:
            messagebox.showerror("Camera error", str(exc))

    def process_scan(self, image_path: Path) -> None:
        try:
            day, results = self.scanner.parse(image_path, self.scan_day.get())
            self.scan_day.set(day)
            self.show_image_preview(image_path)
            self.clear_tree(self.scan_tree)
            for name, crates, source in results:
                self.scan_tree.insert("", END, values=(name, crates, day, source))
            self.status_text.set(f"OCR found {len(results)} worker rows for {day}. Review before saving.")
        except Exception as exc:
            self.show_image_preview(image_path)
            messagebox.showwarning(
                "OCR needs review",
                f"{exc}\n\nYou can still use Manual Entry or add rows to the preview manually.",
            )

    def show_image_preview(self, image_path: Path) -> None:
        try:
            image = Image.open(image_path)
            image.thumbnail((420, 520))
            self._image_preview_ref = ImageTk.PhotoImage(image)
            self.preview_label.configure(image=self._image_preview_ref, text="")
        except Exception:
            self.preview_label.configure(text="Preview unavailable", image="")

    def edit_scan_row_dialog(self) -> None:
        selected = self.scan_tree.selection()
        if not selected:
            return
        item = selected[0]
        values = self.scan_tree.item(item, "values")
        win = Toplevel(self)
        win.title("Correct OCR Result")
        win.geometry("430x300")
        win.transient(self)
        win.grab_set()
        frame = ttk.Frame(win, padding=18)
        frame.pack(fill=BOTH, expand=True)
        worker_var = StringVar(value=values[0])
        crates_var = IntVar(value=int(values[1]))
        day_var = StringVar(value=values[2])
        source = values[3] if len(values) > 3 else ""
        ttk.Label(frame, text="Worker").pack(anchor="w")
        worker_names = [w["name"] for w in self.db.workers()]
        ttk.Combobox(frame, values=worker_names, textvariable=worker_var).pack(fill=X, pady=(4, 12))
        ttk.Label(frame, text="Crates").pack(anchor="w")
        ttk.Spinbox(frame, from_=0, to=10000, textvariable=crates_var).pack(fill=X, pady=(4, 12))
        ttk.Label(frame, text="Day").pack(anchor="w")
        ttk.Combobox(frame, values=DAYS, textvariable=day_var, state="readonly").pack(fill=X, pady=(4, 18))

        def save() -> None:
            self.scan_tree.item(item, values=(worker_var.get(), int(crates_var.get()), day_var.get(), source))
            win.destroy()

        ttk.Button(frame, text="Save", style="Accent.TButton", command=save).pack(side=RIGHT)
        ttk.Button(frame, text="Cancel", command=win.destroy).pack(side=RIGHT, padx=8)

    def save_scan_preview(self) -> None:
        rows = [self.scan_tree.item(item, "values") for item in self.scan_tree.get_children()]
        if not rows:
            messagebox.showwarning("No scan results", "There are no OCR results to save.")
            return
        missing = []
        saved = 0
        for name, crates, day, _source in rows:
            worker = self.db.worker_by_name(str(name))
            if not worker:
                missing.append(str(name))
                continue
            self.db.add_crates(worker["id"], self.week_start.get(), str(day), int(crates))
            saved += 1
        self.refresh_all()
        if missing:
            messagebox.showwarning(
                "Some workers not saved",
                f"Saved {saved} rows. These names were not found and were skipped:\n" + "\n".join(missing),
            )
        else:
            messagebox.showinfo("Scan saved", f"Saved {saved} scanned rows.")

    def show_weekly_report(self) -> None:
        self.fill_pay_tree(self.report_tree, self.db.weekly_rows(self.week_start.get()))

    def individual_report_dialog(self) -> None:
        workers = self.db.workers()
        if not workers:
            messagebox.showwarning("No workers", "Add workers before creating reports.")
            return
        win = Toplevel(self)
        win.title("Individual Worker Report")
        win.geometry("520x420")
        frame = ttk.Frame(win, padding=18)
        frame.pack(fill=BOTH, expand=True)
        worker_names = [w["name"] for w in workers]
        worker_var = StringVar(value=worker_names[0])
        ttk.Label(frame, text="Worker").pack(anchor="w")
        ttk.Combobox(frame, values=worker_names, textvariable=worker_var, state="readonly").pack(fill=X, pady=(4, 12))
        output = ttk.Treeview(frame, columns=("Item", "Value"), show="headings")
        output.heading("Item", text="Item")
        output.heading("Value", text="Value")
        output.pack(fill=BOTH, expand=True, pady=8)

        def load() -> None:
            for item in output.get_children():
                output.delete(item)
            worker = self.db.worker_by_name(worker_var.get())
            if not worker:
                return
            row = next((r for r in self.db.weekly_rows(self.week_start.get()) if r["worker_id"] == worker["id"]), None)
            if not row:
                return
            pay = self.db.payment_for_worker(worker["id"], self.week_start.get())
            report_items = [
                ("Monday crates", row["monday"]),
                ("Tuesday crates", row["tuesday"]),
                ("Wednesday crates", row["wednesday"]),
                ("Thursday crates", row["thursday"]),
                ("Friday crates", row["friday"]),
                ("Weekly total", int(pay["total"])),
                ("Gross pay", money(pay["gross"])),
                ("Debt deductions", money(pay["debt"])),
                ("Final payment", money(pay["net"])),
            ]
            for item in report_items:
                output.insert("", END, values=item)

        ttk.Button(frame, text="Load Report", style="Accent.TButton", command=load).pack(side=LEFT)
        load()

    def export_report(self, kind: str) -> None:
        df = self.weekly_dataframe()
        if df.empty:
            messagebox.showwarning("No data", "There is no report data to export.")
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        if kind == "csv":
            path = EXPORT_DIR / f"weekly_farm_report_{stamp}.csv"
            df.to_csv(path, index=False)
        elif kind == "xlsx":
            path = EXPORT_DIR / f"weekly_farm_report_{stamp}.xlsx"
            df.to_excel(path, index=False)
        elif kind == "pdf":
            if not REPORTLAB_AVAILABLE:
                messagebox.showerror("PDF unavailable", "Install reportlab to export PDF files.")
                return
            path = EXPORT_DIR / f"weekly_farm_report_{stamp}.pdf"
            self.dataframe_to_pdf(df, path, f"Weekly Farm Report - Week {self.week_start.get()}")
        else:
            return
        self.db.log("EXPORT_REPORT", f"Exported {kind.upper()} report to {path}")
        messagebox.showinfo("Export complete", f"Report exported to:\n{path}")

    def dataframe_to_pdf(self, df: pd.DataFrame, path: Path, title: str) -> None:
        doc = SimpleDocTemplate(str(path), pagesize=A4)
        styles = getSampleStyleSheet()
        data = [list(df.columns)] + df.astype(str).values.tolist()
        table = Table(data, repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1d9a8a")),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 7),
                    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f4f6f8")]),
                ]
            )
        )
        doc.build([Paragraph(title, styles["Title"]), Spacer(1, 12), table])

    def export_payment_slips(self) -> None:
        df = self.weekly_dataframe()
        if df.empty:
            messagebox.showwarning("No data", "There are no workers to create slips for.")
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = EXPORT_DIR / f"payment_slips_{stamp}.pdf"
        if not REPORTLAB_AVAILABLE:
            messagebox.showerror("PDF unavailable", "Install reportlab to export payment slips.")
            return
        styles = getSampleStyleSheet()
        doc = SimpleDocTemplate(str(path), pagesize=A4)
        story = [Paragraph(f"Payment Slips - Week {self.week_start.get()}", styles["Title"]), Spacer(1, 12)]
        for _, row in df.iterrows():
            slip_data = [
                ["Worker", row["Worker"]],
                ["Total crates", row["Total Crates"]],
                ["Gross pay", money(float(row["Gross Pay"]))],
                ["Debt deduction", money(float(row["Debt"]))],
                ["Final payment", money(float(row["Amount To Pay"]))],
            ]
            table = Table(slip_data, colWidths=[120, 260])
            table.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#edf2f7")),
                        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                    ]
                )
            )
            story.extend([table, Spacer(1, 16)])
        doc.build(story)
        self.db.log("EXPORT_SLIPS", f"Exported payment slips to {path}")
        messagebox.showinfo("Payment slips exported", f"Payment slips exported to:\n{path}")

    def browse_tesseract(self) -> None:
        filename = filedialog.askopenfilename(
            title="Select tesseract.exe",
            filetypes=[("Tesseract executable", "tesseract.exe"), ("All files", "*.*")],
        )
        if filename:
            self.tesseract_path.set(filename)

    def toggle_dark_mode(self) -> None:
        self.apply_theme()
        self.db.set_setting("dark_mode", "1" if self.dark_mode.get() else "0")

    def save_settings(self) -> None:
        path = self.tesseract_path.get().strip()
        if path and not Path(path).exists():
            messagebox.showerror("Invalid path", "The Tesseract path does not exist.")
            return
        self.db.set_setting("tesseract_path", path)
        self.db.set_setting("dark_mode", "1" if self.dark_mode.get() else "0")
        self.db.log("SAVE_SETTINGS", "Settings updated")
        messagebox.showinfo("Settings saved", "Settings have been saved.")

    def backup_now(self) -> None:
        target = self.db.backup()
        if target:
            self.db.log("BACKUP", f"Created backup {target}")
            messagebox.showinfo("Backup complete", f"Database backup created:\n{target}")


def main() -> None:
    app = CratePayApp()
    app.mainloop()


if __name__ == "__main__":
    main()
