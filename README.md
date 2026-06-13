# CratePay Manager

CratePay Manager is a Streamlit application for farm crate wage tracking. It
stores workers in SQLite, tracks Monday-Friday crates, calculates weekly pay at
R15 per crate, scans photographed record sheets with OCR, and exports reports.

## Install

1. Install Python 3.10 or newer.
2. Install Tesseract OCR:
   - Windows: install from https://github.com/UB-Mannheim/tesseract/wiki
   - During install, note the install path, usually:
     `C:\Program Files\Tesseract-OCR\tesseract.exe`
3. Install Python packages:

```powershell
pip install -r requirements.txt
```

4. If Tesseract is not on your PATH, open the application, go to Settings, and
   set the Tesseract executable path.

## Run Streamlit App

```powershell
streamlit run streamlit_app.py
```

The app opens in your browser.

## Main Features

- Worker management: add, edit, delete, search, history.
- Daily crate tracking from Monday to Friday.
- Automatic wage calculation at R15 per crate.
- OCR record sheet scanner from image upload or webcam.
- Editable OCR preview before saving.
- Manual crate entry if OCR fails.
- Dashboard with daily and weekly totals.
- Individual and weekly reports.
- Export to CSV, Excel, and PDF.
- Automatic SQLite database backups.
- Dark mode.
- Payment slips.
- Audit log of changes.

## Record Sheet Tips

For the most reliable scan, write one worker per line, for example:

```text
Monday
John XXXXXXX
Peter XXXXX
Ben XXXXXXXXXX
```

Use a clear photo with good lighting. The app can still read many imperfect
sheets, but always review the preview before saving.
