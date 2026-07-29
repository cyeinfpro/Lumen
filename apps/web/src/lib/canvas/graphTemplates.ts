import {
  CANVAS_NODE_SPECS,
  createCanvasNode,
} from "#canvas-registry";
import type {
  CanvasDataType,
  CanvasEdgeDefinition,
  CanvasGraph,
} from "#canvas-types";

export function createEmptyCanvasGraph(): CanvasGraph {
  return {
    schema_version: 1,
    nodes: [],
    edges: [],
    frames: [],
    settings: { snap_to_grid: false, grid_size: 16 },
  };
}

export function createDefaultCanvasGraph(): CanvasGraph {
  const prompt = createCanvasNode("prompt", { x: 80, y: 160 }, {
    id: "prompt-1",
    title: "创作提示词",
    config: { text: "", locked: false },
  });
  const image = createCanvasNode("image_generate", { x: 430, y: 130 }, {
    id: "image-generate-1",
  });
  return {
    ...createEmptyCanvasGraph(),
    nodes: [prompt, image],
    edges: [
      {
        id: "edge-prompt-image",
        source_node_id: prompt.id,
        source_handle: "text",
        target_node_id: image.id,
        target_handle: "prompt",
        data_type: "text",
        binding_mode: "follow_active",
        order: 0,
      },
    ],
  };
}

export function createCanvasTemplateGraph(template: string): CanvasGraph {
  switch (template) {
    case "image_to_video":
      return createImageToVideoTemplate();
    case "product_directions":
      return createBranchingImageTemplate(true);
    case "multi_ratio":
      return createBranchingImageTemplate(false);
    case "storyboard_video":
      return createStoryboardVideoTemplate();
    case "image_editing":
      return createImageEditingTemplate();
    case "inpaint":
      return createInpaintTemplate();
    case "reference_video":
      return createReferenceVideoTemplate();
    case "creative_campaign":
      return createCreativeCampaignTemplate();
    default:
      return createDefaultCanvasGraph();
  }
}

function createImageToVideoTemplate(): CanvasGraph {
  const graph = createDefaultCanvasGraph();
  const video = createCanvasNode(
    "video_image_generate",
    { x: 790, y: 120 },
    { id: "video-generate-1", title: "首帧视频" },
  );
  const delivery = createCanvasNode("delivery", { x: 1160, y: 140 }, {
    id: "delivery-1",
  });
  graph.nodes.push(video, delivery);
  graph.edges.push(
    templateEdge(
      "edge-prompt-video",
      "prompt-1",
      "text",
      video.id,
      "prompt",
      "text",
    ),
    templateEdge(
      "edge-image-video",
      "image-generate-1",
      "image",
      video.id,
      "first_frame",
      "image",
    ),
    templateEdge(
      "edge-video-delivery",
      video.id,
      "video",
      delivery.id,
      "videos",
      "video",
    ),
  );
  return graph;
}

function createBranchingImageTemplate(productDirections: boolean): CanvasGraph {
  const graph = createDefaultCanvasGraph();
  const ratios = productDirections
    ? ["4:5", "16:9"]
    : ["4:5", "9:16", "16:9"];
  const firstImage = graph.nodes.find(
    (node) => node.id === "image-generate-1",
  );
  if (firstImage && productDirections) firstImage.title = "视觉方向 1";
  ratios.forEach((ratio, index) => {
    const node = createCanvasNode(
      "image_generate",
      { x: 430, y: 370 + index * 240 },
      {
        id: `image-generate-${index + 2}`,
        title: productDirections
          ? `视觉方向 ${index + 2}`
          : `${ratio} 图片生成`,
        config: {
          ...CANVAS_NODE_SPECS.image_generate.defaultConfig,
          aspect_ratio: ratio,
        },
      },
    );
    graph.nodes.push(node);
    graph.edges.push(
      templateEdge(
        `edge-prompt-image-${index + 2}`,
        "prompt-1",
        "text",
        node.id,
        "prompt",
        "text",
      ),
    );
  });
  if (!productDirections) return graph;

  const product = createCanvasNode("image_asset", { x: 80, y: 430 }, {
    id: "product-reference-1",
    title: "商品参考",
    config: { display_name: "商品参考" },
    ui: { preset_id: "product_reference" },
  });
  graph.nodes.push(product);
  for (const image of graph.nodes.filter(
    (node) => node.type === "image_generate",
  )) {
    graph.edges.push(
      templateEdge(
        `edge-product-${image.id}`,
        product.id,
        "image",
        image.id,
        "references",
        "image",
        "product",
      ),
    );
  }
  return graph;
}

function createStoryboardVideoTemplate(): CanvasGraph {
  const graph = createImageToVideoTemplate();
  graph.nodes.unshift(
    createCanvasNode(
      "frame",
      { x: 30, y: 60 },
      {
        id: "frame-1",
        title: "关键帧到视频",
        size: { width: 1420, height: 520 },
      },
    ),
  );
  return graph;
}

function createImageEditingTemplate(): CanvasGraph {
  const prompt = createCanvasNode("prompt", { x: 80, y: 110 }, {
    id: "edit-prompt-1",
    title: "编辑指令",
  });
  const source = createCanvasNode("image_asset", { x: 80, y: 390 }, {
    id: "edit-source-1",
    title: "待编辑原图",
    config: { display_name: "待编辑原图" },
  });
  const edit = createCanvasNode("image_edit", { x: 450, y: 170 }, {
    id: "image-edit-1",
  });
  const delivery = createCanvasNode("delivery", { x: 830, y: 190 }, {
    id: "delivery-1",
  });
  return {
    ...createEmptyCanvasGraph(),
    nodes: [prompt, source, edit, delivery],
    edges: [
      templateEdge(
        "edge-edit-prompt",
        prompt.id,
        "text",
        edit.id,
        "prompt",
        "text",
      ),
      templateEdge(
        "edge-edit-source",
        source.id,
        "image",
        edit.id,
        "source",
        "image",
        "edit_target",
      ),
      templateEdge(
        "edge-edit-delivery",
        edit.id,
        "image",
        delivery.id,
        "images",
        "image",
      ),
    ],
  };
}

function createInpaintTemplate(): CanvasGraph {
  const prompt = createCanvasNode("prompt", { x: 80, y: 70 }, {
    id: "inpaint-prompt-1",
    title: "重绘指令",
  });
  const source = createCanvasNode("image_asset", { x: 80, y: 320 }, {
    id: "inpaint-source-1",
    title: "待重绘原图",
    config: { display_name: "待重绘原图" },
  });
  const mask = createCanvasNode("mask_asset", { x: 80, y: 570 }, {
    id: "inpaint-mask-1",
    title: "重绘遮罩",
    config: { display_name: "重绘遮罩" },
  });
  const inpaint = createCanvasNode("image_inpaint", { x: 460, y: 220 }, {
    id: "image-inpaint-1",
  });
  const delivery = createCanvasNode("delivery", { x: 840, y: 250 }, {
    id: "delivery-1",
  });
  return {
    ...createEmptyCanvasGraph(),
    nodes: [prompt, source, mask, inpaint, delivery],
    edges: [
      templateEdge(
        "edge-inpaint-prompt",
        prompt.id,
        "text",
        inpaint.id,
        "prompt",
        "text",
      ),
      templateEdge(
        "edge-inpaint-source",
        source.id,
        "image",
        inpaint.id,
        "source",
        "image",
        "edit_target",
      ),
      templateEdge(
        "edge-inpaint-mask",
        mask.id,
        "mask",
        inpaint.id,
        "mask",
        "mask",
      ),
      templateEdge(
        "edge-inpaint-delivery",
        inpaint.id,
        "image",
        delivery.id,
        "images",
        "image",
      ),
    ],
  };
}

function createReferenceVideoTemplate(): CanvasGraph {
  const prompt = createCanvasNode("prompt", { x: 80, y: 70 }, {
    id: "reference-video-prompt-1",
    title: "视频提示词",
  });
  const image = createCanvasNode("image_asset", { x: 80, y: 320 }, {
    id: "reference-image-1",
    title: "人物或风格参考",
  });
  const video = createCanvasNode("video_asset", { x: 80, y: 570 }, {
    id: "reference-video-1",
    title: "动作参考",
  });
  const generate = createCanvasNode(
    "video_reference_generate",
    { x: 470, y: 220 },
    { id: "reference-video-generate-1" },
  );
  const delivery = createCanvasNode("delivery", { x: 860, y: 250 }, {
    id: "delivery-1",
  });
  return {
    ...createEmptyCanvasGraph(),
    nodes: [prompt, image, video, generate, delivery],
    edges: [
      templateEdge(
        "edge-reference-prompt",
        prompt.id,
        "text",
        generate.id,
        "prompt",
        "text",
      ),
      templateEdge(
        "edge-reference-image",
        image.id,
        "image",
        generate.id,
        "reference_images",
        "image",
        "subject",
      ),
      templateEdge(
        "edge-reference-video",
        video.id,
        "video",
        generate.id,
        "reference_videos",
        "video",
      ),
      templateEdge(
        "edge-reference-delivery",
        generate.id,
        "video",
        delivery.id,
        "videos",
        "video",
      ),
    ],
  };
}

function createCreativeCampaignTemplate(): CanvasGraph {
  const frame = createCanvasNode("frame", { x: 30, y: 30 }, {
    id: "campaign-frame-1",
    title: "营销创意流水线",
    size: { width: 1500, height: 780 },
  });
  const brand = createCanvasNode("prompt", { x: 80, y: 90 }, {
    id: "campaign-brand-1",
    title: "品牌与商品信息",
  });
  const scene = createCanvasNode("prompt", { x: 80, y: 330 }, {
    id: "campaign-scene-1",
    title: "场景与视觉风格",
  });
  const constraints = createCanvasNode("prompt", { x: 80, y: 570 }, {
    id: "campaign-constraints-1",
    title: "文案与限制条件",
  });
  const merge = createCanvasNode("prompt_merge", { x: 400, y: 250 }, {
    id: "campaign-prompt-merge-1",
    title: "完整创意提示词",
    config: { separator: "\n\n", trim: true, dedupe: true },
  });
  const product = createCanvasNode("image_asset", { x: 410, y: 570 }, {
    id: "campaign-product-1",
    title: "商品参考",
    config: { display_name: "商品参考" },
    ui: { preset_id: "product_reference" },
  });
  const image = createCanvasNode("image_generate", { x: 760, y: 150 }, {
    id: "campaign-image-1",
    title: "商品主视觉",
    config: { aspect_ratio: "4:5", quality: "4k", size: "4K", fast: false },
    ui: { preset_id: "product_key_visual" },
  });
  const video = createCanvasNode(
    "video_image_generate",
    { x: 760, y: 500 },
    {
      id: "campaign-video-1",
      title: "竖屏动态短片",
      config: { aspect_ratio: "9:16", duration_s: 5 },
    },
  );
  const delivery = createCanvasNode("delivery", { x: 1150, y: 300 }, {
    id: "campaign-delivery-1",
  });
  return {
    ...createEmptyCanvasGraph(),
    nodes: [
      frame,
      brand,
      scene,
      constraints,
      merge,
      product,
      image,
      video,
      delivery,
    ],
    edges: [
      templateEdge(
        "edge-campaign-brand",
        brand.id,
        "text",
        merge.id,
        "texts",
        "text",
        null,
        0,
      ),
      templateEdge(
        "edge-campaign-scene",
        scene.id,
        "text",
        merge.id,
        "texts",
        "text",
        null,
        1,
      ),
      templateEdge(
        "edge-campaign-constraints",
        constraints.id,
        "text",
        merge.id,
        "texts",
        "text",
        null,
        2,
      ),
      templateEdge(
        "edge-campaign-image-prompt",
        merge.id,
        "text",
        image.id,
        "prompt",
        "text",
      ),
      templateEdge(
        "edge-campaign-product",
        product.id,
        "image",
        image.id,
        "references",
        "image",
        "product",
      ),
      templateEdge(
        "edge-campaign-video-prompt",
        merge.id,
        "text",
        video.id,
        "prompt",
        "text",
      ),
      templateEdge(
        "edge-campaign-first-frame",
        image.id,
        "image",
        video.id,
        "first_frame",
        "image",
      ),
      templateEdge(
        "edge-campaign-image-delivery",
        image.id,
        "image",
        delivery.id,
        "images",
        "image",
      ),
      templateEdge(
        "edge-campaign-video-delivery",
        video.id,
        "video",
        delivery.id,
        "videos",
        "video",
      ),
    ],
  };
}

function templateEdge(
  id: string,
  sourceNodeId: string,
  sourceHandle: string,
  targetNodeId: string,
  targetHandle: string,
  dataType: CanvasDataType,
  role: CanvasEdgeDefinition["role"] = null,
  order = 0,
): CanvasEdgeDefinition {
  return {
    id,
    source_node_id: sourceNodeId,
    source_handle: sourceHandle,
    target_node_id: targetNodeId,
    target_handle: targetHandle,
    data_type: dataType,
    binding_mode: "follow_active",
    role,
    order,
  };
}
