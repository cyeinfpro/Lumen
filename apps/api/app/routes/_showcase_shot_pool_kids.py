"""Child and toddler shot pools for showcase generation.

CHILD_POOL  → 7 模板 × 4 类机位 × 3 变体 = 84 条
TODDLER_POOL → 7 模板 × 4 类机位 × 2 变体 = 56 条
"""

from __future__ import annotations

from app.routes._showcase_shot_pool import ShotPool, ShotVariant


CHILD_POOL: ShotPool = {
    # ───────────── 白底主图（white_ecommerce）─────────────
    # 配比：12P / 0T，紧凑构图，白底为主
    "white_ecommerce": {
        "front_full_body": [
            {"label": "正面全身白底站立咧嘴笑", "framing": "product_first"},
            {"label": "正面全身双手叉腰咧嘴一笑", "framing": "product_first"},
            {"label": "正面全身踮脚双手举起", "framing": "product_first"},
        ],
        "natural_pose": [
            {"label": "白底前转半圈让衣摆张开", "framing": "product_first"},
            {"label": "白底前单脚跳起小幅动作", "framing": "product_first"},
            {"label": "白底前抱毛绒玩偶站立", "framing": "product_first"},
        ],
        "detail_half_body": [
            {"label": "白底半身特写小手扶领口", "framing": "product_first"},
            {"label": "白底胸口特写小手扯一下衣角", "framing": "product_first"},
            {"label": "白底半身小手摸袖口口袋", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "白底侧身小手放胸前回头", "framing": "product_first"},
            {"label": "白底背面回头咧嘴一笑", "framing": "product_first"},
            {"label": "白底45度侧身展示后背", "framing": "product_first"},
        ],
    },
    # ───────────── 高级棚拍童版（premium_studio）─────────────
    # 配比：7P / 5T，端正大片但不戏剧化
    "premium_studio": {
        "front_full_body": [
            {"label": "棚拍正面全身端正站姿咧嘴大笑", "framing": "product_first"},
            {"label": "棚拍正面全身双手捧脸看镜头", "framing": "product_first"},
            {"label": "棚拍灰墙前小身影端正站立", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "棚拍前原地转圈让裙摆张开", "framing": "product_first"},
            {"label": "棚拍冷光下小手扶发卡咧嘴笑", "framing": "tone_first"},
            {"label": "棚拍黑底前小身影抱毛绒站立", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "棚拍半身小手轻扶领口", "framing": "product_first"},
            {"label": "棚拍胸口特写小手扶蝴蝶结", "framing": "product_first"},
            {"label": "棚拍半身小手摸袖口扣子", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "棚拍侧身回头咧嘴一笑", "framing": "product_first"},
            {"label": "棚拍冷光下背影小身影站立", "framing": "tone_first"},
            {"label": "棚拍灰底背面小手垂在身侧", "framing": "tone_first"},
        ],
    },
    # ───────────── 质感街拍童版（urban_commute）─────────────
    # 配比：7P / 5T，街景背景 + 童模动作
    "urban_commute": {
        "front_full_body": [
            {"label": "街边花坛旁站立咧嘴一笑", "framing": "product_first"},
            {"label": "街角台阶上踮脚双手举起", "framing": "product_first"},
            {"label": "涂鸦墙前小身影手拿气球站立", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "涂鸦墙前转圈让衣摆张开", "framing": "product_first"},
            {"label": "街角马赛克地砖单脚跳过", "framing": "tone_first"},
            {"label": "街边长椅旁小身影捡起一片叶子", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "街拍半身小手扶肩带", "framing": "product_first"},
            {"label": "街边胸口特写小手扯外套衣角", "framing": "product_first"},
            {"label": "街角半身小手摸口袋鼓鼓的", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "街边花坛旁侧身回头咧嘴笑", "framing": "product_first"},
            {"label": "街角小巷背影抱小皮包走开", "framing": "tone_first"},
            {"label": "街边台阶坐着背影手撑在膝盖", "framing": "tone_first"},
        ],
    },
    # ───────────── 精品空间童版（lifestyle）─────────────
    # 配比：7P / 5T，画廊/儿童精品店/酒店大堂留白
    "lifestyle": {
        "front_full_body": [
            {"label": "画廊白墙前端正站立咧嘴笑", "framing": "product_first"},
            {"label": "酒店大堂落地窗前抱毛绒站立", "framing": "product_first"},
            {"label": "儿童精品店内小身影站在木地板上", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "画廊白墙前转圈让裙摆张开", "framing": "product_first"},
            {"label": "酒店大堂矮凳旁踮脚扶发卡", "framing": "tone_first"},
            {"label": "精品店落地灯旁小身影抱小书站立", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "画廊前半身小手轻扶领口", "framing": "product_first"},
            {"label": "精品店半身小手扶蝴蝶结", "framing": "product_first"},
            {"label": "酒店大堂胸口特写小手摸口袋", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "画廊侧身回头咧嘴一笑", "framing": "product_first"},
            {"label": "酒店大堂背影抱毛绒走向远处", "framing": "tone_first"},
            {"label": "精品店木地板上小身影背对镜头", "framing": "tone_first"},
        ],
    },
    # ───────────── 日常随拍童版（daily_snapshot）─────────────
    # 配比：8P / 4T，家中朋友视角
    "daily_snapshot": {
        "front_full_body": [
            {"label": "客厅沙发前站立咧嘴一笑", "framing": "product_first"},
            {"label": "阳台窗前抱毛绒玩偶踮脚", "framing": "product_first"},
            {"label": "餐桌旁小身影手拿小零食站立", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "沙发上盘腿坐着仰头大笑", "framing": "product_first"},
            {"label": "地毯上单脚跳起小动作", "framing": "product_first"},
            {"label": "阳台前小身影低头玩积木", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "家中半身小手扶领口咧嘴笑", "framing": "product_first"},
            {"label": "沙发前胸口特写小手扯衣角", "framing": "product_first"},
            {"label": "阳台半身小手摸袖口口袋", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "沙发上侧身回头咧嘴一笑", "framing": "product_first"},
            {"label": "阳台背影站在窗前看外面", "framing": "tone_first"},
            {"label": "餐桌旁背影抱小书坐着", "framing": "tone_first"},
        ],
    },
    # ───────────── 自然手机摄影童版（natural_phone_snapshot）─────────────
    # 配比：8P / 4T，真实手机随手拍，儿童房/木质客厅
    "natural_phone_snapshot": {
        "front_full_body": [
            {"label": "儿童房床边站立抱毛绒玩偶", "framing": "product_first"},
            {"label": "木质客厅地板上踮脚双手举起", "framing": "product_first"},
            {"label": "儿童房落地窗前小身影手拿小花", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "床上盘腿坐抱毛绒咧嘴笑", "framing": "product_first"},
            {"label": "客厅地毯上单脚跳起小动作", "framing": "product_first"},
            {"label": "儿童房书架旁小身影低头看绘本", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "儿童房半身小手扶领口", "framing": "product_first"},
            {"label": "床边胸口特写小手扯衣角", "framing": "product_first"},
            {"label": "客厅半身小手摸袖口口袋", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "床边侧身回头咧嘴一笑", "framing": "product_first"},
            {"label": "儿童房背影抱毛绒站在窗前", "framing": "tone_first"},
            {"label": "客厅木地板上背影小身影坐着", "framing": "tone_first"},
        ],
    },
    # ───────────── 自然种草童版（social_seed）─────────────
    # 配比：10P / 2T，试衣镜前展示
    "social_seed": {
        "front_full_body": [
            {"label": "试衣镜前站立咧嘴对镜笑", "framing": "product_first"},
            {"label": "试衣镜前双手叉腰咧嘴一笑", "framing": "product_first"},
            {"label": "试衣镜前踮脚扶蝴蝶结", "framing": "product_first"},
        ],
        "natural_pose": [
            {"label": "镜前转圈让裙摆张开", "framing": "product_first"},
            {"label": "镜前小手比心咧嘴大笑", "framing": "product_first"},
            {"label": "试衣间软凳前小身影抱毛绒", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "镜前半身小手扶领口", "framing": "product_first"},
            {"label": "镜前胸口特写小手扶蝴蝶结", "framing": "product_first"},
            {"label": "镜前半身小手摸袖口扣子", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "镜前侧身回头咧嘴笑", "framing": "product_first"},
            {"label": "镜前背面回头扯一下裙摆", "framing": "product_first"},
            {"label": "试衣间软凳前背影抱小皮包", "framing": "tone_first"},
        ],
    },
}


TODDLER_POOL: ShotPool = {
    # ───────────── 白底主图（white_ecommerce）─────────────
    # 配比：8P / 0T
    "white_ecommerce": {
        "front_full_body": [
            {"label": "白底正面站立咧嘴大笑", "framing": "product_first"},
            {"label": "白底正面双手举起拍手", "framing": "product_first"},
        ],
        "natural_pose": [
            {"label": "白底前坐小凳子抱毛绒玩具", "framing": "product_first"},
            {"label": "白底前坐着双手举起仰头看镜头", "framing": "product_first"},
        ],
        "detail_half_body": [
            {"label": "白底半身小手摸帽顶", "framing": "product_first"},
            {"label": "白底胸口特写小手扯衣角", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "白底侧身回头咧嘴笑", "framing": "product_first"},
            {"label": "白底背面坐小凳回头看", "framing": "product_first"},
        ],
    },
    # ───────────── 高级棚拍幼儿版（premium_studio）─────────────
    # 配比：5P / 3T，端正不戏剧化
    "premium_studio": {
        "front_full_body": [
            {"label": "棚拍正面端正站立仰头看镜头", "framing": "product_first"},
            {"label": "棚拍灰底前小身影抱毛绒坐着", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "棚拍小高凳上坐着拍手", "framing": "product_first"},
            {"label": "棚拍冷光下小身影坐摇椅抱小书", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "棚拍半身小手扶帽檐", "framing": "product_first"},
            {"label": "棚拍胸口特写小手抓住裙摆", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "棚拍侧身回头咧嘴大笑", "framing": "product_first"},
            {"label": "棚拍灰底背面小身影坐着", "framing": "tone_first"},
        ],
    },
    # ───────────── 质感街拍幼儿版（urban_commute）─────────────
    # 配比：5P / 3T
    "urban_commute": {
        "front_full_body": [
            {"label": "街边花坛旁站立仰头看镜头", "framing": "product_first"},
            {"label": "涂鸦墙前小身影坐着拍手", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "街角台阶坐着抱毛绒玩偶", "framing": "product_first"},
            {"label": "街边小身影蹲下捡起一朵小花", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "街拍半身小手摸帽檐", "framing": "product_first"},
            {"label": "街边胸口特写小手扯外套", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "街角侧身回头咧嘴大笑", "framing": "product_first"},
            {"label": "街边背影小身影坐台阶", "framing": "tone_first"},
        ],
    },
    # ───────────── 精品空间幼儿版（lifestyle）─────────────
    # 配比：5P / 3T
    "lifestyle": {
        "front_full_body": [
            {"label": "画廊白墙前端正站立咧嘴笑", "framing": "product_first"},
            {"label": "酒店大堂前小身影坐软凳抱毛绒", "framing": "tone_first"},
        ],
        "natural_pose": [
            {"label": "画廊前小高凳上坐着拍手", "framing": "product_first"},
            {"label": "精品店落地灯旁小身影蹲下看小东西", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "画廊前半身小手扶帽檐", "framing": "product_first"},
            {"label": "精品店胸口特写小手抓住裙摆", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "酒店大堂侧身回头咧嘴笑", "framing": "product_first"},
            {"label": "画廊背面小身影坐木地板", "framing": "tone_first"},
        ],
    },
    # ───────────── 日常随拍幼儿版（daily_snapshot）─────────────
    # 配比：6P / 2T
    "daily_snapshot": {
        "front_full_body": [
            {"label": "客厅地毯上站立仰头大笑", "framing": "product_first"},
            {"label": "阳台前小身影坐小凳抱玩偶", "framing": "product_first"},
        ],
        "natural_pose": [
            {"label": "沙发上盘腿坐抱毛绒咧嘴笑", "framing": "product_first"},
            {"label": "餐桌旁小身影趴着抬头看", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "家中半身小手扶帽顶", "framing": "product_first"},
            {"label": "沙发前胸口特写小手扯衣角", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "沙发上侧身回头咧嘴大笑", "framing": "product_first"},
            {"label": "阳台背影小身影坐着看外面", "framing": "tone_first"},
        ],
    },
    # ───────────── 自然手机摄影幼儿版（natural_phone_snapshot）─────────────
    # 配比：6P / 2T
    "natural_phone_snapshot": {
        "front_full_body": [
            {"label": "儿童房地板上站立咧嘴大笑", "framing": "product_first"},
            {"label": "床边小身影坐着抱毛绒玩具", "framing": "product_first"},
        ],
        "natural_pose": [
            {"label": "床上趴着抬头看镜头", "framing": "product_first"},
            {"label": "儿童房地毯上小身影玩小积木", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "儿童房半身小手摸帽顶", "framing": "product_first"},
            {"label": "床边胸口特写小手扯睡衣角", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "床边侧身回头咧嘴笑", "framing": "product_first"},
            {"label": "客厅木地板上背影小身影坐着", "framing": "tone_first"},
        ],
    },
    # ───────────── 自然种草幼儿版（social_seed）─────────────
    # 配比：7P / 1T
    "social_seed": {
        "front_full_body": [
            {"label": "试衣镜前站立对镜咧嘴笑", "framing": "product_first"},
            {"label": "试衣镜前双手举起拍手", "framing": "product_first"},
        ],
        "natural_pose": [
            {"label": "镜前坐小凳抱毛绒玩偶", "framing": "product_first"},
            {"label": "试衣间软凳前小身影抓住裙摆", "framing": "tone_first"},
        ],
        "detail_half_body": [
            {"label": "镜前半身小手扶帽檐", "framing": "product_first"},
            {"label": "镜前胸口特写小手抓住裙摆", "framing": "product_first"},
        ],
        "side_or_back": [
            {"label": "镜前侧身回头咧嘴笑", "framing": "product_first"},
            {"label": "镜前背面坐小凳回头看", "framing": "product_first"},
        ],
    },
}


__all__ = ["CHILD_POOL", "TODDLER_POOL"]
