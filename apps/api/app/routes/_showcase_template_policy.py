"""Pure showcase template policy for apparel workflow prompts."""

from __future__ import annotations

from typing import Any

TEMPLATE_LABELS = {
    "white_ecommerce": "白底主图",
    "premium_studio": "高级棚拍",
    "urban_commute": "质感街拍",
    "lifestyle": "精品空间",
    "daily_snapshot": "日常随拍",
    "natural_phone_snapshot": "自然手机摄影",
    "social_seed": "自然种草",
}

SCENE_ENVIRONMENT_TEMPLATES = frozenset(
    {
        "daily_snapshot",
        "natural_phone_snapshot",
        "social_seed",
    }
)


def _scene_environment_outdoor_phrase(template: str, category: str) -> str:
    """3 个生活化模板的户外变体；indoor 走原 prompt 保持原有场景锚点。"""
    if template not in SCENE_ENVIRONMENT_TEMPLATES:
        return ""
    scenes = {
        "daily_snapshot": (
            f"户外随拍场景（街角、阳台、庭院、咖啡店外或公园），"
            f"自然日光为主，背景与{category}搭配但不杂乱，超真实、超自然、不像棚拍"
        ),
        "natural_phone_snapshot": (
            f"真实手机竖屏随手拍，平视或自然手持视角；"
            f"户外随手拍场景（街道、公园、海边、景点或建筑前），"
            f"氛围跟{category}搭配；自然日光带方向（侧光、逆光或斜上光）；"
            f"少量生活细节真实但不要遮挡{category}主体，不要棚拍或亚马逊主图感"
        ),
        "social_seed": (
            f"户外种草场景（街拍、橱窗、店外、景点或自然环境），"
            f"自然日光，与{category}匹配的松弛、真实、有生活感氛围"
        ),
    }
    return scenes.get(template, "")


def _template_requirement(
    template: str,
    product_analysis: dict[str, Any],
    scene_environment: str = "indoor",
) -> str:
    category = str(product_analysis.get("category") or "服饰").strip() or "服饰"
    recommended_background = str(
        product_analysis.get("background_recommendation") or ""
    ).strip()
    matched_background = (
        recommended_background
        if recommended_background and recommended_background.lower() != "unknown"
        else f"根据{category}选择好看的服饰商业摄影氛围"
    )
    phone_scene = (
        recommended_background
        if recommended_background and recommended_background.lower() != "unknown"
        else f"与{category}风格搭配的真实生活空间"
    )
    outdoor_phrase = (
        _scene_environment_outdoor_phrase(template, category)
        if scene_environment == "outdoor"
        else ""
    )
    requirements = {
        "white_ecommerce": "白底或近白底，柔和棚拍光",
        "premium_studio": f"{matched_background}，高级棚拍质感，柔和光影",
        "urban_commute": f"与{category}匹配的质感街拍氛围，真实自然但不杂乱",
        "lifestyle": f"与{category}匹配的精品空间氛围，克制、高级、有层次",
        "daily_snapshot": (
            outdoor_phrase
            or f"与{category}匹配的日常随拍质感，手机拍摄感，超真实、超自然、不像棚拍"
        ),
        "natural_phone_snapshot": (
            outdoor_phrase
            or (
                f"真实手机竖屏随手拍，平视或自然手持视角；"
                f"{phone_scene}，氛围跟{category}搭配；自然光或室内暖光；"
                f"少量生活细节真实但不要遮挡{category}主体，不要棚拍或亚马逊主图感"
            )
        ),
        "social_seed": (
            outdoor_phrase or f"与{category}匹配的自然种草氛围，松弛、真实、有生活感"
        ),
    }
    return requirements.get(template, TEMPLATE_LABELS.get(template, template))


# 每个模板的渲染指令都包含四段：
# 1) 调性短语（杂志大片 / 真实街拍 / 自然日常）
# 2) 皮肤质感正面约束（毛孔细纹、自然光泽、皮下血色）
# 3) 真人瑕疵（按调性递增：商业类轻、真实类重）— 仅靠"真实/不磨皮"模型仍渲染完美无瑕
# 4) 简短禁令（塑料感、过度磨皮、AI 美颜脸）
_RENDER_DIRECTIONS: dict[str, str] = {
    "white_ecommerce": (
        "干净高级商业摄影，柔和棚拍光，服装细节清晰；"
        "皮肤保留真实毛孔细纹和自然光泽，皮下血色自然，鼻翼T区有真实油光；"
        "毛孔深浅不均，面部非完美对称；"
        "不要塑料感、过度磨皮、AI美颜脸；无文字水印"
    ),
    "premium_studio": (
        "杂志大片质感，高级棚拍光带方向，颧骨鼻梁有清晰高光、下颌阴影分明；"
        "皮肤真实有毛孔和细纹，自然光泽和皮下血色，眼神有真实质感；"
        "眉毛和嘴唇细微不对称，眼角有真实细纹；"
        "不要塑料感、过度磨皮、AI网红脸"
    ),
    "urban_commute": (
        "真实街头摄影质感，户外日光带明确方向（侧光、逆光或斜上光），"
        "建筑阴影投射在脸或地面，颧骨鼻梁有真实高光，半边脸略阴影；"
        "皮肤保留毛孔细纹和真实阴影，鼻翼T区自然油光，碎发和飞絮真实；"
        "有轻微痘印或泛红，皮肤色不完全均匀；"
        "不要塑料感、过度磨皮、AI美颜脸、全脸均匀照明"
    ),
    "lifestyle": (
        "真实空间摄影质感，窗光或室内射灯带明确方向，"
        "半边脸略阴影、颧骨鼻梁有真实高光，背景明暗渐变；"
        "皮肤有真实毛孔细纹和自然光泽，皮下血色自然，碎发不刻意；"
        "皮肤色不完全均匀，左右脸细微差异；"
        "不要塑料感、过度磨皮、AI网红脸、全脸均匀照明"
    ),
    "daily_snapshot": (
        "真实日常随拍质感，家中窗光或灯光带方向（侧窗光或顶光），"
        "颧骨鼻梁有真实高光，鼻翼下方有真实阴影，背景有光线渐变；"
        "皮肤保留真实毛孔和细纹，自然光泽和皮下血色，碎发自然；"
        "有轻微痘印或鼻翼油光，毛孔深浅不均；"
        "不要塑料感、过度磨皮、AI美颜脸、全脸均匀照明"
    ),
    "natural_phone_snapshot": (
        "真实手机照片质感，自然窗光或柔和室内暖光从侧面打来，"
        "半边脸略阴影、颧骨鼻梁有真实高光、皮肤上有细微光斑；"
        "服装细节可见；真实阴影、真实皮肤毛孔和细纹、自然碎发、衣服真实褶皱；"
        "有轻微痘印或鼻翼黑头，面部非完美对称；"
        "不要棚拍感、过度磨皮、AI网红脸、社交媒体截图界面、全脸均匀照明"
    ),
    "social_seed": (
        "真实生活种草质感，窗光或室内灯光带方向，"
        "颧骨鼻梁有真实高光、半边脸略阴影、玻璃反光真实；"
        "服装搭配清晰；皮肤有真实毛孔细纹和自然光泽，皮下血色自然，碎发真实；"
        "有轻微痘印或皮肤色不均，眼角真实细纹；"
        "不要塑料感、过度磨皮、AI美颜脸、全脸均匀照明，不要画面中出现镜子或镜面"
    ),
}


_RENDER_DIRECTIONS_OUTDOOR: dict[str, str] = {
    "daily_snapshot": (
        "真实日常随拍质感，户外自然日光带明确方向（侧光、逆光或斜上光），"
        "颧骨鼻梁有真实高光，半边脸略阴影，背景有真实空间深度；"
        "皮肤保留真实毛孔和细纹，自然光泽和皮下血色，碎发自然；"
        "有轻微痘印或鼻翼油光，毛孔深浅不均；"
        "不要塑料感、过度磨皮、AI美颜脸、全脸均匀照明"
    ),
    "natural_phone_snapshot": (
        "真实手机照片质感，户外自然日光从侧面或斜上方打来，"
        "半边脸略阴影、颧骨鼻梁有真实高光、皮肤上有细微光斑；"
        "服装细节可见；真实阴影、真实皮肤毛孔和细纹、自然碎发、衣服真实褶皱；"
        "有轻微痘印或鼻翼黑头，面部非完美对称；"
        "不要棚拍感、过度磨皮、AI网红脸、社交媒体截图界面、全脸均匀照明"
    ),
    "social_seed": (
        "真实生活种草质感，户外自然日光带方向（侧光、逆光或斜上光），"
        "颧骨鼻梁有真实高光、半边脸略阴影、建筑或绿植真实阴影投射；"
        "服装搭配清晰；皮肤有真实毛孔细纹和自然光泽，皮下血色自然，碎发真实；"
        "有轻微痘印或皮肤色不均，眼角真实细纹；"
        "不要塑料感、过度磨皮、AI美颜脸、全脸均匀照明，不要画面中出现镜子或镜面"
    ),
}


def _showcase_render_direction(template: str, scene_environment: str = "indoor") -> str:
    if (
        scene_environment == "outdoor"
        and template in SCENE_ENVIRONMENT_TEMPLATES
        and template in _RENDER_DIRECTIONS_OUTDOOR
    ):
        return _RENDER_DIRECTIONS_OUTDOOR[template]
    return _RENDER_DIRECTIONS.get(
        template,
        "真实摄影质感；皮肤保留真实毛孔细纹和自然光泽；不要塑料感、过度磨皮、AI美颜脸",
    )


_POSE_DIRECTIONS: dict[str, str] = {
    "white_ecommerce": "目录摄影舒展自然站姿，肩颈松弛、重心稳定，不僵硬",
    "premium_studio": "高级棚拍但动作克制，戏剧化只在光线和眼神，不靠夸张肢体",
    "urban_commute": "真实街头抓拍感，小幅动作像无意被定格，不刻意摆拍",
    "lifestyle": "从容松弛的空间感，小幅动作，有呼吸感",
    "daily_snapshot": "朋友视角随手拍，动作自然不刻意，身体重心可信",
    "natural_phone_snapshot": "姿态自然松弛，平视手持视角，小幅自然动作，正常手机透视",
    "social_seed": "自然穿搭分享感，小幅互动展示，不摆硬 pose",
}


def _showcase_pose_direction(template: str) -> str:
    return _POSE_DIRECTIONS.get(template, "姿态自然舒展")


_LIFESTYLE_TEMPLATES = frozenset(
    {
        "urban_commute",
        "lifestyle",
        "daily_snapshot",
        "natural_phone_snapshot",
        "social_seed",
    }
)


def _showcase_composition_direction(template: str) -> str:
    """生活化模板加构图变化，白底/棚拍保持中心构图不动。"""
    if template not in _LIFESTYLE_TEMPLATES:
        return ""
    return (
        "三分法构图，模特竖向落在画面左或右 1/3 线，对侧留呼吸感；"
        "可有轻微前景或景深层次，但不抢戏"
    )


_SQUARE_OR_LANDSCAPE_RATIOS = frozenset({"1:1", "4:3", "3:2", "16:9", "21:9"})


def _showcase_framing_direction(
    shot_class: str,
    framing: str | None,
    aspect_ratio: str = "4:5",
) -> str:
    """按机位类 + framing tag + 画面比例控制构图留白，避免身体被画面比例挤扁或拉长。

    - detail_half_body 是上半身近景，单独走"半身入镜"分支
    - tone_first 走"环境为主体、画面留白"
    - product_first（除 detail）走"全身入镜 + 上下留边"
    - 画面方/横时下调人物占比，横向多留余白，避免 AI 为了填高度而压缩身体
    """
    if shot_class == "detail_half_body":
        return (
            "上半身或胸口以上入镜，头顶留出适度边距，肩部肘部不顶画面边缘，背景留白干净"
        )
    is_square_or_landscape = aspect_ratio in _SQUARE_OR_LANDSCAPE_RATIOS
    if framing == "tone_first":
        if is_square_or_landscape:
            return (
                "人物占画面 45-60% 高度，左右留出充足余白，"
                "环境只作为氛围辅助，不要让背景压过服装主体，"
                "头脚完整且透视自然，不要为了填高度压扁身体"
            )
        return (
            "人物占画面 55-70% 高度，环境只作为氛围辅助，"
            "不要让背景压过服装主体，头脚完整且透视自然"
        )
    if is_square_or_landscape:
        return (
            "全身完整入镜，头顶上方留出 5-10% 边距，脚下完整不切断，"
            "人物占画面 60-75% 高度，左右留宽边，"
            "不要为了塞满画面而压缩或拉长身体比例"
        )
    return (
        "全身完整入镜，头顶上方留出 5-10% 边距，脚下完整不切断，"
        "人物占画面 70-85% 高度，避免顶满"
    )
