"""Put the search model hf_api_lookup uses into the package, for development
and release builds. This is the only step that downloads it; the connector
itself only ever loads it from src/hullforge_mcp/data/embed_model/.

    uv run python scripts/vendor_embed_model.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hullforge_mcp.api_index import EMBED_MODEL, EMBED_MODEL_DIR, EMBED_MODEL_FILES, bundled_model  # noqa: E402


def main() -> None:
    if bundled_model():
        print(f"already present: {EMBED_MODEL_DIR}")
        return
    from fastembed import TextEmbedding

    with tempfile.TemporaryDirectory() as cache:
        model = TextEmbedding(EMBED_MODEL, cache_dir=cache)
        src = Path(model.model._model_dir)
        EMBED_MODEL_DIR.mkdir(parents=True, exist_ok=True)
        for name in EMBED_MODEL_FILES:
            shutil.copy2(src / name, EMBED_MODEL_DIR / name)
        del model
    (EMBED_MODEL_DIR / "SOURCE.txt").write_text(
        f"{EMBED_MODEL}, ONNX export by Qdrant (Qdrant/bge-small-en-v1.5-onnx-Q).\n"
        "Model by BAAI, MIT licence: https://huggingface.co/BAAI/bge-small-en-v1.5\n",
        encoding="utf-8")
    size = sum(f.stat().st_size for f in EMBED_MODEL_DIR.iterdir()) / 1e6
    print(f"vendored {EMBED_MODEL} into {EMBED_MODEL_DIR} ({size:.0f} MB)")


if __name__ == "__main__":
    main()
