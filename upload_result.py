"""
upload_result.py

採点結果を Supabase Storage に書き戻す。
Webアプリ（index.html）はこれを取得してiPhoneに結果を表示する。

■ なぜ Storage を使うか（方式B）
  Supabase の Database テーブルを使う方式（方式A）もあるが、
  そちらは事前にSQLでテーブルを作る作業が必要になる。
  Storage なら既存の仕組み（画像アップロードで使用中）の延長で完結し、
  追加のセットアップが要らない。被験者数が数十人規模なら十分。
  将来データが増えて重くなったら方式Aへ移行すればよい。

■ 保存するもの（すべて results/ 以下）
    results/S001_20260714_143022.json      … スコア本体
    results/S001_20260714_143022_radar.png … レーダーチャート
    results/S001_20260714_143022_card.png  … スコアカード
    results/_index.json                    … 全結果の一覧（管理者モード用）

  画像ファイル名（S001_20260714_143022.jpg）と同じ命名にすることで、
  Webアプリは「自分が撮った画像に対応する結果」を確実に取りに行ける。
"""

import json
import urllib.request
import urllib.error
from pathlib import Path


class ResultUploader:
    def __init__(self, url, key, bucket="scans"):
        self.base = f"{url.rstrip('/')}/storage/v1"
        self.key = key
        self.bucket = bucket

    def _headers(self, content_type):
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": content_type,
            "x-upsert": "true",   # 同名があれば上書き
        }

    def _delete(self, path):
        """Storage から既存ファイルを消す。無ければ何もしない。"""
        url = f"{self.base}/object/{self.bucket}/{path}"
        req = urllib.request.Request(
            url, method="DELETE",
            headers={
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return 200 <= r.status < 300
        except Exception:
            return False

    def _put(self, path, data, content_type, retry_with_put=True):
        """
        Storage にファイルを置く。

        Supabase の RLS は INSERT と UPDATE が別ポリシーになっている。
        UPDATE が許可されていない環境だと、同名ファイルへの上書きが
        403 (new row violates row-level security policy) で弾かれる。
        そのため POST→PUT で失敗した場合は、いったん DELETE してから
        INSERT し直すことで、UPDATE 権限が無くても更新できるようにする。
        （割面採点後の再アップロードでこの経路を通る）
        """
        for method in (["POST", "PUT"] if retry_with_put else ["POST"]):
            url = f"{self.base}/object/{self.bucket}/{path}"
            req = urllib.request.Request(
                url, data=data, method=method,
                headers=self._headers(content_type),
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    if 200 <= r.status < 300:
                        return True
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8")[:200]
                except Exception:
                    pass
                if method == "POST" and retry_with_put:
                    # POST が失敗（既存ファイルへの上書き競合など）→ PUT で再試行
                    continue
                # PUT でも失敗 → 削除してから INSERT し直す
                if self._delete(path):
                    req2 = urllib.request.Request(
                        url, data=data, method="POST",
                        headers=self._headers(content_type),
                    )
                    try:
                        with urllib.request.urlopen(req2, timeout=30) as r2:
                            if 200 <= r2.status < 300:
                                return True
                    except Exception:
                        pass
                print(f"  [アップロード失敗] {path}: HTTP {e.code} {e.reason} {body}")
                return False
            except Exception as e:
                print(f"  [アップロード失敗] {path}: {e}")
                return False
        return False

    def _get_json(self, path):
        """
        Storage から JSON を読む。無ければ None。

        Supabase Storage の GET は、バケットの公開設定によって
        エンドポイントが異なる:
          - 公開バケット: /storage/v1/object/public/<bucket>/<path>
          - 非公開バケット: /storage/v1/object/authenticated/<bucket>/<path>
            (または /storage/v1/object/<bucket>/<path> に Authorization を付ける)

        両方試して取得できた方を使う。
        """
        auth_headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
        }
        urls = [
            # 非公開バケット（認証付き）
            f"{self.base}/object/{self.bucket}/{path}",
            # 公開バケット
            f"{self.base}/object/public/{self.bucket}/{path}",
        ]
        for url in urls:
            req = urllib.request.Request(
                url, method="GET", headers=auth_headers,
            )
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = r.read().decode("utf-8")
                    result = json.loads(data)
                    return result
            except urllib.error.HTTPError as e:
                # 404 = まだファイルがない（正常）、それ以外は次のURLを試す
                if e.code == 404:
                    return None
                continue
            except Exception:
                continue
        return None

    def upload_score(self, record_key, score, subject_id,
                     radar_png=None, card_png=None, severity_png=None,
                     tumor_viz_png=None):
        """
        1件分の採点結果をアップロードする。

        _index.json による一覧管理はやめた。個別JSONのINSERTのみにすることで、
        Supabase の RLS（行レベルセキュリティ）の UPDATE 制限を回避する。
        管理者画面は Storage の List API で results/ 以下を列挙して一覧を作る。
        """
        ok = True

        # ── スコア本体（JSON）──
        payload = {
            "record_key": record_key,
            "subject_id": subject_id,
            **{k: _clean(v) for k, v in score.items()},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if not self._put(f"results/{record_key}.json", body, "application/json"):
            ok = False

        # ── 画像 ──
        # severity は「焦げ段階マスク」。研究計画書の図2に相当する、
        # 重度／中度／軽度が色分けされた画像で、iPhoneの結果画面に表示する。
        # tumorviz は割面の解析画像（緑=組織輪郭 / 青=理想円弧 /
        # 水色=腫瘍輪郭 / 赤=左右マージン）で、研究計画書の図1に相当する。
        images = [
            (radar_png,     "radar"),
            (card_png,      "card"),
            (severity_png,  "severity"),
            (tumor_viz_png, "tumorviz"),
        ]
        for png, suffix in images:
            if png and Path(png).exists():
                data = Path(png).read_bytes()
                if not self._put(f"results/{record_key}_{suffix}.png",
                                 data, "image/png"):
                    ok = False

        if ok:
            print(f"  ☁ 結果をアップロードしました（iPhoneで確認できます）")
        else:
            print(f"  ⚠ 一部のアップロードに失敗しました（採点自体は完了）")
        return ok

def _clean(v):
    """numpy型やNaNを、JSONで扱える素のPython型に変換する"""
    import math
    if v is None:
        return None
    if isinstance(v, (bool, str)):
        return v
    if isinstance(v, (int, float)):
        # NaN / Inf は JSON では表現できないので None にする
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return None
        return v
    # numpy のスカラー型など
    if hasattr(v, "item"):
        try:
            return _clean(v.item())
        except Exception:
            pass
    return str(v)


def load_config(config_path):
    """scan_cloud/config.json から接続情報を読む"""
    p = Path(config_path)
    if not p.exists():
        return None
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    url = cfg.get("supabase_url", "").strip()
    key = cfg.get("supabase_anon_key", "").strip()
    if not url or not key or "ここに" in key:
        return None
    return {
        "url": url.rstrip("/"),
        "key": key,
        "bucket": cfg.get("bucket_name", "scans"),
    }