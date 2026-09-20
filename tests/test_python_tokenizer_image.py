"""The production tokenizer must not need writable storage or live downloads."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_python_image_bakes_and_checks_offline_tokenizer_cache():
    dockerfile = (ROOT / "Dockerfile.python").read_text()
    assert dockerfile.count("ENV TIKTOKEN_CACHE_DIR=/app/.cache/tiktoken") == 2
    install = dockerfile.index("RUN uv sync --frozen --no-dev --all-packages")
    prefetch = dockerfile.index("timeout 120 .venv/bin/python -c 'import tiktoken;")
    copy = dockerfile.index("COPY --from=builder --chown=lumen:lumen /app /app")
    user = dockerfile.index("USER lumen")
    offline = dockerfile.index("RUN --network=none python -c 'import tiktoken;")
    assert install < prefetch < copy < user < offline
