# 自宅 Windows PC で TeamAgent を開発する（WSL2・軽量セットアップ）

目的: Windows の自宅PCで **コード編集 + pytest + Claude Code** ができるようにする
（Bot常駐・スクレイパ・RDS・terraform などの「フル稼働」は対象外＝§6に追記手順）。

> **重要**: この軽量セットアップは **会社の秘密情報・AWS認証・DB・ffmpeg・Chrome が一切不要**。
> pytest は AWS/DB/ffmpeg/Chrome をすべてモック/グレースフルにしているので、**鍵ゼロで `579 passed` 相当が通る**。
> → 自宅PCに会社のSlackトークンやクライアントのGoogle認証を置かなくてよい＝統制上クリーン。

## なぜ WSL2 か
このリポジトリは bash スクリプト・venv・Linux前提のツールが多い。Windowsネイティブだと
パス/改行/venv で罠が多発する。**WSL2(Ubuntu) なら Mac/Linux と同じ手順**で素直に動く。

---

## 手順

### 1. WSL2 + Ubuntu（Windows側・PowerShellを管理者で）
```powershell
wsl --install
```
→ 再起動。初回 Ubuntu 起動で username/password を設定。
（最新の `wsl --install` は既定で **Ubuntu 24.04 = Python 3.12** が入る＝要件 `>=3.11` を満たす）

### 2. 基本ツール（以降は Ubuntu/WSL の中で）
```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip build-essential
python3 --version    # 3.11 以上であること（3.12想定）。古い場合は deadsnakes PPA で 3.12 を入れる
```

### 3. GitHub 認証 + クローン
```bash
sudo apt install -y gh
gh auth login        # GitHub.com → HTTPS → ブラウザ認証（表示コードをブラウザで入力）
git clone https://github.com/noirelumiere00/TeamAgent.git
cd TeamAgent
```

### 4. venv + 依存 + テスト
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .                                     # pyproject の依存（初回は大きめ・数分）
pip install pytest pytest-asyncio ruff mypy bandit   # 開発ツール
python -m pytest -q                                  # → 579 passed 相当（AWS/DB不要）
ruff check src/teamagent && mypy src/teamagent       # 静的+型チェック
```
- `pip install -e .` が重い場合あり（埋め込みモデル系の依存が入ると数百MB〜）。容量/時間に注意。
- pgvector の実DB検証テストは `TEAMAGENT_DB_DSN` 未設定で自動 skip（正常）。

### 5. Claude Code を入れる（自宅でも開発できる）
```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -   # Node 20
sudo apt install -y nodejs
npm install -g @anthropic-ai/claude-code
cd ~/TeamAgent
claude               # 初回は自分のアカウントでログイン
```

### 6. 編集環境（任意・どちらか）
- **VS Code**（Windows）+ 拡張「**WSL**」→ WSL内の `~/TeamAgent` を開く。編集はWindows・実行はLinux。
- もしくは **Claude Code + nano/vim** で完結。

---

## 秘密情報について（軽量スコープなら不要）
- **編集 + pytest + Claude Code には会社の秘密情報は一切要らない**。pytest は外部I/Oを全部
  モック/グレースフルにしているので鍵ゼロで通る。
- よって自宅PCに会社の Slack トークン・クライアントの Google OAuth・DB 認証を**置かなくてよい**。
  万一の流出面が無く、統制上もクリーン。

## §6 あとで「フル稼働」にしたくなったら（任意・追加）
自宅PCで実際に Bot/スクレイパ/AWS まで動かしたくなった時だけ、以下を追加（＝会社の鍵を自宅PCに
置く話になるので、本当に必要な時だけ＆統制と相談）:
- `sudo apt install -y ffmpeg`（動画proxy/フレーム/サムネ色）
- `npx @puppeteer/browsers install chrome`（tiktok_search の Puppeteer 用 Chrome）
- AWS CLI + `aws configure`（自分のIAMキー）→ `source scripts/load_secrets.sh` が Secrets Manager から取得
- `.env.production` は **1Password 等の安全手段で移送**（チャット/メール厳禁）
- RDS 接続は別ターミナルで `aws ssm start-session`（踏み台トンネル）
- terraform は tfenv（インフラ作業をする場合のみ）

> 注: Bot を会社プロキシ外で動かす目的なら、自宅PCは「住宅IP」なので TikTok のデータセンターIP
> 遮断を避けられる利点がある（AWS移設より有利な点）。ただし会社の鍵が個人PCに載る統制面は要検討。
