#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
J-PlatPat の Fタームリスト HTML を解析して SQLite データベースを作成・更新する。

使い方（例）:
    python fterm_build_db.py raw/                       # raw フォルダ内の *.html をすべて登録
    python fterm_build_db.py raw/fTermList2E110_20261031.html
    python fterm_build_db.py raw/*.html --db fterm.sqlite
    python fterm_build_db.py raw/fTermList2E110.html --date 2026-10-31
    python fterm_build_db.py raw/fTermList2E110.html --dump-tokens   # 解析の中間結果を表示（不具合調査用）

HTML の保存:
    ブラウザで Fタームリストのページを開き、「名前を付けて保存」で HTML を保存する。
    ファイル名に取得日を入れておくと（例: fTermList2E110_20261031.html）、その日付が取得日として記録される。
    日付がない場合は --date の値、それもなければファイルの更新日時を取得日とする。

動作:
    - Fタームの表が空のページ（一覧表のないテーマ）も、テーマ名・FI・備考を登録し、
      themes.list_status に「一覧表なし」と記録する（Fタームは0件）。
    - 「404 Not Found」のページは登録せずスキップする。
    - テーマ単位で「削除して入れ直す」。同じテーマを再取得したら再実行すればよい。
    - DB にあるデータより古い取得日の HTML は、--force を付けない限りスキップする。
    - 解析時の警告は parse_log テーブルと画面に出力する。

必要なライブラリ:
    pip install beautifulsoup4
"""

import argparse
import datetime as dt
import glob
import os
import re
import sqlite3
import sys
import unicodedata
from collections import deque

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    sys.exit("beautifulsoup4 が必要です:  pip install beautifulsoup4")


SOURCE_URL_TEMPLATE = (
    "https://www.j-platpat.inpit.go.jp/cache/classify/patent/PMGS_HTML/jpp/"
    "F_TERM/ja/fTermList/fTermList{theme}.html"
)

# ---------------------------------------------------------------------------
# 正規表現など
# ---------------------------------------------------------------------------
RE_THEME = re.compile(r"^(\d[A-Z]\d{3})(?:\s+(.*))?$")          # 2E110 壁の仕上げ 住環境
RE_VP_LETTERS = re.compile(r"^[A-Z]{2}$")                        # AA
RE_CODE = re.compile(r"^([A-Z]{2})(\d{2})$")                     # AA01 / AA00
RE_FI = re.compile(r"[A-H]\d{2}[A-Z]\d+/\d+")                    # E04F13/00
RE_FI_RANGE = re.compile(
    r"(?P<from>[A-H]\d{2}[A-Z]\d+/\d+)"
    r"(?:\s*-\s*(?P<to>(?:[A-H]\d{2}[A-Z])?\d+/\d+))?"
    r"(?P<facet>@[0-9A-Z]+)?"
)
RE_FI_START = re.compile(r"^[A-H]\d{2}[A-Z]\d+/\d+")       # 全角を正規化した後の FI の書き出し
RE_FI_WORD = re.compile(r"^[A-H]\d{2}[A-Z]\d+/\d+(?:-(?:[A-H]\d{2}[A-Z])?\d+/\d+)?(?:@[0-9A-Z]+)?$")
RE_DATE_IN_NAME = re.compile(r"(?<!\d)(20\d{2})[-_]?(\d{2})[-_]?(\d{2})(?!\d)")
RE_THEME_IN_NAME = re.compile(r"fTermList(?:_en_)?(\d[A-Z]\d{3})", re.I)
# 説明文中の参照（NFKC 正規化後の文字列に適用）: ＡＡ４８ / ＧＡ３１～４４ / ＣＣ１３～ / 5F031AA01
RE_REF = re.compile(
    r"(?<![A-Z0-9])(?P<theme>\d[A-Z]\d{3})?(?P<vp>[A-Z]{2})(?P<n1>\d{2})"
    r"(?:\s*[～〜~\-ー]\s*(?P<n2>\d{2})?)?"
)

DOT_CHARS = "・･·•"          # 階層を表すドット（全角・半角の揺れを吸収）
ASTERISKS = "＊*"
BLOCK_TAGS = ["table", "thead", "tbody", "tr", "td", "th", "p", "div", "li", "ul", "ol",
              "h1", "h2", "h3", "h4", "h5", "h6", "dt", "dd", "section", "header", "footer"]
NAV_TEXTS = {"English", "閉じる", "Fタームリスト", "TOP", "テーマグループ選択に戻る", "一階層上へ",
             "このページのトップへ", "ページトップへ"}


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------
def norm(s: str) -> str:
    """検索用の正規化（全角英数→半角など）。"""
    s = (s or "").replace("\u301c", "\uff5e")   # 波ダッシュ〜を全角チルダ～に統一（Shift_JIS 由来の揺れ）
    return unicodedata.normalize("NFKC", s).strip()


def clean(s: str) -> str:
    """前後の空白（全角空白を含む）を除去し、内部の連続空白を1つにする。"""
    s = (s or "").replace(" ", " ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return s.strip(" 　")


def is_nav(line: str) -> bool:
    """ナビゲーション用の文字列だけでできた行か（「English閉じる」のように連結されていても判定）。"""
    rest = line
    for n in sorted(NAV_TEXTS, key=len, reverse=True):
        rest = rest.replace(n, "")
    return not rest.strip(" \u3000|")


def leading_dots(s: str) -> int:
    n = 0
    for ch in s:
        if ch in DOT_CHARS:
            n += 1
        elif ch in " 　":
            continue
        else:
            break
    return n


def strip_dots(s: str) -> str:
    return s.lstrip(DOT_CHARS + " 　")


# ---------------------------------------------------------------------------
# HTML → 行（ブロック単位のテキスト）
# ---------------------------------------------------------------------------
def html_to_lines(html_bytes: bytes):
    """
    特定のクラス名やID に依存しないよう、ブロック要素の境界で改行を入れてからテキスト化する。
    インライン要素（span, a など）で分断された説明文は1行にまとまる。
    """
    soup = BeautifulSoup(html_bytes, "html.parser")
    for t in soup(["script", "style", "noscript", "head"]):
        t.decompose()
    body = soup.body or soup

    # FI のリンクを識別できるよう印をつける（ヘッダの FI 範囲判定に使用）
    for br in body.find_all("br"):
        br.replace_with("\n")
    for tag in body.find_all(BLOCK_TAGS):
        tag.insert_before("\n")
        tag.append("\n")

    text = body.get_text()
    lines = []
    for raw in text.split("\n"):
        line = clean(raw)
        if line:
            lines.append(line)
    return lines


def split_line_to_tokens(line: str):
    """
    1行を「コード」「観点記号」「説明文（・で始まる）」「その他テキスト」のトークンに分ける。
    1つのセルに複数のコードや説明文が並んでいる場合にも対応する。
    戻り値: [(kind, text), ...]  kind = 'code' | 'vp' | 'desc' | 'text' | 'fi'
    """
    words = line.split(" ")
    tokens = []
    for w in words:
        if not w:
            continue
        prev = tokens[-1][0] if tokens else None
        if w in ("_", "＿"):                          # 観点横の FI 欄の埋め草
            continue
        if prev != "desc" and (RE_FI_WORD.match(w) or RE_FI_START.match(norm(w))):
            tokens.append(["fi", w])
        elif prev == "fi" and re.match(r"^-\s*\d+/\d+", w):
            tokens[-1][1] += w                       # 「E04F13/00」「-13/30」の連結
        elif prev == "fi" and (tokens[-1][1].rstrip().endswith((";", "；"))
                               or re.match(r"^(?:[A-H]\d{2}[A-Z])?\d+/", norm(w))):
            tokens[-1][1] += " " + w                 # 「9/00-9/04; 11/00-11/02; …」のように空白で区切られた続き
        elif RE_CODE.match(w):
            tokens.append(["code", w])
        elif RE_VP_LETTERS.match(w):
            tokens.append(["vp", w])
        elif w[0] in DOT_CHARS:
            tokens.append(["desc", w])
        else:
            # 直前が説明文またはテキストなら連結（説明文中の空白）
            if tokens and tokens[-1][0] in ("desc", "text"):
                tokens[-1][1] += " " + w
            else:
                tokens.append(["text", w])
    return [(k, t) for k, t in tokens]


# ---------------------------------------------------------------------------
# 解析本体
# ---------------------------------------------------------------------------
class ParseResult:
    def __init__(self):
        self.theme_code = None
        self.theme_name = None
        self.tech_field = None
        self.remarks = []
        self.fi_raw = []          # FI 文字列（ヘッダ・本文とも）
        self.viewpoints = []      # [(vp, name)]
        self.terms = []           # dict のリスト
        self.warnings = []
        self.tokens = []          # デバッグ用
        self.page_status = "ok"   # ok | empty_list | not_found


def parse_fterm_html(html_bytes: bytes, theme_hint: str = None) -> ParseResult:
    r = ParseResult()
    # J-PlatPat のページの種類を判定する
    #   404 ページ      : <title>404 Not Found</title>
    #   一覧表のないテーマ: Fターム表（class="ftermPontTable"）はあるが、コードのセル（ftermDataHead）がない
    m_title = re.search(rb"<title[^>]*>(.*?)</title>", html_bytes, re.I | re.S)
    if m_title and b"404" in m_title.group(1):
        r.page_status = "not_found"
        return r
    if b"ftermPontTable" in html_bytes and b"ftermDataHead" not in html_bytes:
        r.page_status = "empty_list"
    lines = html_to_lines(html_bytes)

    # ---- 1) ヘッダ部（最初の観点 XX00 が現れるまで） --------------------
    i = 0
    first_vp_line = None
    for idx, line in enumerate(lines):
        if re.search(r"(?:^|\s)[A-Z]{2}00(?:\s|$)", line):
            first_vp_line = idx
            break
    header = lines[: first_vp_line if first_vp_line is not None else len(lines)]
    body = lines[first_vp_line:] if first_vp_line is not None else []

    pending_fi = None
    j = 0
    while j < len(header):
        line = header[j]
        j += 1
        if is_nav(line) or RE_VP_LETTERS.match(line):
            continue
        m = RE_THEME.match(line)
        if m and r.theme_code is None:
            r.theme_code = m.group(1)
            rest = clean(m.group(2) or "")
            if rest:
                # 「2E110 壁の仕上げ 住環境」のように1行にまとまっている場合は空白で分ける
                parts = [p for p in re.split(r"[ 　]+", rest) if p]
                r.theme_name = parts[0] if parts else None
                r.tech_field = " ".join(parts[1:]) or None
            else:
                # J-PlatPat の通常のページ：コード・テーマ名・技術分野が別々のセルにある。
                # テーマ名に空白を含むもの（例「交換機の特殊サービス  （２）」）があるので、セルの文字列をそのまま使う
                cells = []
                while len(cells) < 2 and j < len(header):
                    nxt = header[j]
                    if is_nav(nxt) or RE_FI.search(nxt) or RE_THEME.match(nxt):
                        break
                    cells.append(nxt)
                    j += 1
                if cells:
                    r.theme_name = cells[0]
                if len(cells) >= 2:
                    r.tech_field = cells[1]
            continue
        if RE_FI.search(line) or (pending_fi is not None and line.startswith("-")):
            # 「E04F13/00」「-13/30」が別行になっている場合は連結する
            if line.startswith("-") and pending_fi is not None:
                r.fi_raw[-1] = r.fi_raw[-1] + line
                pending_fi = None
            else:
                r.fi_raw.append(line)
                pending_fi = line
            continue
        pending_fi = None
        # それ以外のテキストは備考として扱う（ナビゲーション文字列は除外）
        r.remarks.append(line)

    if r.theme_code is None and theme_hint:
        r.theme_code = theme_hint
        r.warnings.append("テーマコードを HTML から読み取れなかったため、ファイル名から補完しました")
    if theme_hint and r.theme_code and r.theme_code != theme_hint:
        r.warnings.append(f"ファイル名のテーマ {theme_hint} と HTML のテーマ {r.theme_code} が一致しません")

    # ---- 2) 本文（観点・Fターム） -------------------------------------------
    pending_codes = deque()
    cur_vp = None
    awaiting_vp_name = None
    vp_seen = set()
    term_order = 0

    for line in body:
        if is_nav(line):
            continue
        for kind, tok in split_line_to_tokens(line):
            r.tokens.append((kind, tok))
            if kind == "vp":
                continue
            if kind == "fi":
                # 観点の近くにある FI 表記（例: E04F13/08-13/18@Z）もテーマの FI 情報として保持
                r.fi_raw.append(tok)
                continue
            if kind == "code":
                vp, num = RE_CODE.match(tok).groups()
                if num == "00":
                    if pending_codes:
                        r.warnings.append(
                            f"説明文が見つからないコードがあります: {', '.join(pending_codes)}")
                        pending_codes.clear()
                    cur_vp = vp
                    if vp in vp_seen:
                        r.warnings.append(f"観点 {vp} が重複しています")
                    vp_seen.add(vp)
                    awaiting_vp_name = vp
                    r.viewpoints.append([vp, None])
                else:
                    if cur_vp is not None and vp != cur_vp:
                        r.warnings.append(f"観点 {cur_vp} の中に別観点のコード {tok} があります")
                    pending_codes.append(tok)
                continue
            if kind == "desc":
                if not pending_codes:
                    r.warnings.append(f"対応するコードがない説明文: {tok}")
                    continue
                code = pending_codes.popleft()
                label_raw = tok
                depth = leading_dots(label_raw)
                label = strip_dots(label_raw)
                asterisk = 0
                if label and label[-1] in ASTERISKS:
                    asterisk = 1
                    label = label.rstrip(ASTERISKS).rstrip()
                term_order += 1
                r.terms.append(dict(code=code, depth=depth, label=label,
                                    label_raw=label_raw, asterisk=asterisk,
                                    order=term_order))
                continue
            if kind == "text":
                if awaiting_vp_name:
                    r.viewpoints[-1][1] = tok
                    awaiting_vp_name = None
                else:
                    r.warnings.append(f"分類できないテキスト: {tok}")

    if pending_codes:
        r.warnings.append(f"説明文が見つからないコードがあります: {', '.join(pending_codes)}")
    r.fi_raw = list(dict.fromkeys(r.fi_raw))          # 同じ FI 表記の重複を除く（順序は保持）
    for vp, name in r.viewpoints:
        if not name:
            r.warnings.append(f"観点 {vp}00 の名称を読み取れませんでした")
    if r.page_status == "empty_list":
        if r.terms:   # 念のため：判定と内容が食い違う場合は通常のページとして扱う
            r.page_status = "ok"
            r.warnings.append("一覧表なしと判定しましたが Fターム が見つかったため、一覧表ありとして登録します")
    elif not r.terms:
        r.warnings.append("Fタームを1件も読み取れませんでした（HTML の構造を確認してください）")
    return r


# ---------------------------------------------------------------------------
# 階層・参照の計算
# ---------------------------------------------------------------------------
def build_rows(r: ParseResult):
    """fterms / ancestors / refs の行を作る。"""
    theme = r.theme_code
    vp_name = {vp: (name or "") for vp, name in r.viewpoints}
    vp_order = {vp: k for k, (vp, _) in enumerate(r.viewpoints, 1)}

    fterms = []
    ancestors = []
    refs = []
    sort = 0

    # 観点見出し（XX00）を depth 0 の行として先に作る
    heads = {}
    for vp, name in r.viewpoints:
        code = f"{vp}00"
        heads[vp] = dict(fterm=theme + code, code=code, viewpoint=vp, number="00", depth=0,
                         label=name or "", label_raw=name or "", asterisk=0,
                         parent=None, path_codes=[code], path_labels=[name or ""],
                         is_head=1)

    # 観点ごとにスタックで親を決める
    stacks = {vp: [heads[vp]] for vp in heads}
    terms_by_vp = {}
    for t in r.terms:
        vp = t["code"][:2]
        terms_by_vp.setdefault(vp, []).append(t)

    ordered_vps = [vp for vp, _ in r.viewpoints] + \
                  [vp for vp in terms_by_vp if vp not in vp_order]

    for vp in ordered_vps:
        if vp not in heads:
            r.warnings.append(f"観点 {vp}00 の見出しがないため、仮の見出しを作成しました")
            code = f"{vp}00"
            heads[vp] = dict(fterm=theme + code, code=code, viewpoint=vp, number="00",
                             depth=0, label="", label_raw="", asterisk=0, parent=None,
                             path_codes=[code], path_labels=[""], is_head=1)
            stacks[vp] = [heads[vp]]
        head = heads[vp]
        sort += 1
        head["sort"] = sort
        fterms.append(head)
        stack = stacks[vp]
        for t in terms_by_vp.get(vp, []):
            depth = t["depth"]
            if depth < 1:
                r.warnings.append(f"{t['code']} の階層（・の数）を読み取れませんでした")
                depth = 1
            # 親は「直前の depth-1 のターム」。飛び階層がある場合は最も近い浅いものを親にする
            while len(stack) > depth:
                stack.pop()
            if len(stack) < depth:
                r.warnings.append(f"{t['code']} の階層が飛んでいます（・{depth}個）")
            parent = stack[-1]
            row = dict(fterm=theme + t["code"], code=t["code"], viewpoint=vp,
                       number=t["code"][2:], depth=depth, label=t["label"],
                       label_raw=t["label_raw"], asterisk=t["asterisk"],
                       parent=parent["fterm"],
                       path_codes=parent["path_codes"] + [t["code"]],
                       path_labels=parent["path_labels"] + [t["label"]],
                       is_head=0)
            sort += 1
            row["sort"] = sort
            fterms.append(row)
            stack.append(row)

    # 祖先テーブル（自分自身 distance=0 を含む）
    for row in fterms:
        chain = row["path_codes"]
        n = len(chain)
        for k, c in enumerate(chain):
            ancestors.append((row["fterm"], theme + c, n - 1 - k))

    # 説明文中の参照
    for row in fterms:
        if row["is_head"]:
            continue
        ln = norm(row["label"])
        for m in RE_REF.finditer(ln):
            ref_theme = m.group("theme") or theme
            vp = m.group("vp")
            n1 = m.group("n1")
            n2 = m.group("n2")
            open_range = 1 if (m.group(0).rstrip()[-1] in "～〜~-ー" and not n2) else 0
            ref_from = f"{ref_theme}{vp}{n1}"
            ref_to = f"{ref_theme}{vp}{n2}" if n2 else (None if open_range else ref_from)
            if ref_from == row["fterm"] and ref_to == row["fterm"]:
                continue
            refs.append((row["fterm"], ref_from, ref_to, open_range, row["label"]))

    return fterms, ancestors, refs, vp_name


RE_FI_PIECE = re.compile(
    r"^(?P<sc1>[A-H]\d{2}[A-Z])?(?P<from>\d+/\d+(?:,\d+)?)(?P<fc1>@[0-9A-Z]+)?"
    r"(?:-(?P<sc2>[A-H]\d{2}[A-Z])?(?P<to>\d+/\d+(?:,\d+)?)(?P<fc2>@[0-9A-Z]+)?)?$")


def parse_fi(fi_raw_list):
    """
    FI の表記を範囲ごとに分解する。
      E04F13/00 -13/30              → E04F13/00 ～ E04F13/30
      H03F1/00-1/28;3/00-3/195      → H03F1/00～H03F1/28、H03F3/00～H03F3/195（サブクラスを引き継ぐ）
      H01L21/30-21/30,531@Z         → 分冊識別記号（,531）・ファセット（@Z）も保持
      A01D41/12@B-41/12@D           → fi_from=A01D41/12@B, fi_to=A01D41/12@D
    分解できない部分は fi_from/fi_to を空にして原文だけ残す。
    """
    out = []
    seq = 0
    for raw in fi_raw_list:
        s = norm(raw).replace(" ", "").replace("/", "/").replace("－", "-")
        sc = None
        for piece in [x for x in re.split(r"[;；]", s) if x]:
            seq += 1
            m = RE_FI_PIECE.match(piece)
            if not m or not (m.group("sc1") or sc):
                out.append((seq, raw, None, None, None))
                continue
            sc = m.group("sc1") or sc
            fc1, fc2 = m.group("fc1"), m.group("fc2")
            fi_from = sc + m.group("from") + (fc1 or "")
            if m.group("to"):
                sc_to = m.group("sc2") or sc
                fi_to = sc_to + m.group("to") + (fc2 or "")
            else:
                fi_to = fi_from
            # 範囲の末尾にだけ付いたファセット（E04F13/08-13/18@Z）は範囲全体に掛かるものとして facet 列にも入れる
            facet = fc2 if (fc2 and not fc1) else (fc1 if not m.group("to") else None)
            if facet and not fc1:
                fi_to = fi_to[: -len(facet)]
            out.append((seq, raw, fi_from, fi_to, facet))
    return out


# ---------------------------------------------------------------------------
# データベース
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS themes (
    theme_code    TEXT PRIMARY KEY,
    theme_name    TEXT,
    tech_field    TEXT,
    remarks       TEXT,
    source_file   TEXT,
    source_url    TEXT,
    fetched_date  TEXT,
    parsed_at     TEXT,
    n_viewpoints  INTEGER,
    n_fterms      INTEGER,
    list_status   TEXT                 -- 一覧表あり / 一覧表なし
);
CREATE TABLE IF NOT EXISTS theme_fi_ranges (
    theme_code  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    fi_raw      TEXT,
    fi_from     TEXT,
    fi_to       TEXT,
    facet       TEXT,
    PRIMARY KEY (theme_code, seq)
);
CREATE TABLE IF NOT EXISTS viewpoints (
    theme_code      TEXT NOT NULL,
    viewpoint       TEXT NOT NULL,
    viewpoint_name  TEXT,
    sort_order      INTEGER,
    PRIMARY KEY (theme_code, viewpoint)
);
CREATE TABLE IF NOT EXISTS fterms (
    fterm              TEXT PRIMARY KEY,   -- 2E110AA09
    theme_code         TEXT NOT NULL,      -- 2E110
    viewpoint          TEXT NOT NULL,      -- AA
    number             TEXT NOT NULL,      -- 09
    code               TEXT NOT NULL,      -- AA09
    depth              INTEGER NOT NULL,   -- 観点見出し=0, ・=1, ・・=2 ...
    label              TEXT,               -- ・と末尾＊を除いた説明文（原文）
    label_raw          TEXT,               -- HTML 上の表記そのまま
    label_norm         TEXT,               -- NFKC 正規化（検索用）
    asterisk           INTEGER,            -- 末尾に＊があれば 1
    is_viewpoint_head  INTEGER,            -- XX00 なら 1
    parent_fterm       TEXT,
    path_codes         TEXT,               -- AA00/AA07/AA08/AA09
    path_label         TEXT,               -- 目的，効果又は機能 > 流体に関するもの > ...
    sort_order         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fterms_theme  ON fterms(theme_code);
CREATE INDEX IF NOT EXISTS idx_fterms_parent ON fterms(parent_fterm);
CREATE TABLE IF NOT EXISTS fterm_ancestors (
    fterm     TEXT NOT NULL,
    ancestor  TEXT NOT NULL,
    distance  INTEGER NOT NULL,            -- 0=自分自身, 1=親, 2=祖父 ...
    PRIMARY KEY (fterm, ancestor)
);
CREATE INDEX IF NOT EXISTS idx_anc_ancestor ON fterm_ancestors(ancestor);
CREATE TABLE IF NOT EXISTS fterm_refs (
    from_fterm  TEXT NOT NULL,
    ref_from    TEXT NOT NULL,
    ref_to      TEXT,                      -- 範囲の終わり（単独参照なら ref_from と同じ）
    ref_open    INTEGER,                   -- 「ＣＣ１３～」のような終わりのない範囲なら 1
    ref_text    TEXT
);
CREATE INDEX IF NOT EXISTS idx_refs_from ON fterm_refs(from_fterm);
CREATE TABLE IF NOT EXISTS parse_log (
    logged_at    TEXT,
    theme_code   TEXT,
    source_file  TEXT,
    level        TEXT,
    message      TEXT
);
"""


LIST_OK = "一覧表あり"
LIST_NONE = "一覧表なし"


def init_db(conn):
    conn.executescript(SCHEMA)
    # 旧版のスクリプトで作った DB には list_status 列がないので追加する
    cols = [c[1] for c in conn.execute("PRAGMA table_info(themes)")]
    if "list_status" not in cols:
        with conn:
            conn.execute("ALTER TABLE themes ADD COLUMN list_status TEXT")
            conn.execute("UPDATE themes SET list_status=? WHERE n_fterms > 0", (LIST_OK,))
            conn.execute("UPDATE themes SET list_status=? WHERE COALESCE(n_fterms, 0) = 0", (LIST_NONE,))


def delete_theme(cur, theme):
    cur.execute("DELETE FROM fterm_refs WHERE from_fterm IN (SELECT fterm FROM fterms WHERE theme_code=?)", (theme,))
    cur.execute("DELETE FROM fterm_ancestors WHERE fterm IN (SELECT fterm FROM fterms WHERE theme_code=?)", (theme,))
    cur.execute("DELETE FROM fterms WHERE theme_code=?", (theme,))
    cur.execute("DELETE FROM viewpoints WHERE theme_code=?", (theme,))
    cur.execute("DELETE FROM theme_fi_ranges WHERE theme_code=?", (theme,))
    cur.execute("DELETE FROM themes WHERE theme_code=?", (theme,))


def save(conn, r: ParseResult, source_file: str, fetched_date: str):
    fterms, ancestors, refs, _ = build_rows(r)
    theme = r.theme_code
    now = dt.datetime.now().isoformat(timespec="seconds")
    cur = conn.cursor()
    with conn:
        delete_theme(cur, theme)
        n_terms = sum(1 for f in fterms if not f["is_head"])
        cur.execute(
            "INSERT INTO themes (theme_code, theme_name, tech_field, remarks, source_file, source_url,"
            " fetched_date, parsed_at, n_viewpoints, n_fterms, list_status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (theme, r.theme_name, r.tech_field, "\n".join(r.remarks) or None,
             os.path.basename(source_file), SOURCE_URL_TEMPLATE.format(theme=theme),
             fetched_date, now, len(r.viewpoints), n_terms,
             LIST_OK if n_terms else LIST_NONE))
        cur.executemany(
            "INSERT INTO theme_fi_ranges VALUES (?,?,?,?,?,?)",
            [(theme,) + row for row in parse_fi(r.fi_raw)])
        cur.executemany(
            "INSERT INTO viewpoints VALUES (?,?,?,?)",
            [(theme, vp, name, k) for k, (vp, name) in enumerate(r.viewpoints, 1)])
        cur.executemany(
            "INSERT OR REPLACE INTO fterms VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(f["fterm"], theme, f["viewpoint"], f["number"], f["code"], f["depth"],
              f["label"], f["label_raw"], norm(f["label"]), f["asterisk"], f["is_head"],
              f["parent"], "/".join(f["path_codes"]), " > ".join(f["path_labels"]),
              f["sort"]) for f in fterms])
        cur.executemany("INSERT OR REPLACE INTO fterm_ancestors VALUES (?,?,?)", ancestors)
        cur.executemany("INSERT INTO fterm_refs VALUES (?,?,?,?,?)", refs)
        for w in r.warnings:
            cur.execute("INSERT INTO parse_log VALUES (?,?,?,?,?)",
                        (now, theme, os.path.basename(source_file), "WARN", w))
    return fterms


# ---------------------------------------------------------------------------
# 入力ファイルの扱い
# ---------------------------------------------------------------------------
def expand_inputs(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            files.extend(sorted(glob.glob(os.path.join(p, "*.htm*"))))
        elif any(ch in p for ch in "*?["):
            files.extend(sorted(glob.glob(p)))      # Windows ではシェルが展開しないため
        elif os.path.isfile(p):
            files.append(p)
        else:
            print(f"[警告] 見つかりません: {p}", file=sys.stderr)
    return files


def fetched_date_of(path, default_date=None):
    m = RE_DATE_IN_NAME.search(os.path.basename(path))
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            pass
    if default_date:
        return default_date
    return dt.date.fromtimestamp(os.path.getmtime(path)).isoformat()


def main(argv=None):
    ap = argparse.ArgumentParser(description="Fタームリスト HTML → SQLite")
    ap.add_argument("inputs", nargs="+", help="HTML ファイル、フォルダ、またはワイルドカード")
    ap.add_argument("--db", default="fterm.sqlite", help="出力先 SQLite ファイル（既定: fterm.sqlite）")
    ap.add_argument("--date", help="取得日（YYYY-MM-DD）。ファイル名に日付がない場合に使用")
    ap.add_argument("--force", action="store_true", help="DB より古い取得日の HTML でも上書きする")
    ap.add_argument("--dump-tokens", action="store_true", help="解析途中のトークンを表示（DB には書き込まない）")
    args = ap.parse_args(argv)

    files = expand_inputs(args.inputs)
    if not files:
        sys.exit("処理する HTML ファイルがありません")

    # 同じテーマが複数ある場合は取得日の古い順に処理し、最新が残るようにする
    items = []
    for f in files:
        m = RE_THEME_IN_NAME.search(os.path.basename(f))
        items.append((fetched_date_of(f, args.date), f, m.group(1).upper() if m else None))
    items.sort()

    if args.dump_tokens:
        for date, f, hint in items:
            with open(f, "rb") as fh:
                r = parse_fterm_html(fh.read(), hint)
            print(f"===== {f}  theme={r.theme_code} name={r.theme_name} field={r.tech_field}"
                  f" page={r.page_status}")
            print("remarks:", r.remarks)
            print("fi:", r.fi_raw)
            for kind, tok in r.tokens:
                print(f"  {kind:5s} {tok}")
            for w in r.warnings:
                print("  [WARN]", w)
        return

    conn = sqlite3.connect(args.db)
    init_db(conn)

    ok = empty = skipped = notfound = failed = 0
    for date, f, hint in items:
        with open(f, "rb") as fh:
            r = parse_fterm_html(fh.read(), hint)
        if r.page_status == "not_found":
            print(f"[スキップ] ページなし（404 Not Found）: {f}")
            notfound += 1
            continue
        if not r.theme_code:
            failed += 1
            print(f"[エラー] テーマコードを特定できません: {f}", file=sys.stderr)
            continue
        if not r.terms and r.page_status != "empty_list":
            print(f"[エラー] Fタームを読み取れませんでした: {f}（--dump-tokens で確認してください）",
                  file=sys.stderr)
            failed += 1
            continue
        row = conn.execute("SELECT fetched_date, list_status FROM themes WHERE theme_code=?",
                           (r.theme_code,)).fetchone()
        if row and row[0] and row[0] > date and not args.force:
            print(f"[スキップ] {r.theme_code}: DB の取得日 {row[0]} の方が新しい（{f}）")
            skipped += 1
            continue
        if row and row[1] == LIST_OK and r.page_status == "empty_list":
            r.warnings.append("DB では一覧表ありでしたが、今回の HTML では一覧表がありません（一覧表なしで上書き）")
        fterms = save(conn, r, f, date)
        n = sum(1 for x in fterms if not x["is_head"])
        if n:
            print(f"[登録] {r.theme_code} {r.theme_name or ''}  観点 {len(r.viewpoints)} / Fターム {n} 件"
                  f"  (取得日 {date})")
            ok += 1
        else:
            print(f"[登録] {r.theme_code} {r.theme_name or ''}  一覧表なし（テーマ情報のみ）  (取得日 {date})")
            empty += 1
        for w in r.warnings:
            print(f"    [警告] {w}")

    total = conn.execute("SELECT COUNT(*) FROM themes").fetchone()[0]
    n_none = conn.execute("SELECT COUNT(*) FROM themes WHERE list_status=?", (LIST_NONE,)).fetchone()[0]
    conn.close()
    print(f"完了: 登録 {ok + empty} テーマ（一覧表あり {ok} / 一覧表なし {empty}）"
          f" / スキップ {skipped} / ページなし {notfound} / エラー {failed}"
          f"  （DB 内のテーマ数 {total}、うち一覧表なし {n_none}）  → {args.db}")


if __name__ == "__main__":
    main()
