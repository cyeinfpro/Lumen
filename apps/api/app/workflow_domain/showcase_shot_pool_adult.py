"""Adult baseline shot pool for showcase generation.

Derived age bands (teen / adult / middle_aged / senior) share this pool and get
additional soft constraints from the caller.

Design intent:
- Natural photography should still look photographed, not staged by impossible
  body mechanics.
- Default actions are grounded and low-amplitude: standing, small step, slight
  turn, light lean, adjusting collar/cuff/hem, looking aside.
- Avoid high-risk default poses: jumping, dramatic hair toss, kneeling, lying,
  deep squat, big back bends, and exaggerated runway movement.
- The product remains readable even in lifestyle/social templates.
"""

from __future__ import annotations

from .showcase_shot_pool import ShotPool


ADULT_POOL: ShotPool = {
    "white_ecommerce": {
        "front_full_body": [
            {
                "label": "正面全身白底主图，自然站立，双脚稳定着地，目视镜头",
                "framing": "product_first",
            },
            {
                "label": "正面全身白底，一手自然垂落，一手轻扶衣侧",
                "framing": "product_first",
            },
            {
                "label": "正面全身白底，小步站定，前后脚都接地",
                "framing": "product_first",
            },
            {
                "label": "正面全身白底，肩颈放松，手臂自然下垂",
                "framing": "product_first",
            },
        ],
        "natural_pose": [
            {"label": "白底前侧身十五度，单手轻整理袖口", "framing": "product_first"},
            {
                "label": "白底前微侧身，一手轻扶领口，目光略偏侧",
                "framing": "product_first",
            },
            {"label": "白底前自然站立，单手轻碰下摆边缘", "framing": "product_first"},
            {
                "label": "白底前小步停下，重心稳定，衣摆自然垂落",
                "framing": "product_first",
            },
        ],
        "detail_half_body": [
            {"label": "上身近景，手指轻碰领口检查面料", "framing": "product_first"},
            {"label": "上身近景，手指轻拈袖口边缘", "framing": "product_first"},
            {
                "label": "上身近景，单手轻按前襟，衣服细节清楚",
                "framing": "product_first",
            },
            {
                "label": "上身近景，双手自然交叠在身前，不遮挡主体",
                "framing": "product_first",
            },
        ],
        "side_or_back": [
            {
                "label": "完整侧身站立，手臂自然下垂，展示侧面廓形",
                "framing": "product_first",
            },
            {
                "label": "完整背面站立，头自然朝前，展示后背版型",
                "framing": "product_first",
            },
            {
                "label": "白底前侧身九十度，单手轻搭腰侧看廓形",
                "framing": "product_first",
            },
            {"label": "白底前45度背面站定，单手轻扶后摆", "framing": "product_first"},
        ],
    },
    "premium_studio": {
        "front_full_body": [
            {
                "label": "棚拍正面全身自然站立，眼神冷静，双脚稳定着地",
                "framing": "product_first",
            },
            {"label": "棚拍正面全身小步站定，肩线放松", "framing": "product_first"},
            {
                "label": "棚拍灰底远一点的全身站姿，人物完整清晰",
                "framing": "tone_first",
            },
            {
                "label": "棚拍正面全身，单手轻扶衣侧，光影有层次",
                "framing": "product_first",
            },
        ],
        "natural_pose": [
            {
                "label": "棚拍侧身十五度，单手整理袖口，动作轻",
                "framing": "product_first",
            },
            {"label": "棚拍浅灰墙前自然站立，视线略偏侧", "framing": "tone_first"},
            {
                "label": "棚拍前微侧身，手指轻碰领口，颈肩放松",
                "framing": "product_first",
            },
            {
                "label": "棚拍小步停下，前后脚都接地，衣服自然垂落",
                "framing": "product_first",
            },
        ],
        "detail_half_body": [
            {
                "label": "上身近景棚拍光，手指轻碰领口检查质感",
                "framing": "product_first",
            },
            {"label": "上身近景，手指轻拈袖口，纹理清晰", "framing": "product_first"},
            {
                "label": "上身近景，双手轻拢前襟，不遮挡服装结构",
                "framing": "product_first",
            },
            {
                "label": "上身近景，单手轻扶发侧，另一手自然垂落",
                "framing": "product_first",
            },
        ],
        "side_or_back": [
            {
                "label": "棚拍完整侧身站定，肩线松弛，展示衣长",
                "framing": "product_first",
            },
            {"label": "棚拍完整背面站立，手臂自然下垂", "framing": "product_first"},
            {"label": "棚拍灰底45度背面站定，单手轻扶后摆", "framing": "tone_first"},
            {"label": "棚拍侧身九十度站立，光线勾出轮廓", "framing": "product_first"},
        ],
    },
    "urban_commute": {
        "front_full_body": [
            {
                "label": "街边正面全身小步停下，前后脚都接地，目光看镜头",
                "framing": "product_first",
            },
            {
                "label": "咖啡店门口正面全身站定，手臂自然下垂",
                "framing": "product_first",
            },
            {
                "label": "街角远一点的全身站姿，城市背景干净不抢服装",
                "framing": "tone_first",
            },
            {
                "label": "人行道旁正面全身自然站立，一手轻扶衣侧",
                "framing": "product_first",
            },
        ],
        "natural_pose": [
            {
                "label": "便利店门口前侧身站定，单手轻整理袖口",
                "framing": "product_first",
            },
            {"label": "公交站旁自然站立，视线看向路口一侧", "framing": "product_first"},
            {"label": "商业街小步停下，脚步很小，重心稳定", "framing": "tone_first"},
            {
                "label": "公园门口侧身十五度，风吹动衣摆但身体稳定",
                "framing": "tone_first",
            },
        ],
        "detail_half_body": [
            {"label": "上身近景，街角正在轻整理外套领子", "framing": "product_first"},
            {"label": "上身近景，路灯下单手轻碰前襟", "framing": "product_first"},
            {"label": "上身近景，咖啡店外手指轻拈袖口", "framing": "product_first"},
            {"label": "上身近景，街头逆光下轻拨耳后碎发", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "街边完整侧身站立，背景街景轻微虚化", "framing": "product_first"},
            {"label": "地铁口旁完整背面站立，头自然朝前", "framing": "tone_first"},
            {
                "label": "侧身看街角橱窗，身体站稳，服装完整可见",
                "framing": "tone_first",
            },
            {
                "label": "路口旁45度侧身站定，光线自然打在身上",
                "framing": "product_first",
            },
        ],
    },
    "lifestyle": {
        "front_full_body": [
            {
                "label": "画廊白墙前正面全身自然站立，双脚稳定着地",
                "framing": "product_first",
            },
            {
                "label": "选物店木柜前正面全身站定，手轻碰衣侧",
                "framing": "product_first",
            },
            {
                "label": "美术馆庭院远一点的全身站姿，人物完整清晰",
                "framing": "tone_first",
            },
            {
                "label": "酒店大堂中庭正面全身小步站定，空间干净",
                "framing": "tone_first",
            },
        ],
        "natural_pose": [
            {"label": "选物店木柜旁前侧身，轻整理袖口", "framing": "product_first"},
            {
                "label": "酒店软椅旁自然站立，单手轻扶椅背不倚压身体",
                "framing": "product_first",
            },
            {"label": "落地窗边自然站定，目光望向窗外", "framing": "tone_first"},
            {"label": "阳光阅读角旁站立，低头看前襟细节", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {
                "label": "上身近景，画廊白墙前侧面光，领口清楚",
                "framing": "product_first",
            },
            {"label": "上身近景，选物店木柜前低头看袖口", "framing": "product_first"},
            {"label": "上身近景，落地窗边自然光下轻碰前襟", "framing": "product_first"},
            {
                "label": "上身近景，美术馆庭院光线柔和，轻拨碎发",
                "framing": "product_first",
            },
        ],
        "side_or_back": [
            {"label": "画廊白墙前完整侧身站定，肩线松弛", "framing": "product_first"},
            {"label": "落地窗前完整背面站立，逆光勾出轮廓", "framing": "tone_first"},
            {"label": "长廊里45度侧身站定，空间纵深参与构图", "framing": "tone_first"},
            {"label": "白墙前完整背影站定，服装后片清晰", "framing": "product_first"},
        ],
    },
    "daily_snapshot": {
        "front_full_body": [
            {
                "label": "厨房岛台旁正面全身自然站立，朋友平视随手拍",
                "framing": "product_first",
            },
            {
                "label": "玄关处正面全身小步站定，前后脚都接地",
                "framing": "product_first",
            },
            {
                "label": "阳台移门旁正面全身站定，午后侧光打在身上",
                "framing": "product_first",
            },
            {
                "label": "客厅深处远一点的全身站姿，地板光斑自然",
                "framing": "tone_first",
            },
        ],
        "natural_pose": [
            {"label": "沙发旁自然站立，单手轻整理外套下摆", "framing": "product_first"},
            {
                "label": "餐桌旁前侧身站定，肩线放松，目光略偏侧",
                "framing": "product_first",
            },
            {"label": "楼梯口扶手旁站定，单手轻搭扶手", "framing": "tone_first"},
            {"label": "客厅地毯边小步停下，重心稳定", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "上身近景，厨房窗光下低头轻扣纽扣", "framing": "product_first"},
            {
                "label": "上身近景，正在拨耳后碎发，目光向窗外",
                "framing": "product_first",
            },
            {"label": "上身近景，餐桌旁手指轻碰前襟", "framing": "product_first"},
            {"label": "上身近景，书桌前自然抬头，居家光线", "framing": "product_first"},
        ],
        "side_or_back": [
            {
                "label": "阳台移门旁完整侧身站定，自然光勾出轮廓",
                "framing": "product_first",
            },
            {"label": "窗前完整背影站立，肩线放松", "framing": "tone_first"},
            {"label": "厨房入口侧身站定，居家场景入镜", "framing": "tone_first"},
            {"label": "沙发旁45度背面站定，单手轻扶后摆", "framing": "product_first"},
        ],
    },
    "natural_phone_snapshot": {
        "front_full_body": [
            {
                "label": "客厅落地窗前正面全身站定，手机平视随手拍",
                "framing": "product_first",
            },
            {
                "label": "卧室门口正面全身小步站定，室内自然光",
                "framing": "product_first",
            },
            {
                "label": "玄关处正面全身自然站立，前后脚都接地",
                "framing": "product_first",
            },
            {
                "label": "走廊深处远一点的全身站姿，透视自然不广角",
                "framing": "tone_first",
            },
        ],
        "natural_pose": [
            {
                "label": "门框旁侧身十五度站定，正在轻整理袖口",
                "framing": "product_first",
            },
            {"label": "客厅地毯边自然站立，单手轻碰下摆", "framing": "product_first"},
            {"label": "厨房中岛旁轻靠台面站定，身体仍直立", "framing": "tone_first"},
            {
                "label": "书桌前45度侧身站定，回看镜头，脚步稳定",
                "framing": "tone_first",
            },
        ],
        "detail_half_body": [
            {
                "label": "上身近景，手机平视角度，手指轻碰领口",
                "framing": "product_first",
            },
            {"label": "上身近景，平视手机镜头前轻拨刘海", "framing": "product_first"},
            {
                "label": "上身近景，自然光从窗户斜射，单手轻扶前襟",
                "framing": "product_first",
            },
            {"label": "上身近景，书架旁轻整理袖口", "framing": "product_first"},
        ],
        "side_or_back": [
            {
                "label": "落地窗边完整侧身站立，自然光勾出轮廓",
                "framing": "product_first",
            },
            {"label": "阳台门旁完整背影站立，画面带居家纵深", "framing": "tone_first"},
            {"label": "飘窗旁45度侧身站定，望向窗外", "framing": "tone_first"},
            {"label": "玄关墙边完整背面站立，肩线放松", "framing": "product_first"},
        ],
    },
    "social_seed": {
        "front_full_body": [
            {
                "label": "试衣间软椅旁正面全身站定，轻扶衣侧展示穿搭",
                "framing": "product_first",
            },
            {
                "label": "卧室衣柜前正面全身自然站立，刚整理好前襟",
                "framing": "product_first",
            },
            {
                "label": "步入式衣帽间灯光下正面全身轻整理袖口",
                "framing": "product_first",
            },
            {
                "label": "门厅出门前正面全身小步站定，朋友帮拍视角",
                "framing": "product_first",
            },
        ],
        "natural_pose": [
            {
                "label": "试衣间软椅旁前侧身站定，低头检查袖口",
                "framing": "product_first",
            },
            {
                "label": "化妆台旁自然站立，轻碰领口，不入镜面",
                "framing": "product_first",
            },
            {"label": "卧室衣柜前低头看前襟细节，身体站稳", "framing": "tone_first"},
            {"label": "卧室落地窗前45度侧身站定，回看镜头", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {
                "label": "上身近景，低头检查领口，衣服结构清楚",
                "framing": "product_first",
            },
            {"label": "上身近景，手指轻拈袖口纽扣", "framing": "product_first"},
            {"label": "上身近景，双手轻拢前襟，不遮挡主体", "framing": "product_first"},
            {"label": "上身近景，单手轻扶领口边缘", "framing": "product_first"},
        ],
        "side_or_back": [
            {
                "label": "试衣间软椅旁完整侧身站立，展示侧面廓形",
                "framing": "product_first",
            },
            {
                "label": "卧室衣柜前完整背面站立，单手轻扶后摆",
                "framing": "product_first",
            },
            {
                "label": "化妆间灯光下45度背面站定，后腰剪裁清楚",
                "framing": "tone_first",
            },
            {
                "label": "门厅出门前完整背面站立，侧头很轻但身体稳定",
                "framing": "tone_first",
            },
        ],
    },
}


__all__ = ["ADULT_POOL"]
