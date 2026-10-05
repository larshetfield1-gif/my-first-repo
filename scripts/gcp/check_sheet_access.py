#!/usr/bin/env python3
"""サービスアカウントでスプレッドシートに接続できるか確認する（読み取り専用）。

使い方:
    python check_sheet_access.py <スプレッドシートのIDまたはURL>

確認すること:
    - 鍵ファイルを読み込めること
    - 鍵ファイルの権限が 600 であること
    - スプレッドシートのタイトルとタブ名を取得できること

何も書き込まない。スコープは spreadsheets.readonly のみ。
鍵の保存先は環境変数 SA_KEY_PATH で変更できる（既定: ~/.config/marunage-kun1/sa-key.json）。
スプレッドシートの ID はコードに書かず、毎回引数で渡す。
"""
from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

KEY_PATH = Path(
    os.environ.get("SA_KEY_PATH", "~/.config/marunage-kun1/sa-key.json")
).expanduser()
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
API_URL = "https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
URL_ID_PATTERN = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")
ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{20,}$")


def extract_sheet_id(text: str) -> str:
    """ID または共有 URL からスプレッドシート ID を取り出す。"""
    text = text.strip()
    match = URL_ID_PATTERN.search(text)
    if match:
        return match.group(1)
    if ID_PATTERN.match(text):
        return text
    raise ValueError("スプレッドシートの ID（または URL）の形式が正しくありません。")


def fail(message: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"[NG] {message}", file=sys.stderr)
    sys.exit(1)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        fail("使い方: python check_sheet_access.py <スプレッドシートのIDまたはURL>")

    try:
        sheet_id = extract_sheet_id(argv[1])
    except ValueError as exc:
        fail(str(exc))

    if not KEY_PATH.is_file():
        fail(f"鍵ファイルが見つかりません: {KEY_PATH}")
    mode = stat.S_IMODE(KEY_PATH.stat().st_mode)
    if mode & 0o077:
        fail(f"鍵ファイルの権限が {mode:o} です。他の人に読まれないよう 'chmod 600 {KEY_PATH}' を実行してください。")
    print(f"[OK] 鍵ファイルあり、権限 {mode:o}")

    try:
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
        import requests
    except ImportError:
        fail("ライブラリが足りません。'pip install google-auth requests' を実行してください。")

    try:
        creds = service_account.Credentials.from_service_account_file(
            str(KEY_PATH), scopes=SCOPES
        )
    except (ValueError, KeyError) as exc:
        fail(f"鍵ファイルを読み込めません（形式が壊れている可能性）: {type(exc).__name__}")
    print(f"[OK] 鍵を読み込みました: {creds.service_account_email}")

    session = AuthorizedSession(creds)
    try:
        response = session.get(
            API_URL.format(sheet_id=sheet_id),
            params={"fields": "properties.title,sheets.properties.title"},
            timeout=30,
        )
    except (GoogleAuthError, requests.RequestException) as exc:
        fail(f"Google に接続できませんでした: {type(exc).__name__}: {exc}")

    if response.status_code == 200:
        data = response.json()
        title = data.get("properties", {}).get("title", "(タイトル不明)")
        tabs = [s["properties"]["title"] for s in data.get("sheets", [])]
        print(f"[OK] スプレッドシートを読めました: 「{title}」")
        print(f"     タブ ({len(tabs)} 枚): {', '.join(tabs)}")
        return 0

    try:
        detail = response.json().get("error", {}).get("message", "")
    except ValueError:
        detail = ""
    hints = {
        403: f"権限がありません。スプレッドシートの「共有」に {creds.service_account_email} が追加されているか確認してください。",
        404: "スプレッドシートが見つかりません。ID が正しいか確認してください。",
    }
    fail(f"HTTP {response.status_code}: {hints.get(response.status_code, '予期しないエラーです。')} {detail}".strip())


if __name__ == "__main__":
    sys.exit(main(sys.argv))
