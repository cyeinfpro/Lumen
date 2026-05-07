"""Child and toddler shot pools for showcase generation.

CHILD_POOL  → 7 模板 × 4 类机位 × 3 变体 = 84 条
TODDLER_POOL → 7 模板 × 4 类机位 × 2 变体 = 56 条

设计准则（2026-05-08 注入活力重写）：
- 事件化 + 瞬间感：用"刚 X 完 / 正在 X 中 / X 那一刻 / 跨步 / 蹦跳 / 跑两步停下"等替代静态摆姿。
- 童趣动作：单脚跳/双脚跳/蹦跳/转圈/伸懒腰/张开双手/踮脚伸手/跨大步/翻身坐起/盘腿坐仰头大笑 等。
- 幼儿动作：趴着抬头/坐着双手举起/张开双臂/转头看/抓自己衣摆/摇晃身子/蹲坐看地 等。
- 不写死毛绒/包/玩具/积木/绘本/拍手/比心/小皮包/小花/气球 等具体道具。
- social_seed 不出现镜子；改用试衣间软椅/卧室衣柜前/化妆间/衣帽间/卧室落地窗等无镜场景。
- 不出现"牵手/陪伴/妈妈"等需他人配合的描述（toddler 强约束）。
"""

from __future__ import annotations

from app.routes._showcase_shot_pool import ShotPool, ShotVariant


CHILD_POOL: ShotPool = {
    # ───────────── 白底主图（white_ecommerce）─────────────
    # 配比 12P / 0T；动作丰富：站/蹦跳/转圈/单脚跳/盘腿坐/跨大步/张开双手
    "white_ecommerce": {
        "front_full_body": [
            {"label": "正面全身白底，刚跨步停下咧嘴大笑", "framing": "product_first"},
            {"label": "正面全身白底，张开双手转身那一刻", "framing": "product_first"},
            {"label": "正面全身白底，转半圈让衣摆张开瞬间被定格", "framing": "product_first"},
        ],
        "natural_pose": [
            {"label": "白底前侧身十五度，正在拨开刘海咧嘴一笑", "framing": "product_first"},
            {"label": "白底前单脚跳起小动作那一刻", "framing": "product_first"},
            {"label": "白底前盘腿坐木凳，刚坐下伸懒腰", "framing": "product_first"},
        ],
        "detail_half_body": [
            {"label": "白底半身特写，正在轻碰领口神情自然", "framing": "product_first"},
            {"label": "白底胸口特写，小手轻碰衣角抿嘴一笑", "framing": "product_first"},
            {"label": "白底半身，小手摸袖口的动作中", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "白底侧身回头那一刻咧嘴笑", "framing": "product_first"},
            {"label": "白底背影正要回头，单手轻撩后摆", "framing": "product_first"},
            {"label": "白底45度侧身，回眸看镜头瞬间", "framing": "product_first"},
        ],
    },
    # ───────────── 高级棚拍童版（premium_studio）─────────────
    # 配比 7P / 5T；动作：跨步/转圈/仰头大笑/伸懒腰/双手举高/蹲下看地
    "premium_studio": {
        "front_full_body": [
            {"label": "棚拍正面全身，刚跨步停下仰头大笑", "framing": "product_first"},
            {"label": "棚拍正面全身，双手举高伸懒腰那一刻", "framing": "product_first"},
            {"label": "棚拍灰墙前小身影刚跨步停下，远景", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "棚拍前原地转圈让裙摆张开，发丝飞散", "framing": "product_first"},
            {"label": "棚拍冷光下小身影低头略走神那一刻", "framing": "tone_first"},
            {"label": "棚拍黑底前小身影刚蹲下看地，远景", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "棚拍半身，正在轻扶领口的瞬间", "framing": "product_first"},
            {"label": "棚拍胸口特写，小手轻碰前襟抿嘴一笑", "framing": "product_first"},
            {"label": "棚拍半身，小手摸袖口的动作中", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "棚拍侧身回头那一刻咧嘴一笑", "framing": "product_first"},
            {"label": "棚拍冷光下背影小身影刚走到位", "framing": "tone_first"},
            {"label": "棚拍灰底背面，小手垂在身侧正要回头", "framing": "tone_first"},
        ],
    },
    # ───────────── 质感街拍童版（urban_commute）─────────────
    # 微场景：街边花坛/街角台阶/涂鸦墙/公园门口/喷泉边/路边长椅/小书店门口/街角石阶
    # 动作：蹦跳/跨步/单脚跳/踮脚伸手/蹲下看地/跑两步停下回头/张望
    "urban_commute": {
        "front_full_body": [
            {"label": "正面全身，街边花坛旁蹦跳一下回头大笑", "framing": "product_first"},
            {"label": "正面全身，街角台阶上踮脚伸手够望那一刻", "framing": "product_first"},
            {"label": "正面全身远景，公园门口小身影跑两步停下张望", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "涂鸦墙前转圈让衣摆张开瞬间", "framing": "product_first"},
            {"label": "街角马赛克地砖单脚跳起那一刻", "framing": "tone_first"},
            {"label": "路边长椅旁小身影刚蹲下看地", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "上身近景，街边花坛旁正在轻碰肩带", "framing": "product_first"},
            {"label": "上身近景，街角石阶旁小手轻碰外套衣角抿嘴笑", "framing": "product_first"},
            {"label": "上身近景，小书店门口小手摸口袋的动作中", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "街边花坛旁侧身回头那一刻咧嘴笑", "framing": "product_first"},
            {"label": "公园门口背影正要走开那一刻", "framing": "tone_first"},
            {"label": "街边台阶坐着背影手撑膝盖，肩膀微动", "framing": "tone_first"},
        ],
    },
    # ───────────── 精品空间童版（lifestyle）─────────────
    # 微场景：画廊白墙/酒店大堂落地窗/儿童精品店/美术馆儿童区/落地灯旁/木质走廊/儿童阅读角
    # 动作：跨步/转圈/蹲下看作品/张开双手/侧身回头/伸懒腰
    "lifestyle": {
        "front_full_body": [
            {"label": "正面全身，画廊白墙前刚跨步停下咧嘴笑", "framing": "product_first"},
            {"label": "正面全身，酒店大堂落地窗前张开双手转身那一刻", "framing": "product_first"},
            {"label": "儿童精品店内小身影刚走到木地板上远景", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "画廊白墙前转圈让裙摆张开瞬间", "framing": "product_first"},
            {"label": "美术馆儿童区刚蹲下看作品那一刻", "framing": "tone_first"},
            {"label": "落地灯旁小身影刚走到位停下，远景", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "上身近景，画廊前正在轻扶领口", "framing": "product_first"},
            {"label": "上身近景，儿童阅读角光下小手轻碰前襟抿嘴笑", "framing": "product_first"},
            {"label": "上身近景，酒店大堂胸口特写小手摸口袋的动作中", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "画廊侧身回头那一刻咧嘴一笑", "framing": "product_first"},
            {"label": "酒店大堂背影正要走向远处那一刻", "framing": "tone_first"},
            {"label": "木质走廊背对镜头小身影刚坐下木地板", "framing": "tone_first"},
        ],
    },
    # ───────────── 日常随拍童版（daily_snapshot）─────────────
    # 微场景：客厅沙发/阳台/餐桌/客厅地毯/厨房岛台旁/玄关/楼梯口/卧室飘窗/书桌前
    # 动作：踮脚伸手/盘腿坐仰头大笑/单脚跳/翻身坐起/跨门槛/伸懒腰/蹦跳
    "daily_snapshot": {
        "front_full_body": [
            {"label": "正面，客厅沙发前蹦跳一下咧嘴一笑", "framing": "product_first"},
            {"label": "正面，阳台窗前踮脚伸手够望那一刻", "framing": "product_first"},
            {"label": "餐桌旁小身影刚跨步走到位远景", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "沙发上盘腿坐着仰头大笑那一刻", "framing": "product_first"},
            {"label": "客厅地毯上单脚跳起小动作瞬间", "framing": "product_first"},
            {"label": "卧室飘窗台沿坐着低头神情自然", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "家中半身，正在轻扶领口咧嘴笑", "framing": "product_first"},
            {"label": "沙发前胸口特写，小手轻碰衣角抿嘴一笑", "framing": "product_first"},
            {"label": "厨房岛台旁半身小手摸袖口的动作中", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "沙发上侧身回头那一刻咧嘴一笑", "framing": "product_first"},
            {"label": "阳台背影正要走向窗前那一刻", "framing": "tone_first"},
            {"label": "楼梯口背影刚坐下扶把手", "framing": "tone_first"},
        ],
    },
    # ───────────── 自然手机摄影童版（natural_phone_snapshot）─────────────
    # 微场景：儿童房床边/木质客厅地板/儿童房落地窗/客厅地毯/书架旁/飘窗/玄关
    # 动作：踮脚伸手/盘腿坐/单脚跳/翻身坐起/伸懒腰/低头略走神
    "natural_phone_snapshot": {
        "front_full_body": [
            {"label": "儿童房床边刚翻身坐起咧嘴笑", "framing": "product_first"},
            {"label": "木质客厅地板上踮脚伸手够望那一刻", "framing": "product_first"},
            {"label": "儿童房落地窗前小身影刚走到位远景", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "床上盘腿坐着伸懒腰咧嘴笑", "framing": "product_first"},
            {"label": "客厅地毯上单脚跳起小动作瞬间", "framing": "product_first"},
            {"label": "儿童房书架旁小身影低头略走神那一刻", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "儿童房半身，正在轻扶领口", "framing": "product_first"},
            {"label": "床边胸口特写，小手轻碰衣角抿嘴一笑", "framing": "product_first"},
            {"label": "客厅半身小手摸袖口的动作中", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "床边侧身回头那一刻咧嘴一笑", "framing": "product_first"},
            {"label": "儿童房背影正要走向窗前那一刻", "framing": "tone_first"},
            {"label": "客厅木地板上背影小身影刚坐下", "framing": "tone_first"},
        ],
    },
    # ───────────── 自然种草童版（social_seed）─────────────
    # 微场景：试衣间软凳/卧室衣柜前/化妆间灯光下/步入式衣帽间/卧室落地窗，不出现镜子
    # 动作：转圈/蹦跳/踮脚/伸懒腰/侧身回头/背影回头
    "social_seed": {
        "front_full_body": [
            {"label": "试衣间软凳旁刚跨步停下咧嘴笑", "framing": "product_first"},
            {"label": "卧室衣柜前蹦跳一下咧嘴一笑", "framing": "product_first"},
            {"label": "化妆间灯光下踮脚伸手够望那一刻", "framing": "product_first"},
        ],
        "natural_pose": [
            {"label": "卧室落地灯旁转圈让裙摆张开瞬间", "framing": "product_first"},
            {"label": "试衣间软凳前肩膀放松仰头大笑", "framing": "product_first"},
            {"label": "步入式衣帽间小身影刚走到位停下", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "试衣间灯光下半身正在轻扶领口", "framing": "product_first"},
            {"label": "卧室衣柜前胸口特写小手轻碰前襟抿嘴笑", "framing": "product_first"},
            {"label": "化妆间半身小手摸袖口的动作中", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "试衣间软凳旁侧身回头那一刻咧嘴笑", "framing": "product_first"},
            {"label": "卧室衣柜前背影正要回头，单手轻撩裙摆", "framing": "product_first"},
            {"label": "卧室落地窗前背影刚走到位停下", "framing": "tone_first"},
        ],
    },
}


TODDLER_POOL: ShotPool = {
    # ───────────── 白底主图（white_ecommerce）─────────────
    # 配比 8P / 0T；动作：站/双手举起/坐小凳/趴着抬头/张开双臂
    "white_ecommerce": {
        "front_full_body": [
            {"label": "白底正面刚跨步停下咧嘴大笑", "framing": "product_first"},
            {"label": "白底正面双手举起伸懒腰那一刻", "framing": "product_first"},
        ],
        "natural_pose": [
            {"label": "白底前坐小凳子双手放膝摇晃身子", "framing": "product_first"},
            {"label": "白底前坐着仰头看镜头大笑那一刻", "framing": "product_first"},
        ],
        "detail_half_body": [
            {"label": "白底半身小手扶领口的瞬间", "framing": "product_first"},
            {"label": "白底胸口特写小手轻碰衣角抿嘴笑", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "白底侧身回头那一刻咧嘴笑", "framing": "product_first"},
            {"label": "白底背面坐小凳正要回头看", "framing": "product_first"},
        ],
    },
    # ───────────── 高级棚拍幼儿版（premium_studio）─────────────
    # 配比 5P / 3T；动作：站/仰头大笑/坐高凳摇晃/坐摇椅/侧身回头大笑
    "premium_studio": {
        "front_full_body": [
            {"label": "棚拍正面刚跨步停下仰头看镜头大笑", "framing": "product_first"},
            {"label": "棚拍灰底前小身影刚坐下远景", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "棚拍小高凳上坐着摇晃身子", "framing": "product_first"},
            {"label": "棚拍冷光下小身影坐摇椅前后晃", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "棚拍半身小手扶领口的瞬间", "framing": "product_first"},
            {"label": "棚拍胸口特写小手轻拈裙摆抿嘴笑", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "棚拍侧身回头那一刻咧嘴大笑", "framing": "product_first"},
            {"label": "棚拍灰底背面小身影刚坐下", "framing": "tone_first"},
        ],
    },
    # ───────────── 质感街拍幼儿版（urban_commute）─────────────
    # 微场景：街边花坛/街角台阶/涂鸦墙/路边长椅
    # 动作：站/仰头看/坐台阶摇晃/蹲下/侧身回头大笑
    "urban_commute": {
        "front_full_body": [
            {"label": "街边花坛旁站立仰头看镜头大笑", "framing": "product_first"},
            {"label": "涂鸦墙前小身影刚坐下远景", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "街角台阶坐着双手放膝摇晃身子", "framing": "product_first"},
            {"label": "街边小身影刚蹲下看地", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "街拍半身小手扶领口的瞬间", "framing": "product_first"},
            {"label": "街边胸口特写小手轻碰外套衣角抿嘴笑", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "街角侧身回头那一刻咧嘴大笑", "framing": "product_first"},
            {"label": "街边背影小身影刚坐下台阶", "framing": "tone_first"},
        ],
    },
    # ───────────── 精品空间幼儿版（lifestyle）─────────────
    # 微场景：画廊白墙/酒店大堂软凳/精品店落地灯/木地板
    # 动作：站/坐高凳摇晃/蹲下/侧身回头
    "lifestyle": {
        "front_full_body": [
            {"label": "画廊白墙前刚跨步停下咧嘴笑", "framing": "product_first"},
            {"label": "酒店大堂前小身影刚坐下软凳", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "画廊前小高凳上坐着摇晃身子", "framing": "product_first"},
            {"label": "精品店落地灯旁小身影刚蹲下", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "画廊前半身小手扶领口的瞬间", "framing": "product_first"},
            {"label": "精品店胸口特写小手轻拈裙摆抿嘴笑", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "酒店大堂侧身回头那一刻咧嘴笑", "framing": "product_first"},
            {"label": "画廊背面小身影刚坐下木地板", "framing": "tone_first"},
        ],
    },
    # ───────────── 日常随拍幼儿版（daily_snapshot）─────────────
    # 微场景：客厅沙发/阳台/餐桌/客厅地毯/卧室飘窗
    # 动作：站/盘腿坐/趴着抬头/侧身回头大笑/坐看外面
    "daily_snapshot": {
        "front_full_body": [
            {"label": "客厅地毯上刚跨步停下仰头大笑", "framing": "product_first"},
            {"label": "阳台前小身影刚坐下小凳", "framing": "product_first"},
        ],
        "natural_pose": [
            {"label": "沙发上盘腿坐着摇晃身子咧嘴笑", "framing": "product_first"},
            {"label": "餐桌旁小身影趴着抬头看那一刻", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "家中半身小手扶领口的瞬间", "framing": "product_first"},
            {"label": "沙发前胸口特写小手轻碰衣角抿嘴笑", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "沙发上侧身回头那一刻咧嘴大笑", "framing": "product_first"},
            {"label": "阳台背影小身影刚坐下看外面", "framing": "tone_first"},
        ],
    },
    # ───────────── 自然手机摄影幼儿版（natural_phone_snapshot）─────────────
    # 微场景：儿童房地板/床边/儿童房地毯/客厅木地板
    # 动作：站/坐/趴着抬头/蹲坐
    "natural_phone_snapshot": {
        "front_full_body": [
            {"label": "儿童房地板上刚跨步停下咧嘴大笑", "framing": "product_first"},
            {"label": "床边小身影刚坐下双手放膝", "framing": "product_first"},
        ],
        "natural_pose": [
            {"label": "床上趴着抬头看镜头那一刻", "framing": "product_first"},
            {"label": "儿童房地毯上小身影刚蹲坐看地", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "儿童房半身小手扶领口的瞬间", "framing": "product_first"},
            {"label": "床边胸口特写小手轻碰睡衣角抿嘴笑", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "床边侧身回头那一刻咧嘴笑", "framing": "product_first"},
            {"label": "客厅木地板上背影小身影刚坐下", "framing": "tone_first"},
        ],
    },
    # ───────────── 自然种草幼儿版（social_seed）─────────────
    # 微场景：试衣间软凳/卧室衣柜前/卧室落地灯，不出现镜子
    # 动作：站/双手举起/坐凳摇晃/侧身回头
    "social_seed": {
        "front_full_body": [
            {"label": "试衣间软凳旁刚跨步停下咧嘴笑", "framing": "product_first"},
            {"label": "卧室衣柜前双手举起伸懒腰那一刻", "framing": "product_first"},
        ],
        "natural_pose": [
            {"label": "试衣间软凳上坐着摇晃身子双手放膝", "framing": "product_first"},
            {"label": "卧室落地灯旁小身影刚走到位停下", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "试衣间半身小手扶领口的瞬间", "framing": "product_first"},
            {"label": "卧室衣柜前胸口特写小手轻拈裙摆抿嘴笑", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "试衣间软凳旁侧身回头那一刻咧嘴笑", "framing": "product_first"},
            {"label": "卧室软凳前背影正要回头看", "framing": "product_first"},
        ],
    },
}


__all__ = ["CHILD_POOL", "TODDLER_POOL"]
