from __future__ import annotations

import io

from PIL import Image as PILImage

from . import alpha_refine, qc
from .local_chroma import LocalChromaProvider
from .types import (
    BackgroundRemovalProvider,
    TransparentPipelineFailure,
    TransparentPipelineOutput,
    TransparentQcReport,
)

_DEFAULT_PROVIDERS: tuple[BackgroundRemovalProvider, ...] = (LocalChromaProvider(),)


async def process_transparent_request(
    source: PILImage.Image,
    *,
    prompt: str | None = None,
    providers: tuple[BackgroundRemovalProvider, ...] | None = None,
) -> TransparentPipelineOutput:
    chain = providers if providers is not None else _DEFAULT_PROVIDERS
    if not chain:
        raise TransparentPipelineFailure(
            "no_provider_configured", qc=None, provider=None
        )

    last_qc: TransparentQcReport | None = None
    last_provider: str | None = None

    for provider in chain:
        result = None
        try:
            result = await provider.remove_background(source, prompt=prompt)
        except Exception as exc:
            raise TransparentPipelineFailure(
                f"provider_error:{provider.name}:{exc}",
                qc=None,
                provider=provider.name,
            ) from exc

        if result is None:
            last_provider = provider.name
            continue

        # P2-8: 嵌套 try/finally 让 refined / result 都能在任意异常路径里关闭；
        # 同时容忍 qc.evaluate 返回 None（若实现里出现 bug 也不至于 AttributeError
        # 把 PIL 对象悬在内存里）。
        try:
            refined = alpha_refine.refine(result.rgba)
            try:
                report = qc.evaluate(refined)
                if report is None:
                    last_provider = provider.name
                    continue
                last_qc = report
                last_provider = provider.name
                if not report.passed:
                    continue

                rgba_buf = io.BytesIO()
                refined.save(rgba_buf, format="PNG", optimize=False)
                alpha_buf = io.BytesIO()
                refined.getchannel("A").save(alpha_buf, format="PNG", optimize=False)
                width, height = refined.size
                return TransparentPipelineOutput(
                    rgba_png=rgba_buf.getvalue(),
                    alpha_mask_png=alpha_buf.getvalue(),
                    width=width,
                    height=height,
                    provider=provider.name,
                    qc=report,
                )
            finally:
                refined.close()
        finally:
            result.close()

    raise TransparentPipelineFailure(
        "transparent_qc_failed" if last_qc is not None else "background_removal_failed",
        qc=last_qc,
        provider=last_provider,
    )
