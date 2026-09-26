"""Generate synthetic CBP / LFT / RFT lab reports for the OncoCare demo.

All data is fictional and every page is watermarked SYNTHETIC. Reports follow a common
Indian lab layout (lakh/cumm, /cumm, IU/L, DD/MM/YYYY) so the extractor sees realistic input.
Run:  python3 scripts/make_sample_reports.py  (needs reportlab + pillow)
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parents[1] / "sample_reports"
PATIENT = {"name": "Priya Raman", "mrn": "HN-24077", "age": "52 Y", "sex": "Female",
           "ref": "Dr. R. Khanna (Medical Oncology)", "regimen": "Cisplatin 75 mg/m2 q21d"}

# (date on report, label, cycle it precedes)
VISITS = [("03/08/2026", "Baseline (before cycle 1)", 1), ("24/08/2026", "Before cycle 2", 2),
          ("14/09/2026", "Before cycle 3", 3), ("25/09/2026", "Before cycle 4", 4)]

# test, unit, ref text, values per visit
CBP = [
    ("Haemoglobin", "g/dL", "12.0 - 15.5", [13.1, 12.2, 11.0, 9.6]),
    ("Total RBC Count", "mill/cumm", "3.8 - 4.8", [4.52, 4.31, 3.98, 3.52]),
    ("Haematocrit (PCV)", "%", "36 - 46", [39.4, 37.1, 33.8, 29.9]),
    ("MCV", "fL", "83 - 101", [87.2, 86.1, 84.9, 84.9]),
    ("Total Leucocyte Count", "/cumm", "4000 - 11000", [7800, 5600, 4100, 3200]),
    ("Neutrophils", "%", "40 - 80", [65, 59, 51, 36]),
    ("Lymphocytes", "%", "20 - 40", [27, 31, 37, 48]),
    ("Absolute Neutrophil Count", "/cumm", "1500 - 8000", [5070, 3300, 2090, 1150]),
    ("Platelet Count", "lakh/cumm", "1.5 - 4.5", [2.84, 2.21, 1.62, 0.92]),
]
LFT = [
    ("Total Bilirubin", "mg/dL", "0.2 - 1.2", [0.6, 0.7, 0.8, 1.1]),
    ("Direct Bilirubin", "mg/dL", "0.0 - 0.3", [0.2, 0.2, 0.3, 0.4]),
    ("SGOT (AST)", "IU/L", "0 - 40", [24, 31, 48, 86]),
    ("SGPT (ALT)", "IU/L", "0 - 41", [21, 35, 62, 132]),
    ("Alkaline Phosphatase", "IU/L", "44 - 147", [88, 95, 110, 141]),
    ("Total Protein", "g/dL", "6.4 - 8.3", [7.1, 6.9, 6.6, 6.2]),
    ("Serum Albumin", "g/dL", "3.5 - 5.2", [4.2, 4.0, 3.6, 3.1]),
]
RFT = [
    ("Blood Urea", "mg/dL", "15 - 40", [26, 34, 46, 62]),
    ("Serum Creatinine", "mg/dL", "0.6 - 1.1", [0.7, 0.9, 1.2, 1.6]),
    ("eGFR (CKD-EPI)", "mL/min/1.73m2", "> 90", [104, 88, 62, 42]),
    ("Serum Uric Acid", "mg/dL", "2.6 - 6.0", [4.8, 5.2, 5.9, 6.8]),
    ("Serum Sodium", "mmol/L", "135 - 145", [139, 137, 135, 131]),
    ("Serum Potassium", "mmol/L", "3.5 - 5.1", [4.3, 4.0, 3.6, 3.2]),
    ("Serum Chloride", "mmol/L", "98 - 107", [102, 101, 99, 96]),
    ("Serum Magnesium", "mg/dL", "1.7 - 2.2", [2.1, 1.8, 1.5, 1.1]),
    ("Serum Calcium", "mg/dL", "8.6 - 10.3", [9.4, 9.1, 8.8, 8.3]),
]
PANELS = [("COMPLETE BLOOD PICTURE (CBP)", CBP), ("LIVER FUNCTION TEST (LFT)", LFT), ("RENAL FUNCTION TEST (RFT) & ELECTROLYTES", RFT)]


def flag(value: float, ref: str) -> str:
    if ref.startswith(">"):
        return "L" if value < float(ref[1:]) else ""
    low, high = (float(x) for x in ref.split(" - "))
    return "L" if value < low else "H" if value > high else ""


def fmt(v: float) -> str:
    return f"{v:,.0f}" if v >= 1000 else (f"{v:g}")


def pdf_report(index: int) -> Path:
    date, label, cycle = VISITS[index]
    path = OUT / f"{index + 1}_{PATIENT['mrn']}_{date.replace('/', '-')}_pre-C{cycle}.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)
    w, h = A4
    c.setTitle(f"Synthetic lab report {date}")
    c.saveState(); c.setFillColor(colors.Color(0.85, 0.1, 0.1, alpha=0.08)); c.setFont("Helvetica-Bold", 64)
    c.translate(w / 2, h / 2); c.rotate(35); c.drawCentredString(0, 0, "SYNTHETIC DEMO"); c.restoreState()
    c.setFillColor(colors.HexColor("#0b746c")); c.rect(0, h - 70, w, 70, stroke=0, fill=1)
    c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 16); c.drawString(36, h - 38, "Synthetic Diagnostics Lab (DEMO)")
    c.setFont("Helvetica", 9); c.drawString(36, h - 54, "Fictional laboratory for software testing only - not a real report")
    c.setFillColor(colors.black); y = h - 92; c.setFont("Helvetica", 9)
    rows = [("Patient Name", PATIENT["name"], "Sample Collected On", f"{date} 08:15"),
            ("MRN / UHID", PATIENT["mrn"], "Reported On", f"{date} 14:40"),
            ("Age / Sex", f"{PATIENT['age']} / {PATIENT['sex']}", "Referred By", PATIENT["ref"]),
            ("Clinical note", label, "Regimen", PATIENT["regimen"])]
    for a, b, cc, d in rows:
        c.setFont("Helvetica-Bold", 9); c.drawString(36, y, f"{a}:"); c.drawString(300, y, f"{cc}:")
        c.setFont("Helvetica", 9); c.drawString(120, y, b); c.drawString(400, y, d); y -= 14
    y -= 6
    for title, tests in PANELS:
        c.setFillColor(colors.HexColor("#e6f3f0")); c.rect(30, y - 4, w - 60, 16, stroke=0, fill=1)
        c.setFillColor(colors.HexColor("#075b55")); c.setFont("Helvetica-Bold", 10); c.drawString(36, y, title); y -= 18
        c.setFillColor(colors.grey); c.setFont("Helvetica-Bold", 8)
        for x, t in ((36, "TEST"), (250, "RESULT"), (320, "FLAG"), (360, "UNIT"), (450, "BIO. REF. INTERVAL")):
            c.drawString(x, y, t)
        y -= 13
        for name, unit, ref, values in tests:
            v = values[index]; fl = flag(v, ref)
            c.setFillColor(colors.black); c.setFont("Helvetica", 9); c.drawString(36, y, name)
            c.setFont("Helvetica-Bold" if fl else "Helvetica", 9)
            if fl: c.setFillColor(colors.HexColor("#b93b49"))
            c.drawString(250, y, fmt(v)); c.drawString(320, y, fl)
            c.setFillColor(colors.black); c.setFont("Helvetica", 9); c.drawString(360, y, unit); c.drawString(450, y, ref)
            y -= 13
        y -= 8
    c.setFont("Helvetica-Oblique", 8); c.setFillColor(colors.grey)
    c.drawString(36, 40, "SYNTHETIC DATA - generated for Thravi OncoCare prototype testing. Not for clinical use.")
    c.drawString(36, 28, "Electronically verified by: Dr. A. Demo, MD (Pathology) - fictional signatory")
    c.save()
    return path


def scanned_image(index: int) -> Path:
    """A 'phone photo' of the same report: no text layer, slight rotation and blur."""
    date, label, cycle = VISITS[index]
    W, H = 1240, 1754
    img = Image.new("RGB", (W, H), (250, 248, 242))
    d = ImageDraw.Draw(img)
    def font(size, bold=False):
        for name in (("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
            try: return ImageFont.truetype(name, size)
            except OSError: continue
        return ImageFont.load_default()
    d.rectangle([0, 0, W, 120], fill=(11, 116, 108))
    d.text((50, 30), "Synthetic Diagnostics Lab (DEMO)", fill="white", font=font(38, True))
    d.text((50, 80), "Fictional laboratory - not a real report", fill="white", font=font(20))
    y = 150
    for a, b in (("Patient Name", PATIENT["name"]), ("MRN / UHID", PATIENT["mrn"]), ("Sample Collected On", f"{date} 08:15"), ("Clinical note", label)):
        d.text((50, y), f"{a}: {b}", fill=(20, 20, 20), font=font(24)); y += 36
    y += 10
    for title, tests in PANELS:
        d.rectangle([40, y, W - 40, y + 36], fill=(230, 243, 240)); d.text((50, y + 4), title, fill=(7, 91, 85), font=font(24, True)); y += 48
        for name, unit, ref, values in tests:
            v = values[index]; fl = flag(v, ref)
            col = (185, 59, 73) if fl else (20, 20, 20)
            d.text((50, y), name, fill=(20, 20, 20), font=font(22)); d.text((520, y), f"{fmt(v)} {fl}", fill=col, font=font(22, bool(fl)))
            d.text((680, y), unit, fill=(20, 20, 20), font=font(22)); d.text((930, y), ref, fill=(20, 20, 20), font=font(22)); y += 34
        y += 14
    d.text((50, H - 60), "SYNTHETIC DATA - not for clinical use", fill=(150, 150, 150), font=font(20))
    img = img.rotate(-1.2, expand=True, fillcolor=(210, 205, 195)).filter(ImageFilter.GaussianBlur(0.6))
    path = OUT / f"{index + 1}_{PATIENT['mrn']}_{date.replace('/', '-')}_pre-C{cycle}_PHOTO.jpg"
    img.save(path, quality=82)
    return path


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    for i in range(len(VISITS)):
        print(pdf_report(i))
    print(scanned_image(3))
