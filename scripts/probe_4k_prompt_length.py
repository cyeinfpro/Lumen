from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import tiktoken
from PIL import Image as PILImage

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.worker.app import upstream


BASE_URL = "https://api.example.com/v1"
SIZE = "3840x2160"


@dataclass
class ProbeResult:
    route: str
    target_tokens: int
    prompt_chars: int
    prompt_tokens: int
    status: str
    elapsed_s: float
    image_path: str | None = None
    image_width: int | None = None
    image_height: int | None = None
    image_bytes: int | None = None
    error_type: str | None = None
    error_code: str | None = None
    status_code: int | None = None
    message: str | None = None


def load_api_key() -> str:
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY environment variable is required")
    return key


def encoder() -> Any:
    return tiktoken.get_encoding("o200k_base")


def token_count(text: str) -> int:
    return len(encoder().encode(text))


def build_prompt(target_tokens: int) -> str:
    base = (
        "生成一张4K横版电影感摄影作品：雨后的未来城市屋顶花园，一位穿米色风衣的年轻设计师站在玻璃栏杆旁，"
        "远处是霓虹、高楼、低云和刚散去的雨雾。画面要真实、干净、细节丰富，有电影镜头的空间层次。"
        "不要出现文字、logo、水印或海报排版。"
    )
    fragments = [
        "构图使用三分法，人物位于画面左侧三分线，右侧保留城市天际线和云层反光。",
        "光线来自黄昏后的冷暖混合环境光，地面有轻微积水反射，玻璃边缘有细小高光。",
        "人物姿态自然安静，侧脸看向远处，衣料有轻微雨滴和风吹褶皱。",
        "屋顶花园包含低矮绿植、金属长椅、透明雨棚和几盏嵌入式地灯，但不要让元素拥挤。",
        "色彩以青蓝、暖金、深灰和少量植物绿为主，整体克制，高级，不要过饱和。",
        "镜头感接近35mm全画幅摄影，浅景深只作用在远景，主体和近景保持清晰。",
        "城市背景需要有真实空气透视，远楼细节逐渐变软，雨后雾气让层次自然过渡。",
        "画面不要科幻夸张，不要飞船，不要赛博朋克符号堆砌，只保留可信的近未来设计。",
        "材质要区分玻璃、湿混凝土、金属、织物和植物叶片，边缘干净，纹理可见。",
        "整体情绪是安静、专注、带一点希望感，像高端电影剧照而不是广告海报。",
        "保留足够负空间，避免把所有细节平均铺满，视觉重心应该一眼能看清。",
        "人物面部不要过度磨皮，皮肤保留自然纹理，眼神清晰，五官比例真实。",
        "雨滴只作为局部细节出现，不要形成暴雨效果，也不要遮挡主体。",
        "后期风格自然，低对比阴影保留层次，高光不过曝，暗部不要死黑。",
        "最终图像应适合大屏查看，近看有丰富细节，远看有明确轮廓和稳定构图。",
    ]
    prompt = base
    index = 0
    while token_count(prompt) < target_tokens:
        prompt += f"\n- {fragments[index % len(fragments)]}"
        index += 1
    return prompt


def save_image(raw_b64: str, out_dir: Path, stem: str) -> tuple[str, int, int, int]:
    raw = base64.b64decode(raw_b64)
    tmp_path = out_dir / f"{stem}.bin"
    tmp_path.write_bytes(raw)
    with PILImage.open(tmp_path) as image:
        width, height = image.size
        fmt = (image.format or "jpeg").lower()
    image_path = out_dir / f"{stem}.{fmt}"
    tmp_path.replace(image_path)
    return str(image_path), width, height, len(raw)


async def run_one(
    *,
    route: str,
    prompt: str,
    target_tokens: int,
    api_key: str,
    out_dir: Path,
) -> ProbeResult:
    prompt_tokens = token_count(prompt)
    started = time.monotonic()
    try:
        if route == "direct":
            raw_b64, _ = await upstream._direct_generate_image_once(
                prompt=prompt,
                size=SIZE,
                n=1,
                quality="high",
                output_format="jpeg",
                output_compression=90,
                background="auto",
                moderation="low",
                base_url_override=BASE_URL,
                api_key_override=api_key,
            )
        else:
            raw_b64, _ = await upstream._responses_image_stream(
                action="generate",
                prompt=prompt,
                size=SIZE,
                images=None,
                quality="high",
                output_format="jpeg",
                output_compression=90,
                background="auto",
                moderation="low",
                model=None,
                base_url_override=BASE_URL,
                api_key_override=api_key,
            )
        elapsed_s = round(time.monotonic() - started, 2)
        image_path, width, height, size_bytes = save_image(
            raw_b64,
            out_dir,
            f"{route}-4k-{target_tokens}tok",
        )
        return ProbeResult(
            route=route,
            target_tokens=target_tokens,
            prompt_chars=len(prompt),
            prompt_tokens=prompt_tokens,
            status="success",
            elapsed_s=elapsed_s,
            image_path=image_path,
            image_width=width,
            image_height=height,
            image_bytes=size_bytes,
        )
    except BaseException as exc:  # noqa: BLE001
        elapsed_s = round(time.monotonic() - started, 2)
        return ProbeResult(
            route=route,
            target_tokens=target_tokens,
            prompt_chars=len(prompt),
            prompt_tokens=prompt_tokens,
            status="error",
            elapsed_s=elapsed_s,
            error_type=exc.__class__.__name__,
            error_code=getattr(exc, "error_code", None),
            status_code=getattr(exc, "status_code", None),
            message=str(exc)[:500],
        )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--route", choices=["responses", "direct"], default="responses")
    parser.add_argument(
        "--tokens",
        nargs="+",
        type=int,
        default=[120, 260, 520, 800, 1100, 1500],
    )
    parser.add_argument(
        "--out-dir",
        default="output/4k-prompt-length-probe",
    )
    parser.add_argument("--stop-after-errors", type=int, default=2)
    args = parser.parse_args()

    api_key = load_api_key()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[ProbeResult] = []
    consecutive_errors = 0
    try:
        for target in args.tokens:
            prompt = build_prompt(target)
            actual_tokens = token_count(prompt)
            print(
                f"START route={args.route} target={target} "
                f"chars={len(prompt)} tokens={actual_tokens}",
                flush=True,
            )
            result = await run_one(
                route=args.route,
                prompt=prompt,
                target_tokens=target,
                api_key=api_key,
                out_dir=out_dir,
            )
            results.append(result)
            print(
                f"DONE route={result.route} target={result.target_tokens} "
                f"status={result.status} elapsed={result.elapsed_s}s "
                f"size={result.image_width}x{result.image_height} "
                f"error={result.error_code or result.error_type or ''}",
                flush=True,
            )
            consecutive_errors = (
                consecutive_errors + 1 if result.status != "success" else 0
            )
            summary_path = out_dir / f"{args.route}-summary.json"
            summary_path.write_text(
                json.dumps([asdict(item) for item in results], ensure_ascii=False, indent=2)
                + "\n"
            )
            if args.stop_after_errors and consecutive_errors >= args.stop_after_errors:
                break
    finally:
        await upstream.close_client()


if __name__ == "__main__":
    asyncio.run(main())
