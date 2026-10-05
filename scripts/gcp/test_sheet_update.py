"""sheet_update.py のテスト。Google には接続せず、偽物のセッションで動作を確認する。

実行: python -m pytest scripts/gcp/test_sheet_update.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import sheet_update as su  # noqa: E402

SHEET_ID = "A" * 44
TAB = "テスト"


class FakeResponse:
    def __init__(self, status: int = 200, body: dict | None = None):
        self.status_code = status
        self._body = body or {}

    def json(self) -> dict:
        return self._body


class FakeSession:
    """呼び出しを記録し、決められた応答を返す偽の requests.Session。"""

    def __init__(self, current: list[list[str]] | None = None, status: int = 200, error: dict | None = None):
        self.current = current or []
        self.status = status
        self.error = error
        self.calls: list[tuple[str, str, dict]] = []

    def _record(self, method: str, url: str, kwargs: dict) -> None:
        self.calls.append((method, url, kwargs))

    def get(self, url, **kwargs):
        self._record("GET", url, kwargs)
        if self.status != 200:
            return FakeResponse(self.status, self.error)
        return FakeResponse(200, {"values": self.current})

    def put(self, url, **kwargs):
        self._record("PUT", url, kwargs)
        if self.status != 200:
            return FakeResponse(self.status, self.error)
        return FakeResponse(200, {"updatedCells": sum(len(r) for r in kwargs["json"]["values"])})

    def post(self, url, **kwargs):
        self._record("POST", url, kwargs)
        return FakeResponse(200, {"updates": {"updatedRows": len(kwargs["json"]["values"]), "updatedRange": "x"}})

    def methods(self) -> list[str]:
        return [c[0] for c in self.calls]


def parse(*argv: str):
    return su.build_parser().parse_args([SHEET_ID, "--tab", TAB, *argv])


def test_dry_run_reads_but_never_writes(capsys):
    session = FakeSession(current=[["古い"]])
    assert su.run(parse("--range", "Q3", "--values", '[["新しい"]]'), session) == 0
    assert session.methods() == ["GET"]
    out = capsys.readouterr().out
    assert "古い  →  新しい" in out and "確認のみ" in out


def test_apply_writes_with_raw_and_encoded_range(capsys):
    session = FakeSession(current=[["古い"]])
    su.run(parse("--range", "Q3", "--values", '[["新しい"]]', "--apply"), session)
    assert session.methods() == ["GET", "PUT"]
    _, url, kwargs = session.calls[1]
    assert url.endswith(quote("'テスト'!Q3", safe=""))
    assert kwargs["params"] == {"valueInputOption": "RAW"}
    assert kwargs["json"]["values"] == [["新しい"]]


def test_user_entered_option(capsys):
    session = FakeSession()
    su.run(parse("--range", "Q3", "--values", '[["2026/10/31"]]', "--apply", "--user-entered"), session)
    assert session.calls[1][2]["params"] == {"valueInputOption": "USER_ENTERED"}


def test_no_write_when_nothing_changes(capsys):
    session = FakeSession(current=[["同じ"]])
    su.run(parse("--range", "Q3", "--values", '[["同じ"]]', "--apply"), session)
    assert session.methods() == ["GET"]
    assert "変更がないため" in capsys.readouterr().out


def test_header_row_is_protected(capsys):
    session = FakeSession()
    with pytest.raises(SystemExit):
        su.run(parse("--range", "A1", "--values", '[["x"]]', "--apply"), session)
    assert session.calls == []
    assert "見出し" in capsys.readouterr().err


def test_header_row_allowed_with_flag(capsys):
    session = FakeSession()
    su.run(parse("--range", "A1", "--values", '[["x"]]', "--apply", "--allow-header"), session)
    assert session.methods() == ["GET", "PUT"]


@pytest.mark.parametrize("rng,values", [("A2:D2", '[["a","b","c"]]'), ("A2:B3", '[["a","b"]]'), ("Q3", '[["a","b"]]')])
def test_shape_mismatch_is_refused(rng, values, capsys):
    session = FakeSession()
    with pytest.raises(SystemExit):
        su.run(parse("--range", rng, "--values", values, "--apply"), session)
    assert session.calls == []
    assert "大きさを合わせて" in capsys.readouterr().err


def test_multi_cell_diff_lists_only_changes(capsys):
    session = FakeSession(current=[["a", "b"], ["c", "d"]])
    su.run(parse("--range", "B2:C3", "--values", '[["a","X"],["c","d"]]'), session)
    out = capsys.readouterr().out
    assert "C2: b  →  X" in out and "変更 1 セル、変更なし 3 セル" in out


def test_append_dry_run_and_apply(capsys):
    session = FakeSession()
    su.run(parse("--append", "--values", '[["a","b"]]'), session)
    assert session.calls == []
    su.run(parse("--append", "--values", '[["a","b"]]', "--apply"), session)
    assert session.methods() == ["POST"]
    kwargs = session.calls[0][2]
    assert kwargs["params"] == {"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}
    assert session.calls[0][1].endswith(quote("'テスト'!A1", safe="") + ":append")


def test_tab_name_with_apostrophe_is_escaped():
    assert su.quote_tab("it's") == "'it''s'"


@pytest.mark.parametrize("bad", ["not json", "{}", "[]", "[[]]", '["a"]', '[[{"x":1}]]'])
def test_bad_values_are_refused(bad):
    with pytest.raises(ValueError):
        su.parse_values(bad)


def test_permission_error_mentions_sharing(capsys):
    session = FakeSession(status=403, error={"error": {"message": "The caller does not have permission"}})
    with pytest.raises(SystemExit):
        su.run(parse("--range", "Q3", "--values", '[["x"]]', "--apply"), session, "kun1-sa@example.com")
    err = capsys.readouterr().err
    assert "共有" in err and "kun1-sa@example.com" in err


def test_column_conversion_roundtrip():
    for letters, index in [("A", 0), ("Q", 16), ("U", 20), ("Z", 25), ("AA", 26)]:
        assert su.col_to_index(letters) == index
        assert su.index_to_col(index) == letters


def test_url_is_accepted_as_sheet():
    assert su.extract_sheet_id(f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0") == SHEET_ID
