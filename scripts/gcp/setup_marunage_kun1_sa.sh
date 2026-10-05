#!/usr/bin/env bash
# =============================================================================
# setup_marunage_kun1_sa.sh
#
# まるなげクン1号用の Google Cloud サービスアカウントをセットアップする。
#
#   1. プロジェクト marunage-kun1 を作成（既存なら再利用）
#   2. Google Sheets API を有効化（SA 作成に必要な IAM API も併せて有効化）
#   3. サービスアカウント kun1-sa を作成（ロール付与なし）
#      ※ Google の仕様でアカウント名は 6〜30 文字。4 文字の "kun1" は作れないため kun1-sa を既定にしている
#   4. JSON 鍵を ~/.config/marunage-kun1/sa-key.json に保存し、権限を 600 にする
#   5. サービスアカウントのメールアドレスを表示
#
# 何度実行しても安全（冪等）。既存の鍵ファイルは上書きしない。
# 鍵の保存先が Git 管理下（かつ未 ignore）なら、鍵を作る前に中断する。
#
# 前提: gcloud CLI がインストール済みで、`gcloud auth login` 済みであること。
# 使い方: bash scripts/gcp/setup_marunage_kun1_sa.sh
#
# 環境変数で上書き可能:
#   PROJECT_ID (既定: marunage-kun1)  PROJECT_NAME (既定: PROJECT_ID と同じ)
#   SA_NAME    (既定: kun1-sa)        KEY_DIR      (既定: ~/.config/marunage-kun1)
# =============================================================================
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-marunage-kun1}"
PROJECT_NAME="${PROJECT_NAME:-$PROJECT_ID}"
SA_NAME="${SA_NAME:-kun1-sa}"
KEY_DIR="${KEY_DIR:-$HOME/.config/marunage-kun1}"
KEY_PATH="$KEY_DIR/sa-key.json"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
APIS=(sheets.googleapis.com iam.googleapis.com)

log() { printf '[setup] %s\n' "$*"; }
die() { printf '[setup] ERROR: %s\n' "$*" >&2; exit 1; }

# サービスアカウント名の形式チェック（Google の仕様: 6〜30 文字、英小文字で始まり、英小文字・数字・ハイフンのみ）
# 何かを作る前に確認して、ここで止める。
if [[ ! "$SA_NAME" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]; then
  die "サービスアカウント名 '$SA_NAME' は使えません（${#SA_NAME} 文字）。6〜30 文字で、英小文字で始まり、英小文字・数字・ハイフンのみにしてください。例: SA_NAME=kun1-sa"
fi

# --- 0. 前提チェック ---------------------------------------------------------
command -v gcloud >/dev/null 2>&1 \
  || die "gcloud CLI が見つかりません。https://cloud.google.com/sdk/docs/install を参照してください。"

ACTIVE_ACCOUNT="$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null || true)"
[[ -n "$ACTIVE_ACCOUNT" ]] \
  || die "gcloud が未認証です。先に 'gcloud auth login' を実行してください。"
log "認証アカウント: $ACTIVE_ACCOUNT"

# 鍵の保存先を先に用意し、Git 管理下に置かれていないことを鍵作成前に確認する
mkdir -p "$KEY_DIR"
chmod 700 "$KEY_DIR"
if git -C "$KEY_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if ! git -C "$KEY_DIR" check-ignore -q "$KEY_PATH"; then
    die "鍵の保存先 $KEY_DIR は Git 管理下にあり、$KEY_PATH は .gitignore されていません。鍵の漏洩を防ぐため中断します。"
  fi
  log "注意: $KEY_DIR は Git 管理下ですが、鍵ファイルは .gitignore 済みです。"
fi

# --- 1. プロジェクト ---------------------------------------------------------
if gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null 2>&1; then
  log "プロジェクト $PROJECT_ID は既に存在します（再利用）"
else
  log "プロジェクト $PROJECT_ID を作成します"
  gcloud projects create "$PROJECT_ID" --name="$PROJECT_NAME" \
    || die "プロジェクト作成に失敗しました。ID '$PROJECT_ID' が他者に使用済みの可能性があります。PROJECT_ID=<別のID> を指定して再実行してください。"
fi

# --- 2. API 有効化（作成直後は反映待ちが必要なことがあるためリトライ） ---------
log "API を有効化します: ${APIS[*]}"
attempt=1
until gcloud services enable "${APIS[@]}" --project="$PROJECT_ID"; do
  [[ $attempt -lt 6 ]] || die "API の有効化に失敗しました"
  log "プロジェクトの反映待ち... ($attempt/6) 10 秒後に再試行します"
  sleep 10
  attempt=$((attempt + 1))
done

# --- 3. サービスアカウント（ロール付与なし） ----------------------------------
if gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
  log "サービスアカウント $SA_EMAIL は既に存在します（再利用）"
else
  log "サービスアカウント $SA_NAME を作成します（ロールは付与しません）"
  gcloud iam service-accounts create "$SA_NAME" \
    --project="$PROJECT_ID" \
    --display-name="$SA_NAME" \
    --description="marunage-kun1 Google Sheets integration"
fi

# --- 4. JSON 鍵 ----------------------------------------------------------------
if [[ -f "$KEY_PATH" ]]; then
  log "鍵ファイル $KEY_PATH は既に存在するため上書きしません（再発行する場合は削除してから再実行）"
else
  log "JSON 鍵を作成します -> $KEY_PATH"
  (
    umask 077
    attempt=1
    until gcloud iam service-accounts keys create "$KEY_PATH" \
            --iam-account="$SA_EMAIL" --project="$PROJECT_ID"; do
      [[ $attempt -lt 5 ]] || exit 1
      printf '[setup] サービスアカウントの反映待ち... (%s/5) 5 秒後に再試行します\n' "$attempt"
      sleep 5
      attempt=$((attempt + 1))
    done
  ) || die "鍵の作成に失敗しました"
fi
chmod 600 "$KEY_PATH"

# 鍵ファイルが想定のサービスアカウントのものか検証（python3 がある環境のみ）
if command -v python3 >/dev/null 2>&1; then
  KEY_EMAIL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["client_email"])' "$KEY_PATH")"
  [[ "$KEY_EMAIL" == "$SA_EMAIL" ]] \
    || die "鍵ファイルの client_email ($KEY_EMAIL) が想定 ($SA_EMAIL) と一致しません。古い鍵が残っている可能性があります。"
fi

# --- 5. 結果表示 ---------------------------------------------------------------
KEY_MODE="$(stat -c '%a' "$KEY_PATH" 2>/dev/null || stat -f '%Lp' "$KEY_PATH")"
log "完了"
printf '\n'
printf 'サービスアカウントのメールアドレス: %s\n' "$SA_EMAIL"
printf '鍵ファイル: %s (権限 %s)\n' "$KEY_PATH" "$KEY_MODE"
printf '\n'
printf '※ ロールは付与していません。読み書きしたいスプレッドシートを上記メールアドレスに「共有」してください。\n'
printf '※ 鍵ファイルは絶対に Git にコミットしないでください。\n'
