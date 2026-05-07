"""Adult baseline shot pool for showcase generation.

派生段（teen / adult / middle_aged / senior）共享此池子，由调用方叠加
`age_soft_constraint` 软约束句。本文件不依赖 `_showcase_shot_pool` 之外的模块。
"""

from __future__ import annotations

from app.routes._showcase_shot_pool import ShotPool, ShotVariant


ADULT_POOL: ShotPool = {
    "white_ecommerce": {
        "front_full_body": [
            {"label": "正面全身白底主图，双手自然下垂贴身，目视镜头", "framing": "product_first"},
            {"label": "正面全身，单手轻插裤袋，肩线放松直视镜头", "framing": "product_first"},
            {"label": "正面全身，双手交叠身前，站姿端正展示版型", "framing": "product_first"},
            {"label": "正面全身，重心微移单脚点地，姿态紧凑利落", "framing": "product_first"},
        ],
        "natural_pose": [
            {"label": "侧身十五度站立，单手轻搭腰侧，便于看廓形", "framing": "product_first"},
            {"label": "正面收腰单手插袋，另一手自然垂落", "framing": "product_first"},
            {"label": "微侧身双手垂落，下颌微收，紧凑展示线条", "framing": "product_first"},
            {"label": "正面单手轻拢前襟，另一手贴身，棚拍标准姿", "framing": "product_first"},
        ],
        "detail_half_body": [
            {"label": "上身近景，手轻碰领口，凸显面料质感", "framing": "product_first"},
            {"label": "上身近景，双手交叠胸前，展示版型轮廓", "framing": "product_first"},
            {"label": "上身近景，手指拈起袖口边缘，强调剪裁细节", "framing": "product_first"},
            {"label": "上身近景，单手轻按胸前口袋，凸显工艺", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "完整侧影站立，手臂自然下垂，呈现服装侧面线条", "framing": "product_first"},
            {"label": "完整背影双手垂落，展示后片版型", "framing": "product_first"},
            {"label": "侧身九十度站姿，单手轻搭腰侧，看腰线裁剪", "framing": "product_first"},
            {"label": "背影单手轻提下摆，露出后腰剪裁细节", "framing": "product_first"},
        ],
    },
    "premium_studio": {
        "front_full_body": [
            {"label": "正面全身大片，姿态笔挺收下巴，眼神冷静直视镜头", "framing": "product_first"},
            {"label": "正面全身，单手插袋收腰，目光略偏侧，紧凑展示", "framing": "product_first"},
            {"label": "正面全身腾空跳跃，双脚离地，衣摆飞扬", "framing": "tone_first"},
            {"label": "正面远景全身，风机吹起衣摆和头发，气场全开", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "侧身收腰单手插袋，姿态克制紧凑", "framing": "product_first"},
            {"label": "侧身扭腰甩头发，发丝飞散，封面张力", "framing": "tone_first"},
            {"label": "单腿后踢扭身回头，肢体张力满", "framing": "tone_first"},
            {"label": "坐地半躺单手撑地，衣摆自然摊开，慵懒杂志感", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "上身近景，手轻碰领口，凸显面料质感", "framing": "product_first"},
            {"label": "上身近景，手指拈起袖口，强调剪裁细节", "framing": "product_first"},
            {"label": "上身近景，双手交叠胸前，展示版型轮廓", "framing": "product_first"},
            {"label": "上身近景，单手扶发，颈线和服装上半身入镜", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "完整侧影，姿态笔挺，强调服装轮廓线条", "framing": "product_first"},
            {"label": "转身回眸单手撩起后摆，动态张力", "framing": "tone_first"},
            {"label": "完整背影双手举起，延展身体线条", "framing": "tone_first"},
            {"label": "侧身后仰单手撑腰，肢体张力满", "framing": "tone_first"},
        ],
    },
    "urban_commute": {
        "front_full_body": [
            {"label": "街头停步正面照，单手挎包，神情松弛", "framing": "product_first"},
            {"label": "站立等灯正面，目光平视前方，自然不摆拍", "framing": "product_first"},
            {"label": "迎面走来，双手自然摆动，瞬间定格", "framing": "tone_first"},
            {"label": "街角远景，街景延伸为画面主体，人占画面 1/3", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "街边停下整理外套，目光不看镜头", "framing": "product_first"},
            {"label": "走动中侧身回头，发丝微扬，路人抓拍感", "framing": "tone_first"},
            {"label": "街角停步看手机，姿态自然不刻意", "framing": "tone_first"},
            {"label": "跨步前行被定格，街边橱窗入镜参与构图", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "上半身近景，整理外套领子，街头光影", "framing": "product_first"},
            {"label": "上半身近景，双手插口袋，肩线放松", "framing": "product_first"},
            {"label": "上半身近景，挎包带斜跨胸前，展示搭配层次", "framing": "product_first"},
            {"label": "上半身近景，单手撩发，街头神情自然", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "完整侧身行走，背景街景虚化", "framing": "product_first"},
            {"label": "背影斜跨包袋走向远处，姿态自然", "framing": "tone_first"},
            {"label": "侧身转头回望，街头瞬间感", "framing": "tone_first"},
            {"label": "走过路口的远景侧影，光线自然打在身上", "framing": "tone_first"},
        ],
    },
    "lifestyle": {
        "front_full_body": [
            {"label": "画廊大厅正面站立，单手垂落，背景大片留白", "framing": "product_first"},
            {"label": "精品店扶柱正面，目光平视，姿态从容", "framing": "product_first"},
            {"label": "落地窗前远景全身，侧逆光勾边，人占画面 1/3", "framing": "tone_first"},
            {"label": "酒店大堂深处走向镜头，空间纵深拉长画面", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "酒店沙发前侧身站立，单手轻搭沙发背", "framing": "product_first"},
            {"label": "画廊空间内缓步走动，目光偏侧不看镜头", "framing": "tone_first"},
            {"label": "落地窗边背手而立，望向窗外，剪影感强", "framing": "tone_first"},
            {"label": "精品店内扶柱凝望，侧逆光勾出轮廓", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "上身近景，手轻按腰带扣，背景虚化为暖灰", "framing": "product_first"},
            {"label": "上身近景，单手扶柱另一手垂落，光影柔和", "framing": "product_first"},
            {"label": "上身近景，手指拈起领巾边缘，质感细节入镜", "framing": "product_first"},
            {"label": "上身近景，双手交叠胸前，背景留白干净", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "侧身站立画廊白墙前，姿态利落显轮廓", "framing": "product_first"},
            {"label": "背影走向落地窗，逆光剪影拉长", "framing": "tone_first"},
            {"label": "侧身回望长廊深处，空间纵深参与构图", "framing": "tone_first"},
            {"label": "完整背影驻足留白墙前，画面大半留给空间", "framing": "tone_first"},
        ],
    },
    "daily_snapshot": {
        "front_full_body": [
            {"label": "家中沙发前正面站立，双手自然垂落，朋友视角", "framing": "product_first"},
            {"label": "阳台前正面，单手扶门框，自然光打在身上", "framing": "product_first"},
            {"label": "餐桌旁正面而立，单手轻按桌沿，松弛抓拍感", "framing": "product_first"},
            {"label": "客厅深处迎光走来，远景全身入镜，生活氛围浓", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "沙发旁侧身整理头发，目光不看镜头", "framing": "product_first"},
            {"label": "餐桌旁倚靠桌沿，单手撑桌，神情松弛", "framing": "product_first"},
            {"label": "阳台边自然回眸，发丝被风吹起一缕", "framing": "tone_first"},
            {"label": "客厅地毯上盘腿坐着，单手撑地，居家随性", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "上身近景，手轻按胸前，家中柔和自然光", "framing": "product_first"},
            {"label": "上身近景，单手撩耳后碎发，目光向窗外", "framing": "product_first"},
            {"label": "上身近景，双手交叠抱臂，沙发为背景虚化", "framing": "product_first"},
            {"label": "上身近景，手指轻碰领口纽扣，居家光线", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "侧身站在阳台前，自然光勾出服装轮廓", "framing": "product_first"},
            {"label": "背影望向窗外远方，肩线放松", "framing": "tone_first"},
            {"label": "侧身走向厨房深处，居家场景入镜", "framing": "tone_first"},
            {"label": "背影坐在沙发扶手上，居家空间作背景", "framing": "tone_first"},
        ],
    },
    "natural_phone_snapshot": {
        "front_full_body": [
            {"label": "客厅正面站立全身，平视镜头，手机竖屏构图", "framing": "product_first"},
            {"label": "卧室门口正面照，双手自然垂落，室内自然光", "framing": "product_first"},
            {"label": "玄关正面而立，单手扶墙，构图紧凑显版型", "framing": "product_first"},
            {"label": "走廊深处迎面走来，画面带室内透视", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "侧身倚靠门框，单手插袋，手机随手拍质感", "framing": "product_first"},
            {"label": "客厅地毯上单膝跪坐，手轻搭膝盖", "framing": "product_first"},
            {"label": "厨房中岛旁倚靠台面，目光偏侧不看镜头", "framing": "tone_first"},
            {"label": "卧室落地镜旁回头，发丝散落肩侧", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "上身近景，手机竖拍角度，单手轻碰锁骨", "framing": "product_first"},
            {"label": "上身近景，平视手机镜头，手指拈起袖口", "framing": "product_first"},
            {"label": "上身近景，单手撩发，自然光从窗户斜射", "framing": "product_first"},
            {"label": "上身近景，双手交握胸前，居家虚化背景", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "侧身站在落地窗边，自然光勾出版型轮廓", "framing": "product_first"},
            {"label": "背影走向阳台门，画面带居家纵深", "framing": "tone_first"},
            {"label": "侧身坐在窗边长凳，望向窗外，剪影氛围", "framing": "tone_first"},
            {"label": "背影靠在玄关墙边，自然光从背后铺开", "framing": "tone_first"},
        ],
    },
    "social_seed": {
        "front_full_body": [
            {"label": "试衣镜前正面而立，单手轻提下摆展示版型", "framing": "product_first"},
            {"label": "试衣间正面照，双手自然垂落，对镜微笑", "framing": "product_first"},
            {"label": "梳妆台前正面站立，单手撩发，展示整体搭配", "framing": "product_first"},
            {"label": "全身镜前正面，双手插袋微微低头看版型", "framing": "product_first"},
        ],
        "natural_pose": [
            {"label": "试衣镜前侧身整理袖口，对镜检查细节", "framing": "product_first"},
            {"label": "梳妆台前坐姿微侧，单手轻碰领口", "framing": "product_first"},
            {"label": "试衣镜前低头看吊牌，单手拈起标签", "framing": "tone_first"},
            {"label": "镜前转身扭腰看后片版型，回头自拍感", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "上身近景，对镜整理领口，强调试穿氛围", "framing": "product_first"},
            {"label": "上身近景，手指拈起袖口纽扣，凸显细节", "framing": "product_first"},
            {"label": "上身近景，双手轻拢前襟，展示版型剪裁", "framing": "product_first"},
            {"label": "上身近景，单手提起领巾末端，搭配细节入镜", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "试衣镜前侧身站立，单手叉腰看廓形", "framing": "product_first"},
            {"label": "镜前背影回头，单手撩后摆展示后片", "framing": "product_first"},
            {"label": "镜前转身看后腰剪裁，肩侧入镜", "framing": "tone_first"},
            {"label": "背对镜头侧身回望，镜中倒影一同入镜", "framing": "tone_first"},
        ],
    },
}


__all__ = ["ADULT_POOL"]
