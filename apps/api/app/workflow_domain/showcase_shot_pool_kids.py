"""Child and toddler shot pools for showcase generation.

CHILD_POOL  -> 7 templates x 4 shot classes x 3 variants = 84 variants
TODDLER_POOL -> 7 templates x 4 shot classes x 2 variants = 56 variants

Design intent:
- Keep ecommerce usefulness first: clothing must stay readable, complete, and
  physically plausible on the body.
- "Natural" means a real photographer's low-amplitude child-model direction:
  standing, slight turn, small grounded step, looking away, touching straps,
  pocket, hem, or cuff.
- Avoid high-risk motion in the default pool: jumping, spinning, sitting on the
  floor, crouching, kneeling, hands overhead, and wide action poses. Those can
  become an explicit action/lifestyle mode later, not the default showcase pool.
- Avoid extra props and other people.
"""

from __future__ import annotations

from .showcase_shot_pool import ShotPool


CHILD_POOL: ShotPool = {
    "white_ecommerce": {
        "front_full_body": [
            {
                "label": "正面全身白底自然站立，双脚平放，手臂自然垂落",
                "framing": "product_first",
            },
            {
                "label": "正面全身白底轻扶两侧肩带，肩膀放松看镜头",
                "framing": "product_first",
            },
            {
                "label": "正面全身白底半步站姿，前后脚都稳定着地",
                "framing": "product_first",
            },
        ],
        "natural_pose": [
            {
                "label": "白底前侧身十五度，一手轻扶肩带，一手自然垂落",
                "framing": "product_first",
            },
            {"label": "白底前微侧身低头看口袋，双脚站稳", "framing": "product_first"},
            {"label": "白底前自然站立，小手轻碰裙摆边缘", "framing": "product_first"},
        ],
        "detail_half_body": [
            {
                "label": "白底半身特写，小手轻扶肩带，领口和前胸清晰",
                "framing": "product_first",
            },
            {"label": "白底胸口特写，小手轻碰口袋边缘", "framing": "product_first"},
            {"label": "白底半身，小手整理袖口，动作很轻", "framing": "product_first"},
        ],
        "side_or_back": [
            {
                "label": "白底完整侧身站立，手臂自然垂落，展示侧面版型",
                "framing": "product_first",
            },
            {
                "label": "白底完整背面站立，头自然朝前，不回头",
                "framing": "product_first",
            },
            {"label": "白底45度侧身站立，轻扶后摆展示长度", "framing": "product_first"},
        ],
    },
    "premium_studio": {
        "front_full_body": [
            {
                "label": "柔和棚拍正面全身自然站立，双脚平放看镜头",
                "framing": "product_first",
            },
            {"label": "棚拍正面全身轻扶肩带，肩颈放松", "framing": "product_first"},
            {
                "label": "棚拍浅灰背景远一点的全身站姿，人物仍清晰完整",
                "framing": "tone_first",
            },
        ],
        "natural_pose": [
            {
                "label": "棚拍前侧身十五度，低头看口袋，动作安静",
                "framing": "product_first",
            },
            {"label": "棚拍窗光边自然站立，视线略看向一侧", "framing": "tone_first"},
            {"label": "棚拍浅墙前小步站定，前后脚都接地", "framing": "product_first"},
        ],
        "detail_half_body": [
            {
                "label": "棚拍半身，小手轻扶领口，面料纹理清楚",
                "framing": "product_first",
            },
            {
                "label": "棚拍胸口特写，小手轻碰前襟，口袋清晰",
                "framing": "product_first",
            },
            {"label": "棚拍半身，小手整理袖口，肩部不出框", "framing": "product_first"},
        ],
        "side_or_back": [
            {
                "label": "棚拍完整侧身站定，展示衣长和侧面廓形",
                "framing": "product_first",
            },
            {"label": "棚拍完整背面站立，手臂自然下垂", "framing": "product_first"},
            {"label": "棚拍浅灰背景背面全身，背景留白干净", "framing": "tone_first"},
        ],
    },
    "urban_commute": {
        "front_full_body": [
            {
                "label": "街边花坛旁正面全身站定，双脚平放自然微笑",
                "framing": "product_first",
            },
            {"label": "街角台阶前正面全身，小手轻扶肩带", "framing": "product_first"},
            {
                "label": "公园门口远一点的全身站姿，环境干净不抢服装",
                "framing": "tone_first",
            },
        ],
        "natural_pose": [
            {
                "label": "街边前侧身小步停下，前后脚都稳定接地",
                "framing": "product_first",
            },
            {"label": "小书店门口自然站立，视线看向橱窗外侧", "framing": "tone_first"},
            {"label": "花坛旁低头看口袋，小手轻碰口袋边缘", "framing": "product_first"},
        ],
        "detail_half_body": [
            {"label": "上身近景，街边花坛旁轻扶肩带", "framing": "product_first"},
            {"label": "上身近景，街角石阶旁小手轻碰前襟", "framing": "product_first"},
            {"label": "上身近景，小书店门口小手整理袖口", "framing": "product_first"},
        ],
        "side_or_back": [
            {
                "label": "街边花坛旁完整侧身站立，展示侧面版型",
                "framing": "product_first",
            },
            {"label": "公园门口完整背面站立，头自然朝前", "framing": "product_first"},
            {"label": "街边台阶旁45度侧身站定，背景轻微虚化", "framing": "tone_first"},
        ],
    },
    "lifestyle": {
        "front_full_body": [
            {
                "label": "画廊白墙前正面全身自然站立，双脚平放",
                "framing": "product_first",
            },
            {
                "label": "儿童精品店内正面全身轻扶肩带，空间干净",
                "framing": "product_first",
            },
            {
                "label": "酒店大堂落地窗前远一点的全身站姿，人物完整清晰",
                "framing": "tone_first",
            },
        ],
        "natural_pose": [
            {
                "label": "画廊白墙前前侧身十五度，小手轻碰裙摆",
                "framing": "product_first",
            },
            {"label": "儿童阅读角旁安静站立，低头看口袋", "framing": "product_first"},
            {"label": "木质走廊中自然站定，视线略偏侧", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {
                "label": "上身近景，画廊前轻扶领口，衣服纹理清楚",
                "framing": "product_first",
            },
            {
                "label": "上身近景，儿童阅读角光下小手轻碰前襟",
                "framing": "product_first",
            },
            {
                "label": "上身近景，酒店大堂胸口特写，小手轻扶口袋",
                "framing": "product_first",
            },
        ],
        "side_or_back": [
            {"label": "画廊白墙前完整侧身站立，展示衣长", "framing": "product_first"},
            {"label": "酒店大堂完整背面站姿，手臂自然垂落", "framing": "product_first"},
            {"label": "木质走廊背面全身站定，空间留白干净", "framing": "tone_first"},
        ],
    },
    "daily_snapshot": {
        "front_full_body": [
            {
                "label": "客厅沙发前正面全身站定，朋友平视随手拍",
                "framing": "product_first",
            },
            {
                "label": "阳台窗前正面全身轻扶肩带，自然光侧照",
                "framing": "product_first",
            },
            {
                "label": "餐桌旁远一点的全身站姿，生活细节不遮挡衣服",
                "framing": "tone_first",
            },
        ],
        "natural_pose": [
            {"label": "客厅地毯边小步停下，前后脚都接地", "framing": "product_first"},
            {"label": "卧室飘窗旁自然站立，低头看口袋", "framing": "product_first"},
            {"label": "玄关旁轻靠墙边站定，肩膀放松", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {
                "label": "家中半身，小手轻扶领口，手机平视拍摄",
                "framing": "product_first",
            },
            {"label": "沙发前胸口特写，小手轻碰衣角", "framing": "product_first"},
            {"label": "厨房岛台旁半身，小手整理袖口", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "沙发旁完整侧身站立，展示侧面版型", "framing": "product_first"},
            {"label": "阳台窗前完整背面站立，头自然朝前", "framing": "product_first"},
            {"label": "楼梯口45度侧身站定，扶手只作为背景", "framing": "tone_first"},
        ],
    },
    "natural_phone_snapshot": {
        "front_full_body": [
            {
                "label": "儿童房床边正面全身站定，手机平视随手拍",
                "framing": "product_first",
            },
            {"label": "木质客厅地板上正面全身轻扶肩带", "framing": "product_first"},
            {
                "label": "儿童房落地窗前远一点的全身站姿，人物完整",
                "framing": "tone_first",
            },
        ],
        "natural_pose": [
            {
                "label": "床边前侧身十五度，小手轻碰裙摆，双脚站稳",
                "framing": "product_first",
            },
            {"label": "客厅地毯边小步停下，前后脚都接地", "framing": "product_first"},
            {"label": "儿童房书架旁自然站立，低头看口袋", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {
                "label": "儿童房半身，小手轻扶领口，手机平视拍摄",
                "framing": "product_first",
            },
            {"label": "床边胸口特写，小手轻碰衣角", "framing": "product_first"},
            {"label": "客厅半身，小手整理袖口，动作很轻", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "床边完整侧身站立，展示侧面衣长", "framing": "product_first"},
            {"label": "儿童房完整背面站立，头自然朝前", "framing": "product_first"},
            {"label": "客厅木地板上45度侧身站定，背景简单", "framing": "tone_first"},
        ],
    },
    "social_seed": {
        "front_full_body": [
            {
                "label": "卧室衣柜前正面全身站定，轻扶肩带展示穿搭",
                "framing": "product_first",
            },
            {"label": "试衣间软凳旁正面全身站立，双脚平放", "framing": "product_first"},
            {
                "label": "化妆间灯光下正面全身小步站定，人物完整清晰",
                "framing": "product_first",
            },
        ],
        "natural_pose": [
            {
                "label": "卧室落地灯旁前侧身十五度，轻碰口袋边缘",
                "framing": "product_first",
            },
            {
                "label": "试衣间软凳旁低头看衣摆，小手轻扶裙摆",
                "framing": "product_first",
            },
            {
                "label": "步入式衣帽间自然站定，背景衣物轻微虚化",
                "framing": "tone_first",
            },
        ],
        "detail_half_body": [
            {"label": "试衣间灯光下半身正在轻扶领口", "framing": "product_first"},
            {"label": "卧室衣柜前胸口特写，小手轻碰前襟", "framing": "product_first"},
            {"label": "化妆间半身，小手整理袖口", "framing": "product_first"},
        ],
        "side_or_back": [
            {
                "label": "试衣间软凳旁完整侧身站立，展示侧面版型",
                "framing": "product_first",
            },
            {
                "label": "卧室衣柜前完整背面站立，单手轻扶后摆",
                "framing": "product_first",
            },
            {"label": "卧室落地窗前45度侧身站定，空间干净", "framing": "tone_first"},
        ],
    },
}


TODDLER_POOL: ShotPool = {
    "white_ecommerce": {
        "front_full_body": [
            {
                "label": "白底正面全身自然站立，双脚平放，手臂自然垂落",
                "framing": "product_first",
            },
            {
                "label": "白底正面全身轻扶衣角，动作很小，重心稳定",
                "framing": "product_first",
            },
        ],
        "natural_pose": [
            {"label": "白底前侧身十五度站立，小手轻碰前襟", "framing": "product_first"},
            {
                "label": "白底前自然站定，视线略偏侧，双脚站稳",
                "framing": "product_first",
            },
        ],
        "detail_half_body": [
            {"label": "白底半身小手扶领口，面料和领口清楚", "framing": "product_first"},
            {"label": "白底胸口特写小手轻碰衣角", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "白底完整侧身站立，手臂自然垂落", "framing": "product_first"},
            {"label": "白底完整背面站立，头自然朝前", "framing": "product_first"},
        ],
    },
    "premium_studio": {
        "front_full_body": [
            {
                "label": "棚拍正面全身自然站立，双脚平放看镜头",
                "framing": "product_first",
            },
            {
                "label": "棚拍浅灰底远一点的全身站姿，人物完整清晰",
                "framing": "tone_first",
            },
        ],
        "natural_pose": [
            {"label": "棚拍前侧身十五度站定，小手轻扶衣角", "framing": "product_first"},
            {"label": "棚拍柔光下自然站立，视线略偏侧", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "棚拍半身小手扶领口，动作很轻", "framing": "product_first"},
            {"label": "棚拍胸口特写小手轻碰前襟", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "棚拍完整侧身站立，展示衣长", "framing": "product_first"},
            {"label": "棚拍完整背面站立，背景留白干净", "framing": "tone_first"},
        ],
    },
    "urban_commute": {
        "front_full_body": [
            {"label": "街边花坛旁正面全身站定，双脚平放", "framing": "product_first"},
            {
                "label": "涂鸦墙前远一点的全身站姿，环境不抢服装",
                "framing": "tone_first",
            },
        ],
        "natural_pose": [
            {"label": "街角台阶前侧身站定，小手轻扶衣角", "framing": "product_first"},
            {"label": "街边自然站立，视线略看向一侧", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "街拍半身小手扶领口，手机平视拍摄", "framing": "product_first"},
            {"label": "街边胸口特写小手轻碰外套衣角", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "街角完整侧身站立，展示侧面版型", "framing": "product_first"},
            {"label": "街边完整背面站立，头自然朝前", "framing": "tone_first"},
        ],
    },
    "lifestyle": {
        "front_full_body": [
            {"label": "画廊白墙前正面全身站定，双脚平放", "framing": "product_first"},
            {"label": "酒店大堂前远一点的全身站姿，人物完整", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "画廊前侧身十五度站立，小手轻扶前襟", "framing": "product_first"},
            {"label": "精品店落地灯旁自然站定，视线略偏侧", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "画廊前半身小手扶领口，衣服细节清楚", "framing": "product_first"},
            {"label": "精品店胸口特写小手轻碰衣角", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "酒店大堂完整侧身站立，展示衣长", "framing": "product_first"},
            {"label": "画廊完整背面站立，背景干净", "framing": "tone_first"},
        ],
    },
    "daily_snapshot": {
        "front_full_body": [
            {
                "label": "客厅地毯前正面全身站定，朋友平视随手拍",
                "framing": "product_first",
            },
            {
                "label": "阳台窗前正面全身轻扶衣角，自然光侧照",
                "framing": "product_first",
            },
        ],
        "natural_pose": [
            {"label": "沙发旁前侧身站定，小手轻碰前襟", "framing": "product_first"},
            {
                "label": "餐桌旁自然站立，视线略偏侧，背景不遮挡",
                "framing": "tone_first",
            },
        ],
        "detail_half_body": [
            {"label": "家中半身小手扶领口，手机平视拍摄", "framing": "product_first"},
            {"label": "沙发前胸口特写小手轻碰衣角", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "沙发旁完整侧身站立，展示侧面版型", "framing": "product_first"},
            {"label": "阳台前完整背面站立，头自然朝前", "framing": "tone_first"},
        ],
    },
    "natural_phone_snapshot": {
        "front_full_body": [
            {
                "label": "儿童房地板上正面全身站定，手机平视随手拍",
                "framing": "product_first",
            },
            {"label": "床边正面全身轻扶衣角，双脚平放", "framing": "product_first"},
        ],
        "natural_pose": [
            {"label": "床边前侧身十五度站定，小手轻碰前襟", "framing": "product_first"},
            {"label": "儿童房地毯旁自然站立，视线略偏侧", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "儿童房半身小手扶领口，手机平视拍摄", "framing": "product_first"},
            {"label": "床边胸口特写小手轻碰衣角", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "床边完整侧身站立，展示侧面衣长", "framing": "product_first"},
            {"label": "客厅木地板上完整背面站立，背景简单", "framing": "tone_first"},
        ],
    },
    "social_seed": {
        "front_full_body": [
            {"label": "试衣间软凳旁正面全身站定，双脚平放", "framing": "product_first"},
            {"label": "卧室衣柜前正面全身轻扶衣角展示穿搭", "framing": "product_first"},
        ],
        "natural_pose": [
            {
                "label": "试衣间软凳旁前侧身站定，小手轻碰前襟",
                "framing": "product_first",
            },
            {"label": "卧室落地灯旁自然站立，背景柔和虚化", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "试衣间半身小手扶领口，灯光柔和", "framing": "product_first"},
            {"label": "卧室衣柜前胸口特写小手轻碰衣角", "framing": "product_first"},
        ],
        "side_or_back": [
            {
                "label": "试衣间软凳旁完整侧身站立，展示侧面版型",
                "framing": "product_first",
            },
            {"label": "卧室软凳前完整背面站立，头自然朝前", "framing": "product_first"},
        ],
    },
}


__all__ = ["CHILD_POOL", "TODDLER_POOL"]
