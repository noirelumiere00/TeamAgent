"""Vertex AI (Gemini) の ADC 認証・疎通確認スクリプト。

組織ポリシーで API キーが禁止されているため、Vertex AI を ADC
(Application Default Credentials) で叩けるかを確認する。GeminiClient と
同じ google-genai SDK 経由で軽いテキスト生成を 1 回行い、認証 + プロジェクト +
Vertex AI API 有効化がすべて通っているかを切り分ける。

前提 (👤 セットアップ):
    1. gcloud CLI を入れて `gcloud auth application-default login`
       (または サービスアカウント JSON を GOOGLE_APPLICATION_CREDENTIALS に)
    2. 対象 GCP プロジェクトで Vertex AI API (aiplatform.googleapis.com) を有効化
    3. 実行アカウントに「Vertex AI User」ロール

Usage:
    GEMINI_USE_VERTEX=true \\
    GEMINI_VERTEX_PROJECT=<project-id> \\
    GEMINI_VERTEX_LOCATION=us-central1 \\
    python scripts/verify_vertex.py

成功すると:
    ✅ Vertex AI 疎通 OK (project=..., location=..., model=...)
       応答: <Gemini の短い返答>
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    project = os.environ.get("GEMINI_VERTEX_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GEMINI_VERTEX_LOCATION", "us-central1")
    model_id = os.environ.get("GEMINI_MODEL_ID", "gemini-2.5-flash")

    if not project:
        print("❌ GEMINI_VERTEX_PROJECT (または GOOGLE_CLOUD_PROJECT) が未設定です。")
        print("   例: GEMINI_VERTEX_PROJECT=ntv-ai-xxxxx python scripts/verify_vertex.py")
        return 2

    print(f"… Vertex AI に接続中 (project={project}, location={location}, model={model_id})")
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(vertexai=True, project=project, location=location)
        resp = client.models.generate_content(
            model=model_id,
            contents=[
                types.Content(
                    role="user",
                    parts=[types.Part(text="「疎通確認OK」とだけ返答してください。")],
                )
            ],
        )
        text = (resp.text or "").strip()
    except Exception as e:  # 切り分け用に型名を表示
        print(f"❌ Vertex AI 疎通に失敗: {type(e).__name__}: {e}")
        print("   切り分け:")
        print("   - 認証(ADC)未設定 → `gcloud auth application-default login`")
        print("   - Vertex AI API 未有効 → `gcloud services enable aiplatform.googleapis.com`")
        print("   - 権限不足 → 実行アカウントに『Vertex AI User』ロールを付与")
        print("   - location 非対応 → us-central1 など別リージョンを試す")
        return 1

    print(f"✅ Vertex AI 疎通 OK (project={project}, location={location}, model={model_id})")
    print(f"   応答: {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
