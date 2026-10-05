#!/usr/bin/env python3
"""サービスアカウントでスプレッドシートを更新する。

標準は「確認のみ」で、何も書き込まない。--apply を付けたときだけ書き込む。

使い方:
    # 1) 指定した範囲を上書きする（まず確認のみ）
    python sheet_update.py <ID または URL> --tab <タブ名> --range Q3 --values '[["2026/10/31"]]'
    # 内容を確認して問題なければ、同じコマンドの最後に --apply を付ける
    python sheet_update.py <ID または URL> --tab <タブ名> --range Q3 --values '[["2026/10/31"]]' --apply

    # 2) 表の末尾に行を追加する
    python sheet_update.py <ID または URL> --tab <タブ名> --append --values '[["a","b","c"]]' --apply

    # 3) 行番号がずれていないか、書き込む前に契約名で照合する（何個でも指定できる）
    python sheet_update.py <ID または URL> --tab <タブ名> --range Q20:Q21 \\
        --values '[["2027/04/06"],["2027/04/06"]]' --expect C20=契約A --expect C21=契約B

    # 4) 空のセルにだけ書く（すでに何か入っていたら中止する。新しい行の追加に使う）
    python sheet_update.py <ID または URL> --tab <タブ名> --range A42:C42 --values '[[41,"","名前"]]' --only-if-empty

安全のための決まり:
    - 1 行目（見出し）への書き込みは --allow-header を付けない限り拒否する
    - 範囲の大きさと値の大きさが違う場合は何もせず止まる
    - --expect で指定したセルの中身が違う場合は、確認のみのときも含めて何もせず止まる
    - --only-if-empty を付けると、書き込み先に何か入っている場合は何もせず止まる
    - 値は RAW（入力した文字をそのまま保存）が標準。外部から来た文字が数式として
      実行されるのを防ぐため。日付や数式として解釈させたいときだけ --user-entered を付ける

鍵の保存先は環境変数 SA_KEY_PATH で変更できる（既定: ~/.config/marunage-kun1/sa-key.json）。
スプレッドシートの ID はコードに書かず、毎回引数で渡す。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

KEY_PATH = Path(
    os.environ.get("SA_KEY_PATH", "~/.config/marunage-kun1/sa-key.json")
).expanduser()
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
BASE_URL = "https://sheets.googleapis.com/v4/spreadsheets"
URL_ID_PATTERN = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,}$")
CELL_PATTERN = re.compile(r"^([A-Z]{1,3})(\d+)$")
MAX_DIFF_LINES = 50


def fail(message: str) -> None:
    """エラーを表示して終了する。"""
    print(f"[NG] {message}", file=sys.stderr)
    sys.exit(1)


def extract_sheet_id(text: str) -> str:
    """ID または共有 URL からスプレッドシート ID を取り出す。"""
    text = text.strip()
    match = URL_ID_PATTERN.search(text)
    if match:
        return match.group(1)
    if ID_PATTERN.match(text):
        return text
    raise ValueError("スプレッドシートの ID（または URL）の形式が正しくありません。")


def col_to_index(col: str) -> int:
    """列の英字を 0 始まりの番号にする（A→0, Z→25, AA→26）。"""
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def index_to_col(index: int) -> str:
    """0 始まりの列番号を英字にする（0→A, 26→AA）。"""
    index += 1
    letters = ""
    while index:
        index, rem = divmod(index - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def parse_range(rng: str) -> tuple[tuple[int, int], tuple[int, int]]:
    """'A2:D3' や 'Q3' を ((開始列, 開始行), (終了列, 終了行)) にする。列は 0 始まり、行は 1 始まり。"""
    parts = rng.upper().replace("$", "").split(":")
    if len(parts) > 2:
        raise ValueError(f"範囲の書き方が正しくありません: {rng}")
    cells: list[tuple[int, int]] = []
    for part in parts:
        match = CELL_PATTERN.match(part)
        if not match:
            raise ValueError(
                f"範囲は 'A2' や 'A2:D5' のように、列と行の両方を書いてください: {rng}"
            )
        cells.append((col_to_index(match.group(1)), int(match.group(2))))
    start, end = cells[0], cells[-1]
    if end[0] < start[0] or end[1] < start[1]:
        raise ValueError(f"範囲の終わりが始まりより前になっています: {rng}")
    if start[1] < 1:
        raise ValueError(f"行番号は 1 以上にしてください: {rng}")
    return start, end


def quote_tab(tab: str) -> str:
    """タブ名を A1 表記用にシングルクォートで囲む。名前の中の ' は '' にする。"""
    return "'" + tab.replace("'", "''") + "'"


def parse_values(text: str) -> list[list[Any]]:
    """--values の JSON を検証して、行のリストにする。"""
    try:
        values = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"--values が JSON として読めません（{exc.msg}）。例: '[[\"a\",\"b\"]]'"
        ) from exc
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(row, list) and row for row in values)
    ):
        raise ValueError("--values は行のリストにしてください。例: '[[\"a\",\"b\"],[\"c\",\"d\"]]'")
    for row in values:
        for cell in row:
            if isinstance(cell, (dict, list)):
                raise ValueError("--values の各セルは、文字・数字・true/false・null のどれかにしてください。")
    return values


def parse_expects(items: list[str]) -> list[tuple[str, str]]:
    """--expect 'C20=契約名' を (セル, 期待する文字) のリストにする。"""
    result: list[tuple[str, str]] = []
    for item in items:
        cell, sep, expected = item.partition("=")
        cell = cell.strip().upper()
        if not sep or not CELL_PATTERN.match(cell) or not expected.strip():
            raise ValueError(f"--expect は 'C20=契約名' の形で書いてください: {item}")
        result.append((cell, expected.strip()))
    return result


def show(value: Any) -> str:
    """表示用。空は（空）にする。"""
    return "（空）" if value in ("", None) else str(value)


def api_error(response: Any, creds_email: str) -> None:
    """API のエラー応答を分かりやすい文にして終了する。"""
    try:
        detail = response.json().get("error", {}).get("message", "")
    except ValueError:
        detail = ""
    hints = {
        400: "リクエストの内容が正しくありません。タブ名と範囲を確認してください。",
        403: f"権限がありません。スプレッドシートの「共有」で {creds_email} が「編集者」になっているか確認してください。",
        404: "スプレッドシートが見つかりません。ID が正しいか確認してください。",
    }
    fail(
        f"HTTP {response.status_code}: "
        f"{hints.get(response.status_code, '予期しないエラーです。')} {detail}".strip()
    )


def check_expectations(
    session: Any, sheet_id: str, tab: str, expects: list[tuple[str, str]], account_email: str
) -> None:
    """指定セルの中身が期待どおりか確認し、違えば何も書かずに終了する。"""
    response = session.get(
        f"{BASE_URL}/{sheet_id}/values:batchGet",
        params={"ranges": [f"{tab}!{cell}" for cell, _ in expects]},
        timeout=30,
    )
    if response.status_code != 200:
        api_error(response, account_email)
    value_ranges = response.json().get("valueRanges", [])
    if len(value_ranges) != len(expects):
        fail("照合用のセルを読み取れませんでした。何も書き込まずに中止しました。")
    problems = []
    for (cell, expected), value_range in zip(expects, value_ranges):
        rows = value_range.get("values", [])
        actual = str(rows[0][0]).strip() if rows and rows[0] else ""
        if actual != expected:
            problems.append(f"  {cell}: 「{expected}」のはずが「{show(actual)}」でした")
    if problems:
        fail("行がずれている可能性があるため、何も書き込まずに中止しました。\n" + "\n".join(problems))
    print("事前照合 OK: " + "、".join(f"{c}={v}" for c, v in expects))


def run(args: argparse.Namespace, session: Any, account_email: str = "(不明)") -> int:
    """更新の本体。session は requests.Session 互換（テストでは偽物を渡す）。"""
    try:
        sheet_id = extract_sheet_id(args.sheet)
        values = parse_values(args.values)
    except ValueError as exc:
        fail(str(exc))

    tab = quote_tab(args.tab)
    value_option = "USER_ENTERED" if args.user_entered else "RAW"
    mode_label = "日付・数式として解釈" if args.user_entered else "入力どおりに保存"

    if args.append:
        if args.expect or args.only_if_empty:
            fail("--expect と --only-if-empty は --range と一緒に使ってください（--append とは併用できません）。")
        a1 = f"{tab}!A1"
        url = f"{BASE_URL}/{sheet_id}/values/{quote(a1, safe='')}:append"
        print(f"対象: {args.tab} の表の末尾に {len(values)} 行を追加  [{mode_label}]")
        for row in values:
            print("  + " + " | ".join(show(c) for c in row))
        if not args.apply:
            print("\n[確認のみ] まだ何も書き込んでいません。書き込むには --apply を付けてください。")
            return 0
        response = session.post(
            url,
            params={"valueInputOption": value_option, "insertDataOption": "INSERT_ROWS"},
            json={"majorDimension": "ROWS", "values": values},
            timeout=30,
        )
        if response.status_code != 200:
            api_error(response, account_email)
        updates = response.json().get("updates", {})
        print(f"\n[完了] {updates.get('updatedRows', len(values))} 行を追加しました: {updates.get('updatedRange', '')}")
        return 0

    try:
        (sc, sr), (ec, er) = parse_range(args.range)
    except ValueError as exc:
        fail(str(exc))
    rows, cols = er - sr + 1, ec - sc + 1
    if len(values) != rows or any(len(row) != cols for row in values):
        shape = f"{len(values)} 行 × {max(len(r) for r in values)} 列"
        fail(f"範囲は {rows} 行 × {cols} 列ですが、値は {shape} です。大きさを合わせてください。")
    if sr == 1 and not args.allow_header:
        fail("1 行目は見出しです。書き換える場合は --allow-header を付けてください。")

    a1 = f"{tab}!{index_to_col(sc)}{sr}" + (f":{index_to_col(ec)}{er}" if (sc, sr) != (ec, er) else "")
    url = f"{BASE_URL}/{sheet_id}/values/{quote(a1, safe='')}"

    try:
        expects = parse_expects(args.expect)
    except ValueError as exc:
        fail(str(exc))
    if expects:
        check_expectations(session, sheet_id, tab, expects, account_email)

    response = session.get(url, timeout=30)
    if response.status_code != 200:
        api_error(response, account_email)
    current = response.json().get("values", [])

    def current_at(r: int, c: int) -> Any:
        return current[r][c] if r < len(current) and c < len(current[r]) else ""

    if args.only_if_empty:
        occupied = [
            f"  {index_to_col(sc + c)}{sr + r}: {show(current_at(r, c))}"
            for r in range(rows)
            for c in range(cols)
            if str(current_at(r, c)) != ""
        ]
        if occupied:
            fail("書き込み先に、すでに値が入っています。何も書き込まずに中止しました。\n" + "\n".join(occupied))

    print(f"対象: {a1}  [{mode_label}]")
    changed = unchanged = 0
    lines: list[str] = []
    for r, row in enumerate(values):
        for c, new in enumerate(row):
            old = current_at(r, c)
            if str(old) == str(new):
                unchanged += 1
                continue
            changed += 1
            lines.append(f"  {index_to_col(sc + c)}{sr + r}: {show(old)}  →  {show(new)}")
    for line in lines[:MAX_DIFF_LINES]:
        print(line)
    if len(lines) > MAX_DIFF_LINES:
        print(f"  …ほか {len(lines) - MAX_DIFF_LINES} 件")
    print(f"変更 {changed} セル、変更なし {unchanged} セル")

    if not args.apply:
        print("\n[確認のみ] まだ何も書き込んでいません。書き込むには --apply を付けてください。")
        return 0
    if changed == 0:
        print("\n[完了] 変更がないため、書き込みは行いませんでした。")
        return 0

    response = session.put(
        url,
        params={"valueInputOption": value_option},
        json={"range": a1, "majorDimension": "ROWS", "values": values},
        timeout=30,
    )
    if response.status_code != 200:
        api_error(response, account_email)
    print(f"\n[完了] {response.json().get('updatedCells', changed)} セルを更新しました: {a1}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="サービスアカウントでスプレッドシートを更新する（標準は確認のみ）")
    parser.add_argument("sheet", help="スプレッドシートの ID または URL")
    parser.add_argument("--tab", required=True, help="タブ名")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--range", help="上書きする範囲。例: Q3 または A2:D2")
    target.add_argument("--append", action="store_true", help="表の末尾に行を追加する")
    parser.add_argument("--values", required=True, help='書き込む値。行のリストの JSON。例: \'[["a","b"]]\'')
    parser.add_argument("--apply", action="store_true", help="実際に書き込む（付けなければ確認のみ）")
    parser.add_argument("--user-entered", action="store_true", help="日付や数式として解釈させる（標準は入力どおり）")
    parser.add_argument(
        "--expect", action="append", default=[], metavar="セル=文字",
        help="書き込む前に、このセルの中身が一致するか確認する。例: --expect C20=契約名（何個でも指定可）",
    )
    parser.add_argument("--only-if-empty", action="store_true", help="書き込み先がすべて空のときだけ書き込む")
    parser.add_argument("--allow-header", action="store_true", help="1 行目（見出し）への書き込みを許可する")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv[1:])

    if not KEY_PATH.is_file():
        fail(f"鍵ファイルが見つかりません: {KEY_PATH}")
    mode = stat.S_IMODE(KEY_PATH.stat().st_mode)
    if mode & 0o077:
        fail(f"鍵ファイルの権限が {mode:o} です。'chmod 600 {KEY_PATH}' を実行してください。")

    try:
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
        import requests
    except ImportError:
        fail("ライブラリが足りません。'pip install google-auth requests' を実行してください。")

    try:
        creds = service_account.Credentials.from_service_account_file(str(KEY_PATH), scopes=SCOPES)
    except (ValueError, KeyError) as exc:
        fail(f"鍵ファイルを読み込めません: {type(exc).__name__}")

    try:
        return run(args, AuthorizedSession(creds), creds.service_account_email)
    except (GoogleAuthError, requests.RequestException) as exc:
        fail(f"Google に接続できませんでした: {type(exc).__name__}: {exc}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
