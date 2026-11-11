#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fterm_build_db.py で作成した SQLite から、用途別のファイルを出力する。

使い方（例）:
    python fterm_export.py                          # 既定: simple と tree の CSV を export/ に出力
    python fterm_export.py --format simple          # コード＋説明文だけの CSV
    python fterm_export.py --format tree            # 階層の列付き CSV（＋テーマ一覧、祖先表）
    python fterm_export.py --format json            # 入れ子の JSON
    python fterm_export.py --format xlsx            # 字下げ・アウトライン付きの Excel
    python fterm_export.py --format all             # すべて
    python fterm_export.py --format tree --theme 2E110 4C083   # テーマを絞る
    python fterm_export.py --encoding utf-8         # BOM なし UTF-8（既定は Excel 向けの BOM 付き）

出力ファイル（export/ 以下）:
    fterm_simple.csv     fterm, label
    fterm_tree.csv       テーマ名・観点名・depth・parent・path などを含む1表
    fterm_themes.csv     収録済みテーマの一覧（一覧表の有無、取得日など。一覧表なしのテーマも含む）
    fterm_ancestors.csv  Fターム×祖先の対応表（下位概念ごとの集計用）
    fterm_tree.json      テーマ → 観点 → Fターム の入れ子
    fterm.xlsx           閲覧用（説明文を字下げ、行をアウトラインでグループ化）

xlsx 出力には openpyxl が必要:  pip install openpyxl
"""

import argparse
import csv
import json
import os
import sqlite3
import sys


def connect(db):
    if not os.path.exists(db):
        sys.exit(f"DB が見つかりません: {db}")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def list_status(t):
    """themes 行の一覧表の有無（list_status 列がない旧版の DB では Fターム数から判定）。"""
    if "list_status" in t.keys() and t["list_status"]:
        return t["list_status"]
    return "一覧表あり" if (t["n_fterms"] or 0) > 0 else "一覧表なし"


def theme_filter(themes):
    if not themes:
        return "", []
    ph = ",".join("?" * len(themes))
    return f" WHERE f.theme_code IN ({ph})", [t.upper() for t in themes]


def fetch_tree_rows(conn, themes):
    where, params = theme_filter(themes)
    sql = f"""
        SELECT f.theme_code, t.theme_name, f.viewpoint, v.viewpoint_name,
               f.fterm, f.code, f.label, f.depth, f.parent_fterm,
               f.path_codes, f.path_label, f.asterisk, f.is_viewpoint_head,
               f.label_norm, f.sort_order
        FROM fterms f
        LEFT JOIN themes t     ON t.theme_code = f.theme_code
        LEFT JOIN viewpoints v ON v.theme_code = f.theme_code AND v.viewpoint = f.viewpoint
        {where}
        ORDER BY f.theme_code, f.sort_order
    """
    return conn.execute(sql, params).fetchall()


def write_csv(path, header, rows, encoding):
    with open(path, "w", newline="", encoding=encoding) as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)   # コードの先頭ゼロ等を守るため全項目を引用符で囲む
        w.writerow(header)
        w.writerows(rows)
    print(f"  出力: {path}  ({len(rows)} 行)")


# ---------------------------------------------------------------------------
def export_simple(conn, themes, out, enc):
    rows = fetch_tree_rows(conn, themes)
    write_csv(os.path.join(out, "fterm_simple.csv"),
              ["fterm", "label"],
              [(r["fterm"], r["label"]) for r in rows], enc)


def export_tree(conn, themes, out, enc):
    rows = fetch_tree_rows(conn, themes)
    header = ["theme_code", "theme_name", "viewpoint", "viewpoint_name", "fterm", "code",
              "label", "depth", "parent_fterm", "path_codes", "path_label", "asterisk",
              "is_viewpoint_head", "label_norm", "sort_order"]
    write_csv(os.path.join(out, "fterm_tree.csv"), header,
              [tuple(r[h] for h in header) for r in rows], enc)

    # テーマ一覧
    where, params = theme_filter(themes)
    trows = conn.execute(
        f"SELECT * FROM themes t {where.replace('f.', 't.')} ORDER BY theme_code", params).fetchall()
    th = ["theme_code", "theme_name", "tech_field", "list_status", "fi_ranges", "remarks",
          "fetched_date", "n_viewpoints", "n_fterms", "source_file", "source_url"]
    out_rows = []
    for t in trows:
        fi = conn.execute("SELECT fi_raw FROM theme_fi_ranges WHERE theme_code=? ORDER BY seq",
                          (t["theme_code"],)).fetchall()
        out_rows.append((t["theme_code"], t["theme_name"], t["tech_field"], list_status(t),
                         " ; ".join(x[0] for x in fi), t["remarks"], t["fetched_date"],
                         t["n_viewpoints"], t["n_fterms"], t["source_file"], t["source_url"]))
    write_csv(os.path.join(out, "fterm_themes.csv"), th, out_rows, enc)

    # 祖先表（例: ancestor=2E110AA07 で絞れば AA07 とその下位すべてが得られる）
    where_a = where.replace("f.theme_code", "f.theme_code")
    arows = conn.execute(f"""
        SELECT a.fterm, a.ancestor, a.distance
        FROM fterm_ancestors a JOIN fterms f ON f.fterm = a.fterm
        {where_a}
        ORDER BY f.theme_code, f.sort_order, a.distance
    """, params).fetchall()
    write_csv(os.path.join(out, "fterm_ancestors.csv"), ["fterm", "ancestor", "distance"],
              [tuple(r) for r in arows], enc)


def export_json(conn, themes, out):
    where, params = theme_filter(themes)
    trows = conn.execute(
        f"SELECT * FROM themes t {where.replace('f.', 't.')} ORDER BY theme_code", params).fetchall()
    data = []
    for t in trows:
        tc = t["theme_code"]
        fi = [dict(fi_raw=r["fi_raw"], fi_from=r["fi_from"], fi_to=r["fi_to"], facet=r["facet"])
              for r in conn.execute("SELECT * FROM theme_fi_ranges WHERE theme_code=? ORDER BY seq", (tc,))]
        nodes = {}
        roots = []
        for r in conn.execute("SELECT * FROM fterms WHERE theme_code=? ORDER BY sort_order", (tc,)):
            node = dict(fterm=r["fterm"], code=r["code"], label=r["label"], depth=r["depth"])
            if r["asterisk"]:
                node["asterisk"] = True
            node["children"] = []
            nodes[r["fterm"]] = node
            if r["parent_fterm"] and r["parent_fterm"] in nodes:
                nodes[r["parent_fterm"]]["children"].append(node)
            else:
                roots.append(node)          # 観点見出し（XX00）
        data.append(dict(theme_code=tc, theme_name=t["theme_name"], tech_field=t["tech_field"],
                         list_status=list_status(t),
                         remarks=t["remarks"], fetched_date=t["fetched_date"],
                         source_url=t["source_url"], fi_ranges=fi, viewpoints=roots))
    path = os.path.join(out, "fterm_tree.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    print(f"  出力: {path}  ({len(data)} テーマ)")


def export_xlsx(conn, themes, out):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("  [スキップ] xlsx 出力には openpyxl が必要です:  pip install openpyxl", file=sys.stderr)
        return

    rows = fetch_tree_rows(conn, themes)
    wb = Workbook()
    ws = wb.active
    ws.title = "Fターム"
    header = ["テーマ", "テーマ名", "Fターム", "説明", "depth", "親", "＊", "階層パス"]
    ws.append(header)
    head_fill = PatternFill("solid", fgColor="DDEBF7")
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.sheet_properties.outlinePr.summaryBelow = False   # 見出し行の下に子をぶら下げる

    i = 1                                                # ws.max_row は毎回シート全体を数えて遅いので自分で数える
    for r in rows:
        ws.append([r["theme_code"], r["theme_name"], r["fterm"], r["label"], r["depth"],
                   r["parent_fterm"] or "", "＊" if r["asterisk"] else "", r["path_label"]])
        i += 1
        for col in (1, 3, 6):
            ws.cell(i, col).number_format = "@"          # 文字列として保持
        ws.cell(i, 4).alignment = Alignment(indent=r["depth"] * 2)
        if r["is_viewpoint_head"]:
            for col in range(1, len(header) + 1):          # ws[i] も毎回シート全体を数えて遅いので使わない
                c = ws.cell(i, col)
                c.fill = head_fill
                c.font = Font(bold=True)
        lvl = min(int(r["depth"]), 7)                     # Excel のアウトラインは最大 7 段
        if lvl:
            ws.row_dimensions[i].outline_level = lvl

    widths = [8, 18, 12, 60, 6, 12, 4, 80]
    for k, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(k)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(header))}{i}"

    # テーマ一覧シート
    ws2 = wb.create_sheet("テーマ一覧")
    ws2.append(["テーマ", "テーマ名", "技術分野", "一覧表", "FI", "備考", "取得日", "観点数", "Fターム数", "URL"])
    for c in ws2[1]:
        c.font = Font(bold=True)
    where, params = theme_filter(themes)
    for t in conn.execute(
            f"SELECT * FROM themes t {where.replace('f.', 't.')} ORDER BY theme_code", params):
        fi = conn.execute("SELECT fi_raw FROM theme_fi_ranges WHERE theme_code=? ORDER BY seq",
                          (t["theme_code"],)).fetchall()
        ws2.append([t["theme_code"], t["theme_name"], t["tech_field"], list_status(t),
                    " ; ".join(x[0] for x in fi), t["remarks"], t["fetched_date"],
                    t["n_viewpoints"], t["n_fterms"], t["source_url"]])
    for k, w in enumerate([8, 24, 12, 10, 30, 50, 12, 8, 10, 60], 1):
        ws2.column_dimensions[get_column_letter(k)].width = w

    path = os.path.join(out, "fterm.xlsx")
    wb.save(path)
    print(f"  出力: {path}  ({len(rows)} 行)")


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Fターム SQLite → CSV / JSON / Excel")
    ap.add_argument("--db", default="fterm.sqlite")
    ap.add_argument("--out", default="export", help="出力フォルダ（既定: export）")
    ap.add_argument("--format", nargs="+", default=["simple", "tree"],
                    choices=["simple", "tree", "json", "xlsx", "all"])
    ap.add_argument("--theme", nargs="*", help="出力するテーマコード（省略時はすべて）")
    ap.add_argument("--encoding", default="utf-8-sig",
                    help="CSV の文字コード（既定: utf-8-sig = Excel で文字化けしない BOM 付き UTF-8）")
    args = ap.parse_args(argv)

    fmts = {"simple", "tree", "json", "xlsx"} if "all" in args.format else set(args.format)
    os.makedirs(args.out, exist_ok=True)
    conn = connect(args.db)
    if "simple" in fmts:
        export_simple(conn, args.theme, args.out, args.encoding)
    if "tree" in fmts:
        export_tree(conn, args.theme, args.out, args.encoding)
    if "json" in fmts:
        export_json(conn, args.theme, args.out)
    if "xlsx" in fmts:
        export_xlsx(conn, args.theme, args.out)
    conn.close()


if __name__ == "__main__":
    main()
