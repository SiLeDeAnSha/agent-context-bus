#!/usr/bin/env python3
"""Prepare gsheets batch JSON files from Work and Personal Excel workbooks."""

import json
import os
from typing import Any

from openpyxl import load_workbook

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
EXAMPLE_CONFIG_PATH = os.path.join(BASE_DIR, "config.example.json")
WORK_BATCH_OUT = os.path.join(BASE_DIR, "data", "gsheets_batch.json")
PERSONAL_BATCH_OUT = os.path.join(BASE_DIR, "data", "gsheets_batch_personal.json")


def load_config() -> dict[str, Any]:
    path = CONFIG_PATH if os.path.exists(CONFIG_PATH) else EXAMPLE_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    for k in ("excel_path", "personal_excel_path", "export_dir"):
        if isinstance(cfg.get(k), str):
            cfg[k] = os.path.expanduser(cfg[k])
    return cfg


def export_batch_from_xlsx(xlsx_path: str, batch_out: str) -> int:
    if not os.path.exists(xlsx_path):
        raise FileNotFoundError(f"Excel file not found: {xlsx_path}")

    wb = load_workbook(xlsx_path, data_only=True)
    requests: list[dict[str, Any]] = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows_iter = ws.iter_rows(values_only=True)
        rows: list[list[Any]] = []
        found_header = False
        for row in rows_iter:
            if not found_header:
                if row and row[0] in ("日期", "项目名称"):
                    found_header = True
                else:
                    continue
            rows.append(["" if v is None else v for v in row])
        col_count = len(rows[0]) if rows else 9
        for _ in range(20):
            rows.append([""] * col_count)
        requests.append(
            {
                "op": "write",
                "range": f"'{sheet_name}'!A1",
                "data": rows,
            }
        )
    wb.close()

    os.makedirs(os.path.dirname(batch_out), exist_ok=True)
    with open(batch_out, "w", encoding="utf-8") as f:
        json.dump(requests, f, ensure_ascii=False, indent=2)
    return len(requests)


def main() -> None:
    cfg = load_config()
    n_work = export_batch_from_xlsx(os.path.expanduser(cfg["excel_path"]), WORK_BATCH_OUT)
    n_pers = export_batch_from_xlsx(os.path.expanduser(cfg["personal_excel_path"]), PERSONAL_BATCH_OUT)
    print(f"Prepared {WORK_BATCH_OUT} ({n_work} sheets) and {PERSONAL_BATCH_OUT} ({n_pers} sheets).")


if __name__ == "__main__":
    main()
