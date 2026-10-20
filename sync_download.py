"""
sync_download.py  —  Windows PCで起動する同期スクリプト

Supabaseに新しい画像がアップロードされると自動でダウンロードし、
analyze_burn.py を実行して解析結果を保存する。

使い方:
    python sync_download.py           ← 常時監視モード
    python sync_download.py --once    ← 1回だけ同期して終了
"""

import json
import time
import urllib.request
import urllib.error
import os
import sys
import subprocess
from pathlib import Path
from datetime import datetime


# =====================================================
#  設定読み込み
# =====================================================
CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config():
    """config.json を読み込んで接続情報を返す"""
    if not CONFIG_PATH.exists():
        print("\n[エラー] config.json が見つかりません。")
        print("  同じフォルダに config.json を作成して、")
        print("  Supabase の URL と Publishable Key を設定してください。\n")
        sys.exit(1)

    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = json.load(f)

    # 未設定チェック
    url = cfg.get("supabase_url", "")
    key = cfg.get("supabase_anon_key", "")

    if not url or "xxxxxxxxxx" in url:
        print("\n[エラー] config.json の supabase_url が未設定です。")
        sys.exit(1)
    if not key or key.startswith("ここに"):
        print("\n[エラー] config.json の supabase_anon_key が未設定です。")
        sys.exit(1)

    # 末尾スラッシュを除去
    cfg["supabase_url"] = url.rstrip("/")

    return cfg


# =====================================================
#  Supabase Storage API（urllib のみ、追加ライブラリ不要）
# =====================================================
class SupabaseStorage:
    def __init__(self, url, key, bucket):
        self.base   = f"{url}/storage/v1"
        self.bucket = bucket
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
        }

    def list_files(self, limit=100):
        """バケット内のファイル一覧を取得（新しい順）"""
        url = f"{self.base}/object/list/{self.bucket}"
        body = json.dumps({
            "prefix": "",
            "limit": limit,
            "offset": 0,
            "sortBy": {"column": "created_at", "order": "desc"}
        }).encode()

        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={**self.headers, "Content-Type": "application/json"}
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                return json.loads(res.read())
        except urllib.error.HTTPError as e:
            print(f"\n  [API エラー] ファイル一覧取得失敗: {e.code}")
            try:
                print(f"  　詳細: {e.read().decode()[:200]}")
            except Exception:
                pass
            return []
        except Exception as e:
            print(f"\n  [通信エラー] {e}")
            return []

    def download_file(self, filename, save_path):
        """ファイルをダウンロードして保存"""
        url = f"{self.base}/object/{self.bucket}/{urllib.parse.quote(filename)}"
        req = urllib.request.Request(url, headers=self.headers)

        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                data = res.read()
                save_path.parent.mkdir(parents=True, exist_ok=True)
                with open(save_path, "wb") as f:
                    f.write(data)
            return True
        except urllib.error.HTTPError as e:
            print(f"  [ダウンロードエラー] {filename}: HTTP {e.code}")
            return False
        except Exception as e:
            print(f"  [ダウンロードエラー] {filename}: {e}")
            return False


# =====================================================
#  同期マネージャー
# =====================================================
class SyncManager:
    def __init__(self, config):
        self.storage = SupabaseStorage(
            config["supabase_url"],
            config["supabase_anon_key"],
            config.get("bucket_name", "scans")
        )
        self.download_dir  = Path(config.get("download_dir", "received_images"))
        self.output_dir    = Path(config.get("output_dir", "output"))
        self.poll_interval = config.get("poll_interval_sec", 5)
        self.analyze_script = Path(__file__).parent / "analyze_burn.py"

        self.download_dir.mkdir(exist_ok=True)
        self.output_dir.mkdir(exist_ok=True)

        # ダウンロード履歴（同じファイルを2回DLしない）
        self.downloaded = set()
        self.history_file = self.download_dir / ".sync_history.json"
        self._load_history()

    def _load_history(self):
        """過去のダウンロード履歴を読み込む"""
        if self.history_file.exists():
            try:
                with open(self.history_file, encoding="utf-8") as f:
                    self.downloaded = set(json.load(f))
            except Exception:
                self.downloaded = set()

    def _save_history(self):
        """ダウンロード履歴を保存"""
        with open(self.history_file, "w", encoding="utf-8") as f:
            json.dump(sorted(self.downloaded), f, indent=2)

    def sync_once(self):
        """
        1回の同期：
        新しい画像をダウンロード → analyze_burn.py を自動実行
        """
        files = self.storage.list_files()
        new_images = []

        for f in files:
            name = f.get("name", "")

            # 画像ファイルだけ対象（メタJSONはスキップ）
            if not name.lower().endswith((".jpg", ".jpeg", ".png")):
                continue

            # 既にダウンロード済みならスキップ
            if name in self.downloaded:
                continue

            save_path = self.download_dir / name
            print(f"\n  ↓ ダウンロード中: {name}")

            if self.storage.download_file(name, save_path):
                self.downloaded.add(name)
                self._save_history()
                new_images.append((name, save_path))
                print(f"  ✓ 保存完了: {save_path}")

        # 新しい画像を解析
        for name, path in new_images:
            self._run_analysis(name, path)

        return len(new_images)

    def _run_analysis(self, name, image_path):
        """analyze_burn.py を使って画像を解析"""
        if not self.analyze_script.exists():
            print(f"  [情報] analyze_burn.py が見つかりません。")
            print(f"  　画像は {image_path} に保存済みです。")
            print(f"  　analyze_burn.py を同じフォルダに置くと自動解析されます。")
            return

        print(f"  [解析] {name}...")

        # 環境変数でパスを渡す
        env = os.environ.copy()
        env["SCAN_IMAGE_PATH"]  = str(image_path.resolve())
        env["SCAN_OUTPUT_DIR"]  = str(self.output_dir.resolve())
        env["SCAN_SUBJECT_ID"]  = name.split("_")[0] if "_" in name else "unknown"

        try:
            result = subprocess.run(
                [sys.executable, str(self.analyze_script)],
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(self.analyze_script.parent)
            )

            if result.returncode == 0:
                print(f"  ✓ 解析完了: {name}")
                # 出力の最後の数行を表示
                if result.stdout:
                    lines = result.stdout.strip().split("\n")
                    for line in lines[-6:]:
                        print(f"    {line}")
            else:
                print(f"  ✗ 解析エラー: {name}")
                if result.stderr:
                    for line in result.stderr.strip().split("\n")[-4:]:
                        print(f"    {line}")

        except subprocess.TimeoutExpired:
            print(f"  ✗ タイムアウト: {name} （120秒超過）")
        except Exception as e:
            print(f"  ✗ 実行エラー: {e}")

    def watch(self):
        """ポーリングで新しい画像を監視し続ける"""
        print()
        print("=" * 56)
        print("  Supabase 画像同期 -- 監視開始")
        print("=" * 56)
        print(f"  ダウンロード先   : {self.download_dir.resolve()}")
        print(f"  解析結果         : {self.output_dir.resolve()}")
        print(f"  チェック間隔     : {self.poll_interval}秒")
        print(f"  ダウンロード済み : {len(self.downloaded)}件")

        if self.analyze_script.exists():
            print(f"  解析スクリプト   : {self.analyze_script}")
        else:
            print(f"  [!] analyze_burn.py が見つかりません（画像保存のみ）")

        print()
        print("  iPhoneから撮影すると自動でここに届きます。")
        print("  Ctrl+C で停止。")
        print("=" * 56)
        print()

        dot_count = 0

        try:
            while True:
                try:
                    n = self.sync_once()
                    if n > 0:
                        ts = datetime.now().strftime("%H:%M:%S")
                        print(f"\n  [{ts}] {n}件の新しい画像を処理しました\n")
                        dot_count = 0
                    else:
                        # 監視中を示すドット表示
                        print(".", end="", flush=True)
                        dot_count += 1
                        if dot_count >= 60:
                            ts = datetime.now().strftime("%H:%M:%S")
                            print(f" [{ts}] 監視中...")
                            dot_count = 0

                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    print(f"\n  [通信エラー] {e}")

                time.sleep(self.poll_interval)

        except KeyboardInterrupt:
            print(f"\n\n  監視を停止しました。")
            print(f"  ダウンロード済み画像: {self.download_dir.resolve()}")
            print()


# =====================================================
#  メイン
# =====================================================
if __name__ == "__main__":
    # urllib.parse が必要（Python標準ライブラリ）
    import urllib.parse

    config = load_config()
    manager = SyncManager(config)

    if "--once" in sys.argv:
        n = manager.sync_once()
        print(f"\n  {n}件の画像を同期しました。\n")
    else:
        manager.watch()
