import sqlite3
from pathlib import Path
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo


OUT_DIR = Path("propublica_output")
DB_PATH = OUT_DIR / "propublica_scrape.sqlite"
EXCEL_PATH = OUT_DIR / "propublica_all_states.xlsx"

STATES = [
    "CA", "NY", "TX", "FL", "PA", "IL", "OH", "MA", "VA", "NC",
    "MI", "NJ", "WA", "CO", "DC", "MN", "GA", "MD", "WI", "IN",
    "MO", "TN", "OR", "CT", "AZ", "SC", "IA", "LA", "AL", "KY",
    "OK", "DE", "KS", "UT", "NE", "NV", "NM", "AR", "MS", "ME",
    "WV", "HI", "MT", "VT", "NH", "RI", "ID", "AK", "SD", "ND",
    "WY", "PR", "VI", "GU", "MP", "PW"
]

HEADERS = [
    "State",
    "Page",
    "Org Name",
    "Address",
    "Employer ID",
    "Phone",
    "Email",
    "Org Website",
    "Org URL",
    "Filing URL",
    "IRS990 URL",
    "Error",
]


def autosize_columns(ws):
    max_width = 45

    for col in ws.columns:
        col_letter = get_column_letter(col[0].column)
        longest = 0

        for cell in col:
            value = str(cell.value) if cell.value else ""
            longest = max(longest, len(value))

        ws.column_dimensions[col_letter].width = min(longest + 3, max_width)


def style_sheet(ws):
    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    light_fill = PatternFill("solid", fgColor="D9EAF7")
    error_fill = PatternFill("solid", fgColor="F4CCCC")

    thin_border = Border(
        left=Side(style="thin", color="D9E2F3"),
        right=Side(style="thin", color="D9E2F3"),
        top=Side(style="thin", color="D9E2F3"),
        bottom=Side(style="thin", color="D9E2F3"),
    )

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions

    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = thin_border

    for row in ws.iter_rows(min_row=2):
        has_error = row[-1].value not in [None, ""]

        for cell in row:
            cell.border = thin_border
            cell.alignment = Alignment(vertical="top", wrap_text=True)

            if has_error:
                cell.fill = error_fill
            elif cell.row % 2 == 0:
                cell.fill = light_fill

    autosize_columns(ws)


def create_summary_sheet(wb, state_counts):
    ws = wb.create_sheet("Summary", 0)

    ws.append(["State", "Total Records"])
    for state, count in state_counts.items():
        ws.append([state, count])

    ws["A1"].fill = PatternFill("solid", fgColor="7030A0")
    ws["B1"].fill = PatternFill("solid", fgColor="7030A0")
    ws["A1"].font = Font(color="FFFFFF", bold=True)
    ws["B1"].font = Font(color="FFFFFF", bold=True)

    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(horizontal="center")
            cell.border = Border(
                left=Side(style="thin", color="D9E2F3"),
                right=Side(style="thin", color="D9E2F3"),
                top=Side(style="thin", color="D9E2F3"),
                bottom=Side(style="thin", color="D9E2F3"),
            )

    ws.freeze_panes = "A2"
    autosize_columns(ws)


def export_to_excel():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"SQLite database not found: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    wb = Workbook()
    default_sheet = wb.active
    wb.remove(default_sheet)

    state_counts = {}

    for state in STATES:
        print(f"[EXPORT] Creating sheet for {state}")

        ws = wb.create_sheet(title=state)
        ws.append(HEADERS)

        cur.execute("""
            SELECT
                state,
                page,
                org_name,
                address,
                employer_id,
                phone,
                email,
                org_website,
                org_url,
                filing_url,
                irs990_url,
                error
            FROM results
            WHERE state = ?
            ORDER BY page, org_name
        """, (state,))

        rows = cur.fetchall()
        state_counts[state] = len(rows)

        for row in rows:
            ws.append(row)

        if rows:
            table_ref = f"A1:{get_column_letter(len(HEADERS))}{len(rows) + 1}"
            table = Table(displayName=f"Table_{state}", ref=table_ref)

            style = TableStyleInfo(
                name="TableStyleMedium2",
                showFirstColumn=False,
                showLastColumn=False,
                showRowStripes=True,
                showColumnStripes=False,
            )

            table.tableStyleInfo = style
            ws.add_table(table)

        style_sheet(ws)

        print(f"[DONE] {state}: {len(rows)} rows")

    create_summary_sheet(wb, state_counts)

    wb.save(EXCEL_PATH)
    conn.close()

    print(f"\n[SAVED] Excel exported successfully:")
    print(EXCEL_PATH)


if __name__ == "__main__":
    export_to_excel()