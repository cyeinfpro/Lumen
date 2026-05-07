"""Adult baseline shot pool for showcase generation.

派生段（teen / adult / middle_aged / senior）共享此池子，由调用方叠加
`age_soft_constraint` 软约束句。本文件不依赖 `_showcase_shot_pool` 之外的模块。

设计准则（2026-05-08 注入活力重写）：
- 事件化：label 描述"正在做某事"，不是"保持某姿势"。
- 瞬间感：用"刚 X 完 / 正要 X / X 那一刻 / X 中被定格"等修辞替代静态摆姿。
- 动作多元：跨步/小跑/推门/伸懒腰/扣扣子/卷袖口/拨刘海/抬手挡光 等动态动作占比 ≥ 40%。
- 场景轮换：每模板 4 类机位 × 4 变体落在不同微场景。
- 不写死毛绒/包/玩具/积木 等具体道具；手机视为日常物件可低频出现。
- social_seed 不出现镜子，改用试衣间软椅/卧室衣柜前/化妆间/衣帽间/卧室落地窗等无镜场景。
"""

from __future__ import annotations

from app.routes._showcase_shot_pool import ShotPool, ShotVariant


ADULT_POOL: ShotPool = {
    # ───────────── 白底主图（white_ecommerce）─────────────
    # 白底固定，但动作丰富：站/转身回头/单手插袋/卷袖口/半蹲整理/坐凳子
    "white_ecommerce": {
        "front_full_body": [
            {"label": "正面全身白底主图，刚走到位停下，肩颈放松目视镜头", "framing": "product_first"},
            {"label": "正面全身白底，单手插袋一手抬起拨刘海，肩线放松", "framing": "product_first"},
            {"label": "正面全身白底，重心微前倾正要走的瞬间，体态舒展", "framing": "product_first"},
            {"label": "正面全身白底，转身回头看镜头那一刻，发丝微扬", "framing": "product_first"},
        ],
        "natural_pose": [
            {"label": "白底前侧身十五度，单手卷起袖口的瞬间", "framing": "product_first"},
            {"label": "白底前微侧身正在拨耳后碎发，目光略偏侧", "framing": "product_first"},
            {"label": "白底前坐木凳，刚坐下双手放膝神态松弛", "framing": "product_first"},
            {"label": "白底前半蹲整理裤脚后正起身那一刻", "framing": "product_first"},
        ],
        "detail_half_body": [
            {"label": "上身近景，正在轻碰领口检查面料的瞬间", "framing": "product_first"},
            {"label": "上身近景，双手轻松交叠胸前抿嘴一笑", "framing": "product_first"},
            {"label": "上身近景，手指轻拈袖口边缘的动作中", "framing": "product_first"},
            {"label": "上身近景，单手撩发另一手轻按胸前的瞬间", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "完整侧影刚走到位站定，手臂自然下垂", "framing": "product_first"},
            {"label": "完整背影正要回头那一刻，肩膀放松", "framing": "product_first"},
            {"label": "白底前侧身九十度，单手轻搭腰侧看廓形", "framing": "product_first"},
            {"label": "白底前背影微侧回头，单手轻提下摆", "framing": "product_first"},
        ],
    },
    # ───────────── 高级棚拍（premium_studio）─────────────
    # 大片调性：跳跃/甩头/单腿后踢/坐地半躺/转身回眸/抬手挡光/后仰大笑
    "premium_studio": {
        "front_full_body": [
            {"label": "正面全身大片，刚跨步停下，眼神冷静直视镜头", "framing": "product_first"},
            {"label": "正面全身灰底前，抬手挡光的瞬间气场松弛", "framing": "product_first"},
            {"label": "正面全身腾空跳跃，双脚离地衣摆飞扬", "framing": "tone_first"},
            {"label": "正面远景全身，风机吹散衣摆和头发，气场全开", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "侧身松弛单手插袋，正要扭头那一刻", "framing": "product_first"},
            {"label": "侧身甩头发瞬间，发丝飞散封面动态感", "framing": "tone_first"},
            {"label": "单腿后踢扭身回头，动作幅度大但肢体放松", "framing": "tone_first"},
            {"label": "坐地半躺单手撑地后仰大笑，慵懒杂志感", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "上身近景棚拍光，正在轻碰领口检查质感", "framing": "product_first"},
            {"label": "上身近景，手指正在轻拈袖口的动作中", "framing": "product_first"},
            {"label": "上身近景，双手交叠胸前抿嘴一笑的瞬间", "framing": "product_first"},
            {"label": "上身近景，单手扶发另一手垂落，颈线入镜", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "完整侧影刚走到位停下，肩线松弛", "framing": "product_first"},
            {"label": "转身回眸单手撩起后摆那一刻", "framing": "tone_first"},
            {"label": "完整背影双手举起延展身体线条", "framing": "tone_first"},
            {"label": "侧身后仰单手撑腰大笑，动作幅度大但松弛", "framing": "tone_first"},
        ],
    },
    # ───────────── 质感街拍（urban_commute）─────────────
    # 动作：跨过斑马线/小跑/推门/抬手挡光/接电话/拨刘海/挂柱子张望
    "urban_commute": {
        "front_full_body": [
            {"label": "正面全身，跨过斑马线那一刻被定格，目光看路口", "framing": "product_first"},
            {"label": "正面全身，推开咖啡店玻璃门走出，街灯逆射", "framing": "product_first"},
            {"label": "正面全身，从地铁出口迎面小跑出来，光从出入口逆射", "framing": "tone_first"},
            {"label": "正面远景全身，天桥上停步张望，城市天际线延展", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "便利店门口刚停下回头，街灯打在身上", "framing": "product_first"},
            {"label": "公交站柱子旁单手挂着张望，肩颈放松", "framing": "product_first"},
            {"label": "商业街匆忙跨步被定格，脚步张开瞬间感", "framing": "tone_first"},
            {"label": "公园门口侧身张望，风吹乱头发抬手按住", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "上身近景，街角正在整理外套领子的动作中", "framing": "product_first"},
            {"label": "上身近景，路灯下双手插口袋抿嘴一笑", "framing": "product_first"},
            {"label": "上身近景，咖啡店外正接电话那一刻", "framing": "product_first"},
            {"label": "上身近景，正在拨耳后碎发的瞬间，街头逆光", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "完整侧身边走边推外套袖口，背景街景虚化", "framing": "product_first"},
            {"label": "背影斜跨一只手插袋走向地铁口", "framing": "tone_first"},
            {"label": "侧身回头看街角橱窗那一刻", "framing": "tone_first"},
            {"label": "走过路口的远景侧影，光线自然打在身上", "framing": "tone_first"},
        ],
    },
    # ───────────── 精品空间（lifestyle）─────────────
    # 动作：从一幅画走向下一幅/拨袖看时间/翻阅一页/伸手轻碰陈列/缓步走动
    "lifestyle": {
        "front_full_body": [
            {"label": "正面全身，画廊白墙前从一幅画走向下一幅，半侧身回看", "framing": "product_first"},
            {"label": "正面全身，选物店木柜前伸手轻碰陈列，目光略低", "framing": "product_first"},
            {"label": "正面远景全身，美术馆庭院水池边缓步走动", "framing": "tone_first"},
            {"label": "正面远景全身，酒店大堂中庭刚步入那一刻，吊灯虚化身后", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "选物店木柜旁拨袖看时间的动作中", "framing": "product_first"},
            {"label": "酒店软椅旁倚靠扶手，单手撑膝盖目光偏侧", "framing": "product_first"},
            {"label": "落地窗边背手而立望向窗外，剪影感强", "framing": "tone_first"},
            {"label": "阳光阅读角刚翻完一页抬头那一刻", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "上身近景，画廊白墙前侧面光抿嘴一笑", "framing": "product_first"},
            {"label": "上身近景，选物店木柜前低头看物的动作中", "framing": "product_first"},
            {"label": "上身近景，落地窗边逆光抬手挡光那一刻", "framing": "product_first"},
            {"label": "上身近景，美术馆庭院光线柔和，正在拨刘海", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "侧身画廊白墙前驻足看画，肩线松弛带出轮廓", "framing": "product_first"},
            {"label": "背影正要走向落地窗那一刻，逆光剪影", "framing": "tone_first"},
            {"label": "侧身回望长廊深处，空间纵深参与构图", "framing": "tone_first"},
            {"label": "完整背影驻足留白墙前，画面大半留给空间", "framing": "tone_first"},
        ],
    },
    # ───────────── 日常随拍（daily_snapshot）─────────────
    # 动作：推开移门/伸懒腰/扣纽扣/披外套/上楼梯回头/跨门槛/翻身坐起
    "daily_snapshot": {
        "front_full_body": [
            {"label": "正面全身，厨房岛台旁刚伸懒腰回头被拍", "framing": "product_first"},
            {"label": "正面全身，玄关换好鞋正要出门那一刻回头", "framing": "product_first"},
            {"label": "正面全身，推开阳台移门走出，午后光打脸侧", "framing": "product_first"},
            {"label": "正面远景全身，客厅深处迎光走来，地板上斜射光斑", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "沙发旁正在披外套到肩上的动作中", "framing": "product_first"},
            {"label": "餐桌旁单手撑桌沿，肩线放松抿嘴一笑", "framing": "product_first"},
            {"label": "楼梯口扶把手正要上楼那一刻回头", "framing": "tone_first"},
            {"label": "客厅地毯上盘腿坐着伸懒腰，居家随性", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "上身近景，厨房窗光下侧脸正在低头扣纽扣", "framing": "product_first"},
            {"label": "上身近景，正在拨耳后碎发，目光向窗外", "framing": "product_first"},
            {"label": "上身近景，餐桌旁双手交叠抱臂抿嘴一笑", "framing": "product_first"},
            {"label": "上身近景，书桌前抬头那一刻，居家光线", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "侧身正要推开阳台移门，自然光勾出轮廓", "framing": "product_first"},
            {"label": "背影望向窗外远方，肩线放松", "framing": "tone_first"},
            {"label": "侧身正走向厨房深处，居家场景入镜", "framing": "tone_first"},
            {"label": "背影翻身坐到沙发扶手上的瞬间", "framing": "tone_first"},
        ],
    },
    # ───────────── 自然手机摄影（natural_phone_snapshot）─────────────
    # 动作：推门/转身/低头扣纽扣/单膝跪坐/卷袖口/抬手挡光/伸懒腰
    "natural_phone_snapshot": {
        "front_full_body": [
            {"label": "正面全身，客厅落地窗前抬手挡光那一刻", "framing": "product_first"},
            {"label": "正面全身，卧室门口刚推门进来，室内自然光", "framing": "product_first"},
            {"label": "正面全身，玄关换好鞋正要出门那一刻回头", "framing": "product_first"},
            {"label": "正面远景全身，走廊深处迎面走来带室内透视", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "侧身倚靠门框正在卷袖口的动作中", "framing": "product_first"},
            {"label": "客厅地毯上单膝跪坐正起身那一刻", "framing": "product_first"},
            {"label": "厨房中岛旁倚靠台面单手撑下巴，目光偏侧", "framing": "tone_first"},
            {"label": "工作书桌前转身回头，发丝散落肩侧", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "上身近景，手机竖拍角度，正在轻碰锁骨抿嘴一笑", "framing": "product_first"},
            {"label": "上身近景，平视手机镜头正在拨刘海", "framing": "product_first"},
            {"label": "上身近景，单手撩发的瞬间，自然光从窗户斜射", "framing": "product_first"},
            {"label": "上身近景，书架旁刚翻完一页抬头那一刻", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "侧身刚走到落地窗边停下，自然光勾出轮廓", "framing": "product_first"},
            {"label": "背影正要走向阳台门那一刻，画面带居家纵深", "framing": "tone_first"},
            {"label": "侧身刚坐到飘窗台沿，望向窗外剪影氛围", "framing": "tone_first"},
            {"label": "背影靠在玄关墙边伸懒腰那一刻", "framing": "tone_first"},
        ],
    },
    # ───────────── 自然种草（social_seed）─────────────
    # 动作：转一圈让裙摆张开/卷袖口/拉拉链/整理袖口/低头检查/单手撑柜子
    "social_seed": {
        "front_full_body": [
            {"label": "正面全身，试衣间软椅旁转一圈让裙摆张开", "framing": "product_first"},
            {"label": "正面全身，卧室衣柜前刚拉好拉链回头那一刻", "framing": "product_first"},
            {"label": "正面全身，步入式衣帽间灯光下卷袖口的瞬间", "framing": "product_first"},
            {"label": "正面全身，门厅出门前正要回头那一刻", "framing": "product_first"},
        ],
        "natural_pose": [
            {"label": "试衣间软椅旁侧身正在整理袖口，低头检查细节", "framing": "product_first"},
            {"label": "化妆台旁坐姿微侧，正在轻碰领口（不入镜面）", "framing": "product_first"},
            {"label": "卧室衣柜前刚低头看完吊牌抬头那一刻", "framing": "tone_first"},
            {"label": "卧室落地窗前正要转身回头那一刻", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "上身近景，正在低头检查领口的瞬间", "framing": "product_first"},
            {"label": "上身近景，手指正在轻拈袖口纽扣的动作中", "framing": "product_first"},
            {"label": "上身近景，双手轻拢前襟抿嘴一笑", "framing": "product_first"},
            {"label": "上身近景，单手轻提领巾末端的瞬间", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "试衣间软椅旁侧身单手撑柜看廓形", "framing": "product_first"},
            {"label": "卧室衣柜前背影正要回头那一刻，单手轻撩后摆", "framing": "product_first"},
            {"label": "化妆间灯光下转身看后腰剪裁的瞬间", "framing": "tone_first"},
            {"label": "门厅出门前背对镜头侧身回望，朋友帮拍视角", "framing": "tone_first"},
        ],
    },
}


__all__ = ["ADULT_POOL"]
