"""
CratePay Manager - Streamlit Edition

Farm crate payroll manager using:
- Streamlit for the application interface
- SQLite for local data storage
- OpenCV and Tesseract OCR for record-sheet scanning
- Pandas for reporting and exports

Run:
    streamlit run streamlit_app.py
"""

from __future__ import annotations

import io
import re
import shutil
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

import cv2
import numpy as np
import pandas as pd
import pytesseract
import streamlit as st
from PIL import Image

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


def current_week_start() -> str:
    today = date.today()
    monday = today.fromordinal(today.toordinal() - today.weekday())
    return monday.isoformat()


def default_day() -> str:
    day = date.today().strftime("%A")
    return day if day in DAYS else "Monday"


class Database:
    def __init__(self, path: Path = DB_PATH) -> None:
        DATA_DIR.mkdir(exist_ok=True)
        BACKUP_DIR.mkdir(exist_ok=True)
        EXPORT_DIR.mkdir(exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
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
        defaults = {"tesseract_path": "", "dark_mode": "0"}
        for key, value in defaults.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO Settings(key, value) VALUES (?, ?)", (key, value)
            )
        self.conn.commit()

    def log(self, action: str, details: str) -> None:
        self.conn.execute(
            "INSERT INTO AuditLog(action, details, created_at) VALUES (?, ?, ?)",
            (action, details, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def backup(self) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = BACKUP_DIR / f"cratepay_backup_{stamp}.db"
        shutil.copy2(self.path, target)
        self.log("BACKUP", f"Created database backup {target.name}")
        return target

    def setting(self, key: str, default: str = "") -> str:
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

    def workers(self, search: str = "") -> list[sqlite3.Row]:
        if search:
            return self.conn.execute(
                "SELECT * FROM Workers WHERE name LIKE ? ORDER BY name", (f"%{search}%",)
            ).fetchall()
        return self.conn.execute("SELECT * FROM Workers ORDER BY name").fetchall()

    def worker_by_id(self, worker_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM Workers WHERE id = ?", (worker_id,)).fetchone()

    def worker_by_name(self, name: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM Workers WHERE lower(name) = lower(?)", (name.strip(),)
        ).fetchone()

    def add_worker(self, name: str, debt: float) -> None:
        self.conn.execute(
            "INSERT INTO Workers(name, debt_amount, date_added, active) VALUES (?, ?, ?, 1)",
            (name.strip(), debt, date.today().isoformat()),
        )
        self.conn.commit()
        self.log("ADD_WORKER", f"Added {name}")

    def update_worker(self, worker_id: int, name: str, debt: float) -> None:
        self.conn.execute(
            "UPDATE Workers SET name = ?, debt_amount = ? WHERE id = ?",
            (name.strip(), debt, worker_id),
        )
        self.conn.commit()
        self.log("EDIT_WORKER", f"Updated {name}")

    def delete_worker(self, worker_id: int) -> None:
        worker = self.worker_by_id(worker_id)
        self.conn.execute("DELETE FROM Workers WHERE id = ?", (worker_id,))
        self.conn.commit()
        self.log("DELETE_WORKER", f"Deleted {worker['name'] if worker else worker_id}")

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
            raise ValueError("Invalid day")
        column = DAY_COLUMNS[day]
        self.ensure_crate_row(worker_id, week_start)
        self.conn.execute(
            f"UPDATE DailyCrates SET {column} = ?, updated_at = ? WHERE worker_id = ? AND week_start = ?",
            (int(crates), datetime.now().isoformat(timespec="seconds"), worker_id, week_start),
        )
        self.conn.commit()
        self.log("SET_CRATES", f"Worker #{worker_id}: {day} set to {crates}")

    def add_crates(self, worker_id: int, week_start: str, day: str, crates: int) -> None:
        if day not in DAY_COLUMNS:
            raise ValueError("Invalid day")
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
        self.log("ADD_CRATES", f"Worker #{worker_id}: added {crates} to {day}")

    def weekly_rows(self, week_start: str, search: str = "") -> list[sqlite3.Row]:
        params: list[object] = [week_start]
        where = ""
        if search:
            where = "WHERE w.name LIKE ?"
            params.append(f"%{search}%")
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
            LEFT JOIN DailyCrates d ON d.worker_id = w.id AND d.week_start = ?
            {where}
            ORDER BY w.name
            """,
            params,
        ).fetchall()

    def worker_history(self, worker_id: int) -> pd.DataFrame:
        rows = self.conn.execute(
            "SELECT * FROM DailyCrates WHERE worker_id = ? ORDER BY week_start DESC", (worker_id,)
        ).fetchall()
        data = []
        for row in rows:
            total = sum(int(row[col]) for col in DAY_COLUMNS.values())
            data.append(
                {
                    "Week": row["week_start"],
                    "Mon": row["monday"],
                    "Tue": row["tuesday"],
                    "Wed": row["wednesday"],
                    "Thu": row["thursday"],
                    "Fri": row["friday"],
                    "Total": total,
                }
            )
        return pd.DataFrame(data)

    def save_payment(self, worker_id: int, week_start: str) -> None:
        row = next((r for r in self.weekly_rows(week_start) if r["worker_id"] == worker_id), None)
        if not row:
            raise ValueError("Worker has no weekly row")
        total = sum(int(row[col]) for col in DAY_COLUMNS.values())
        gross = total * RATE_PER_CRATE
        debt = float(row["debt_amount"])
        self.conn.execute(
            """
            INSERT INTO Payments(worker_id, week_start, total_crates, gross_pay, debt_deducted, net_pay, paid_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                worker_id,
                week_start,
                total,
                gross,
                debt,
                gross - debt,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()
        self.log("SAVE_PAYMENT", f"Saved payment for worker #{worker_id}")

    def audit_df(self) -> pd.DataFrame:
        rows = self.conn.execute(
            "SELECT created_at AS Time, action AS Action, details AS Details FROM AuditLog ORDER BY id DESC LIMIT 250"
        ).fetchall()
        return pd.DataFrame([dict(r) for r in rows])


class OCRScanner:
    def __init__(self, db: Database) -> None:
        self.db = db

    def configure(self) -> None:
        configured = self.db.setting("tesseract_path")
        if configured and Path(configured).exists():
            pytesseract.pytesseract.tesseract_cmd = configured

    def image_to_text(self, image: Image.Image) -> str:
        self.configure()
        rgb = image.convert("RGB")
        arr = cv2.cvtColor(np.array(rgb), cv2.COLOR_RGB2BGR)
        gray = cv2.cvtColor(arr, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (3, 3), 0)
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return pytesseract.image_to_string(thresh, config="--psm 6")

    def detect_day(self, text: str, fallback: str) -> str:
        lower = text.lower()
        for day in DAYS:
            if day.lower() in lower:
                return day
        return fallback

    def count_crosses(self, line: str) -> int:
        direct = len(re.findall(r"[Xx+*×]", line))
        grouped = re.findall(r"\b[IXx+*×]{2,}\b", line)
        grouped_total = sum(sum(1 for ch in group if ch in "Xx+*×") for group in grouped)
        return max(direct, grouped_total)

    def match_worker_name(self, line: str, workers: Iterable[sqlite3.Row]) -> Optional[str]:
        line_lower = line.lower()
        best = None
        best_len = 0
        for worker in workers:
            name = worker["name"]
            if name.lower() in line_lower and len(name) > best_len:
                best = name
                best_len = len(name)
        return best

    def parse(self, image: Image.Image, fallback_day: str) -> tuple[str, pd.DataFrame, str]:
        text = self.image_to_text(image)
        if not text.strip():
            raise ValueError("No readable text found. Use manual entry or try a clearer photo.")
        day = self.detect_day(text, fallback_day)
        workers = self.db.workers()
        results = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            crates = self.count_crosses(line)
            if crates <= 0:
                continue
            name = self.match_worker_name(line, workers)
            if not name:
                name = re.sub(r"[Xx+*×:;|0-9]+", " ", line).strip()
            if name:
                results.append({"Worker": name, "Crates": crates, "Day": day, "Source Line": line})
        if not results:
            raise ValueError("No worker rows with X marks were found.")
        return day, pd.DataFrame(results), text


@st.cache_resource
def get_db() -> Database:
    return Database()


def weekly_dataframe(db: Database, week_start: str, search: str = "") -> pd.DataFrame:
    rows = db.weekly_rows(week_start, search)
    data = []
    for row in rows:
        total = sum(int(row[col]) for col in DAY_COLUMNS.values())
        gross = total * RATE_PER_CRATE
        debt = float(row["debt_amount"])
        data.append(
            {
                "Worker ID": row["worker_id"],
                "Worker": row["name"],
                "Mon": int(row["monday"]),
                "Tue": int(row["tuesday"]),
                "Wed": int(row["wednesday"]),
                "Thu": int(row["thursday"]),
                "Fri": int(row["friday"]),
                "Total Crates": total,
                "Gross Pay": gross,
                "Debt": debt,
                "Amount To Pay": gross - debt,
            }
        )
    return pd.DataFrame(data)


def pdf_bytes(df: pd.DataFrame, title: str) -> bytes:
    if not REPORTLAB_AVAILABLE:
        raise RuntimeError("reportlab is not installed")
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    styles = getSampleStyleSheet()
    export_df = df.drop(columns=["Worker ID"], errors="ignore").copy()
    data = [list(export_df.columns)] + export_df.astype(str).values.tolist()
    table = Table(data, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0f766e")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, -1), 7),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f3f4f6")]),
            ]
        )
    )
    doc.build([Paragraph(title, styles["Title"]), Spacer(1, 12), table])
    return buffer.getvalue()


def excel_bytes(df: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.drop(columns=["Worker ID"], errors="ignore").to_excel(writer, index=False, sheet_name="Weekly Report")
    return buffer.getvalue()


def csv_bytes(df: pd.DataFrame) -> bytes:
    return df.drop(columns=["Worker ID"], errors="ignore").to_csv(index=False).encode("utf-8")


def page_header(title: str, caption: str = "") -> None:
    st.title(title)
    if caption:
        st.caption(caption)


def dashboard_page(db: Database, week_start: str) -> None:
    page_header("Dashboard", "Today and weekly payroll summary")
    df = weekly_dataframe(db, week_start)
    total_workers = len(df)
    total_crates = int(df["Total Crates"].sum()) if not df.empty else 0
    gross = float(df["Gross Pay"].sum()) if not df.empty else 0
    debt = float(df["Debt"].sum()) if not df.empty else 0
    net = float(df["Amount To Pay"].sum()) if not df.empty else 0

    cols = st.columns(5)
    cols[0].metric("Total Workers", total_workers)
    cols[1].metric("Total Crates", total_crates)
    cols[2].metric("Gross Pay", money(gross))
    cols[3].metric("Total Debt", money(debt))
    cols[4].metric("Net Pay", money(net))

    st.subheader("Weekly Summary")
    st.write(f"Total crates Monday-Friday: **{total_crates}**")
    st.write(f"Total amount payable: **{money(net)}**")
    st.dataframe(format_money_columns(df), hide_index=True, use_container_width=True)


def workers_page(db: Database) -> None:
    page_header("Workers", "Add, edit, delete, search, and view worker history")
    search = st.text_input("Search worker")
    workers = db.workers(search)
    worker_df = pd.DataFrame(
        [{"ID": w["id"], "Worker Name": w["name"], "Debt Amount": w["debt_amount"], "Date Added": w["date_added"]} for w in workers]
    )
    st.dataframe(format_money_columns(worker_df), hide_index=True, use_container_width=True)

    with st.expander("Add worker", expanded=True):
        with st.form("add_worker_form", clear_on_submit=True):
            name = st.text_input("Worker name")
            debt = st.number_input("Debt amount", min_value=0.0, step=5.0)
            submitted = st.form_submit_button("Add worker")
            if submitted:
                if not name.strip():
                    st.error("Worker name is required.")
                else:
                    try:
                        db.add_worker(name, debt)
                        st.success("Worker added.")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("A worker with this name already exists.")

    if not worker_df.empty:
        st.subheader("Edit or delete worker")
        selected_id = st.selectbox("Select worker", worker_df["ID"], format_func=lambda wid: worker_df.loc[worker_df["ID"] == wid, "Worker Name"].iloc[0])
        worker = db.worker_by_id(int(selected_id))
        if worker:
            with st.form("edit_worker_form"):
                new_name = st.text_input("Name", value=worker["name"])
                new_debt = st.number_input("Debt", min_value=0.0, step=5.0, value=float(worker["debt_amount"]))
                col1, col2 = st.columns(2)
                save = col1.form_submit_button("Save changes")
                delete = col2.form_submit_button("Delete worker")
                if save:
                    try:
                        db.update_worker(worker["id"], new_name, new_debt)
                        st.success("Worker updated.")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("Another worker already has that name.")
                if delete:
                    db.delete_worker(worker["id"])
                    st.warning("Worker deleted.")
                    st.rerun()

            st.subheader("Worker History")
            st.dataframe(db.worker_history(worker["id"]), hide_index=True, use_container_width=True)


def daily_records_page(db: Database, week_start: str) -> None:
    page_header("Daily Records", "Manual entry and wage calculation")
    search = st.text_input("Search records")
    df = weekly_dataframe(db, week_start, search)
    st.dataframe(format_money_columns(df), hide_index=True, use_container_width=True)

    workers = db.workers()
    if not workers:
        st.info("Add workers before entering crates.")
        return

    st.subheader("Manual Entry")
    with st.form("manual_entry_form"):
        worker_id = st.selectbox("Worker", [w["id"] for w in workers], format_func=lambda wid: db.worker_by_id(wid)["name"])
        day = st.selectbox("Day", DAYS, index=DAYS.index(default_day()))
        crates = st.number_input("Crates", min_value=0, step=1)
        mode = st.radio("Mode", ["Set total for day", "Add to existing total"], horizontal=True)
        submitted = st.form_submit_button("Save crates")
        if submitted:
            if mode.startswith("Set"):
                db.set_crates(worker_id, week_start, day, crates)
            else:
                db.add_crates(worker_id, week_start, day, crates)
            st.success("Crates saved.")
            st.rerun()

    st.subheader("Save Payment Record")
    payment_worker_id = st.selectbox("Worker to mark paid", [w["id"] for w in workers], format_func=lambda wid: db.worker_by_id(wid)["name"], key="payment_worker")
    if st.button("Save payment record"):
        db.save_payment(payment_worker_id, week_start)
        st.success("Payment record saved.")


def scan_sheet_page(db: Database, week_start: str) -> None:
    page_header("Scan Record Sheet", "Upload a phone photo or use the browser camera, then correct results before saving")
    scanner = OCRScanner(db)
    fallback_day = st.selectbox("Default day if OCR cannot detect it", DAYS, index=DAYS.index(default_day()))

    upload = st.file_uploader("Upload record sheet image", type=["png", "jpg", "jpeg", "bmp", "tif", "tiff"])
    camera = st.camera_input("Or take a photo with the camera")
    source = camera or upload

    if source:
        image = Image.open(source)
        st.image(image, caption="Record sheet preview", use_container_width=True)
        if st.button("Scan image"):
            try:
                day, results, text = scanner.parse(image, fallback_day)
                st.session_state["scan_results"] = results
                st.session_state["scan_text"] = text
                st.success(f"OCR found {len(results)} worker rows for {day}.")
            except Exception as exc:
                st.session_state["scan_results"] = pd.DataFrame(columns=["Worker", "Crates", "Day", "Source Line"])
                st.warning(str(exc))

    st.subheader("Editable OCR Preview")
    preview = st.session_state.get(
        "scan_results", pd.DataFrame(columns=["Worker", "Crates", "Day", "Source Line"])
    )
    edited = st.data_editor(
        preview,
        num_rows="dynamic",
        use_container_width=True,
        column_config={
            "Crates": st.column_config.NumberColumn("Crates", min_value=0, step=1),
            "Day": st.column_config.SelectboxColumn("Day", options=DAYS),
        },
    )

    col1, col2 = st.columns([1, 1])
    if col1.button("Save scanned results"):
        if edited.empty:
            st.error("No scan rows to save.")
        else:
            saved = 0
            missing = []
            for _, row in edited.iterrows():
                worker = db.worker_by_name(str(row["Worker"]))
                if not worker:
                    missing.append(str(row["Worker"]))
                    continue
                db.add_crates(worker["id"], week_start, str(row["Day"]), int(row["Crates"]))
                saved += 1
            if missing:
                st.warning(f"Saved {saved} rows. These workers were not found: {', '.join(missing)}")
            else:
                st.success(f"Saved {saved} scanned rows.")
            st.session_state["scan_results"] = edited

    if col2.button("Clear preview"):
        st.session_state["scan_results"] = pd.DataFrame(columns=["Worker", "Crates", "Day", "Source Line"])
        st.rerun()

    with st.expander("Raw OCR text"):
        st.text(st.session_state.get("scan_text", "No OCR text yet."))


def reports_page(db: Database, week_start: str) -> None:
    page_header("Reports", "Individual reports, weekly farm report, exports, and payment slips")
    df = weekly_dataframe(db, week_start)
    st.subheader("Weekly Farm Report")
    st.dataframe(format_money_columns(df), hide_index=True, use_container_width=True)

    if df.empty:
        st.info("No report data yet.")
        return

    col1, col2, col3 = st.columns(3)
    col1.download_button("Download CSV", csv_bytes(df), "weekly_farm_report.csv", "text/csv")
    col2.download_button(
        "Download Excel",
        excel_bytes(df),
        "weekly_farm_report.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    if REPORTLAB_AVAILABLE:
        col3.download_button(
            "Download PDF",
            pdf_bytes(df, f"Weekly Farm Report - Week {week_start}"),
            "weekly_farm_report.pdf",
            "application/pdf",
        )
    else:
        col3.warning("Install reportlab for PDF exports.")

    st.subheader("Individual Worker Report")
    worker_name = st.selectbox("Worker", df["Worker"].tolist())
    worker_report = df[df["Worker"] == worker_name].drop(columns=["Worker ID"], errors="ignore")
    st.dataframe(format_money_columns(worker_report), hide_index=True, use_container_width=True)

    st.subheader("Weekly Payment Slips")
    slip_text = build_payment_slips_text(df, week_start)
    st.download_button("Download payment slips as text", slip_text, "payment_slips.txt", "text/plain")


def settings_page(db: Database) -> None:
    page_header("Settings", "OCR path, backups, and audit log")
    with st.form("settings_form"):
        tesseract_path = st.text_input("Tesseract executable path", value=db.setting("tesseract_path"))
        dark_mode = st.checkbox("Dark mode", value=db.setting("dark_mode") == "1")
        submitted = st.form_submit_button("Save settings")
        if submitted:
            if tesseract_path and not Path(tesseract_path).exists():
                st.error("The Tesseract path does not exist.")
            else:
                db.set_setting("tesseract_path", tesseract_path)
                db.set_setting("dark_mode", "1" if dark_mode else "0")
                db.log("SAVE_SETTINGS", "Settings updated")
                st.success("Settings saved.")

    if st.button("Create database backup now"):
        target = db.backup()
        st.success(f"Backup created: {target}")

    st.subheader("Audit Log")
    st.dataframe(db.audit_df(), hide_index=True, use_container_width=True)


def format_money_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    formatted = df.copy()
    for col in ["Gross Pay", "Debt", "Amount To Pay", "Debt Amount"]:
        if col in formatted.columns:
            formatted[col] = formatted[col].apply(lambda value: money(float(value)))
    return formatted


def build_payment_slips_text(df: pd.DataFrame, week_start: str) -> str:
    lines = [f"CratePay Manager - Payment Slips", f"Week starting {week_start}", ""]
    for _, row in df.iterrows():
        lines.extend(
            [
                f"Worker: {row['Worker']}",
                f"Total crates: {row['Total Crates']}",
                f"Gross pay: {money(float(row['Gross Pay']))}",
                f"Debt deduction: {money(float(row['Debt']))}",
                f"Final payment: {money(float(row['Amount To Pay']))}",
                "-" * 34,
            ]
        )
    return "\n".join(lines)


def inject_theme(db: Database) -> None:
    if db.setting("dark_mode") != "1":
        return
    st.markdown(
        """
        <style>
        .stApp { background: #111827; color: #f9fafb; }
        [data-testid="stSidebar"] { background: #0f172a; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(page_title=APP_NAME, page_icon="CP", layout="wide")
    db = get_db()
    inject_theme(db)

    st.sidebar.title("CratePay")
    st.sidebar.caption("Farm payroll manager")
    week_start = st.sidebar.text_input("Week start", value=current_week_start())
    page = st.sidebar.radio(
        "Page",
        ["Dashboard", "Workers", "Daily Records", "Scan Sheet", "Reports", "Settings"],
    )
    st.sidebar.divider()
    st.sidebar.write(f"Rate per crate: **{money(RATE_PER_CRATE)}**")

    if page == "Dashboard":
        dashboard_page(db, week_start)
    elif page == "Workers":
        workers_page(db)
    elif page == "Daily Records":
        daily_records_page(db, week_start)
    elif page == "Scan Sheet":
        scan_sheet_page(db, week_start)
    elif page == "Reports":
        reports_page(db, week_start)
    elif page == "Settings":
        settings_page(db)


if __name__ == "__main__":
    main()
