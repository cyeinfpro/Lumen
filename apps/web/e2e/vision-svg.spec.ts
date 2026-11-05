import { expect, test, type Page } from "@playwright/test";
import { installAgentFixture } from "./agent-fixture";
import { readFile } from "node:fs/promises";

const PNG = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAE0lEQVR4nGO8oyHHAANMcBZeDgA6ZgEqpR5TKwAAAABJRU5ErkJggg==", "base64");
const rawSource = '<svg xmlns="http://www.w3.org/2000/svg"><script>window.__svgExecuted=true</script></svg>';
const created = "2026-11-05T10:00:00Z";
const image = { image_id: "vision-svg", id: "vision-svg", mime: "image/svg+xml", actual_size: "64x64", width: 64, height: 64, url: "/api/images/vision-svg/binary", preview_url: "/api/images/vision-svg/variants/preview1024", display_url: "/api/images/vision-svg/variants/display2048" };

async function fixtures(page: Page) {
  await installAgentFixture(page);
  await page.route("**/api/images/*/variants/*", route => route.fulfill({ status: 200, contentType: "image/png", body: PNG }));
  await page.route("**/api/images/*/binary", route => route.fulfill({ status: 200, contentType: "image/svg+xml", headers: { "content-disposition": 'attachment; filename="verified.svg"' }, body: '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64"><rect width="64" height="64" fill="blue"/></svg>' }));
  await page.route("**/api/images/upload", route => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ id: "vision-reference", width: 4, height: 4, mime: "image/png", url: "/reference.png", preview_url: "/reference.png", display_url: "/reference.png" }) }));
  await page.route("**/reference.png", route => route.fulfill({ status: 200, contentType: "image/png", body: PNG }));
}

test("vision SVG requires explicit opt-in and keeps native settings and original draft", async ({page}, info) => {
  await fixtures(page);
  let submitted: Record<string, unknown> | null = null;
  let posts = 0;
  await page.route("**/api/conversations/*/messages**", async route => {
    if (route.request().method() !== "POST") return route.fallback();
    posts++; submitted = route.request().postDataJSON();
    await route.fulfill({ status: 422, contentType: "application/json", body: JSON.stringify({ detail: "fixture captured only" }) });
  });
  const history = page.waitForResponse(r => r.request().method() === "GET" && new URL(r.url()).pathname === "/api/conversations/studio-conversation-1/messages");
  await page.goto("/?conversationId=studio-conversation-1"); await history;
  if ((page.viewportSize()?.width ?? 0) >= 768) await page.getByRole("button", {name:"展开输入框"}).click();
  await page.getByRole("textbox", {name:"输入提示词"}).fill("Rebuild this shape as an editable SVG");
  await page.getByRole("tablist", {name:"模式",exact:true}).getByRole("tab",{name:"生图",exact:true}).click();
  await page.getByRole("button",{name:"执行设置",exact:true}).click();
  const dialog = page.getByRole("dialog",{name:/^(高级)?执行设置$/});
  await dialog.getByRole("combobox",{name:"输出格式",exact:true}).selectOption("svg");
  const button = dialog.getByRole("button",{name:"用视觉模型重建 SVG（文本计费）",exact:true});
  await expect(button).toBeDisabled();
  await expect(dialog).toContainText("不使用上方 SVG 作者 ID");
  await expect(dialog).toContainText("16,000 tokens");
  await page.keyboard.press("Escape");
  await page.locator('input[type="file"][accept="image/*"]').last().setInputFiles({name:"reference.png",mimeType:"image/png",buffer:PNG});
  await page.getByRole("button",{name:"执行设置",exact:true}).click();
  await expect(button).toBeEnabled();
  await dialog.getByRole("textbox",{name:"SVG 作者/绘制模型 ID",exact:true}).fill("native-only-author");
  await button.click();
  await expect.poll(()=>posts).toBe(1);
  expect(submitted).toMatchObject({intent:"vision_qa",attachment_image_ids:["vision-reference"],chat_params:{artifact_format:"svg",fast:false,max_output_tokens:16000}});
  expect(submitted).not.toHaveProperty("image_params");
  await expect(dialog.getByRole("alert")).toContainText("fixture captured");
  await expect(dialog.getByRole("textbox",{name:"SVG 作者/绘制模型 ID",exact:true})).toHaveValue("native-only-author");
  await page.screenshot({path:info.outputPath("vision-svg-explicit.png")});
  await page.keyboard.press("Escape");
  await expect(page.getByRole("textbox",{name:"输入提示词"})).toHaveValue("Rebuild this shape as an editable SVG");
});

for (const initialStatus of ["ready","invalid","processing_failed"] as const) {
  test(`vision SVG ${initialStatus} is honest, escaped and locally recoverable`,async ({page},info)=>{
    await fixtures(page);
    let status: string = initialStatus;
    let repairs=0, modelPosts=0, completionGets=0;
    function content() { return { text: status==="ready"?"已交付已验证的 SVG":"SVG 本地处理结果", completion_id:"vision-completion", artifact_format:"svg", artifact_status:status, artifact_image_id:status==="ready"?"vision-svg":null, artifact_error:status==="ready"?null:"fixture safety or processing result", artifact_source_text:rawSource, images:status==="ready"?[image]:[] }; }
    await page.route("**/api/conversations/*/messages**", async route=>{
      if(route.request().method()!=="GET"){modelPosts++;return route.fulfill({status:500,body:"unexpected model request"});}
      return route.fulfill({status:200,contentType:"application/json",body:JSON.stringify({items:[
        {id:"vision-user",role:"user",intent:"vision_qa",conversation_id:"studio-conversation-1",created_at:"2026-11-05T09:59:59Z",content:{text:"Rebuild this",intent:"vision_qa",attachment_image_ids:[]}},
        {id:"vision-assistant",role:"assistant",conversation_id:"studio-conversation-1",created_at:created,parent_message_id:"vision-user",intent:"vision_qa",status:"succeeded",content:content()},
      ],completions:[{id:"vision-completion",message_id:"vision-assistant",status:"succeeded",...content()}],images:status==="ready"?[image]:[],generations:[],next_cursor:null})});
    });
    await page.route("**/api/completions/vision-completion",route=>{completionGets++;return route.fulfill({status:200,contentType:"application/json",body:JSON.stringify({id:"vision-completion",status:"succeeded",...content()})});});
    await page.route("**/api/completions/vision-completion/artifact/retry",async route=>{
      repairs++;status=repairs === 1 ? "processing_failed" : "ready";
      await route.fulfill({status:200,contentType:"application/json",body:JSON.stringify({status:"succeeded",artifact_status:"pending"})});
    });
    await page.goto("/?conversationId=studio-conversation-1");
    const card=page.getByRole("region",{name:"视觉 SVG 结果"});
    await expect(card).toBeVisible();
    await card.getByText("查看模型原始文本（仅文本，不执行）",{exact:true}).click();
    await expect(card.locator("pre")).toHaveText(rawSource);
    expect(await card.evaluate(element => element.scrollWidth <= element.clientWidth + 1)).toBe(true);
    expect(await card.locator("pre").evaluate(element => element.getBoundingClientRect().right <= element.parentElement!.getBoundingClientRect().right + 1)).toBe(true);
    expect(await page.evaluate(()=>("__svgExecuted" in window))).toBe(false);
    await expect(card.locator("svg,script")).toHaveCount(0);
    await page.screenshot({path:info.outputPath(`vision-svg-${initialStatus}-initial.png`)});
    if(initialStatus==="ready") {
      await expect(card.getByRole("button",{name:"下载已验证 SVG"})).toBeEnabled();
      const downloadReady=page.waitForEvent("download");
      await card.getByRole("button",{name:"下载已验证 SVG"}).click();
      const download=await downloadReady;
      const target=info.outputPath("verified.svg");await download.saveAs(target);
      expect(await readFile(target,"utf8")).toContain("<rect");
    }
    else await expect(card.getByRole("button",{name:"下载已验证 SVG"})).toHaveCount(0);
    if(initialStatus==="invalid") {
      await expect(card).toContainText("未通过安全或格式校验");
      await expect(card.getByRole("button",{name:"仅重试本地交付（不重新生成）"})).toHaveCount(0);
    }
    if(initialStatus==="processing_failed"){
      await expect(card).toContainText("本地处理失败");
      await card.getByRole("button",{name:"仅重试本地交付（不重新生成）"}).click();
      await expect.poll(()=>repairs).toBe(1);
      await expect(card).toContainText("已提交本地处理");
      const readsBefore = completionGets;
      await card.getByRole("button",{name:"刷新结果",exact:true}).click();
      await expect.poll(()=>completionGets).toBeGreaterThan(readsBefore);
      await expect(card).toContainText("本地处理失败");
      const retry = card.getByRole("button",{name:"仅重试本地交付（不重新生成）"});
      await expect(retry).toBeEnabled();
      await page.screenshot({path:info.outputPath("vision-svg-local-retry-still-failed.png")});
      await retry.click();
      await expect.poll(()=>repairs).toBe(2);
      const readsBeforeSecond = completionGets;
      await card.getByRole("button",{name:"刷新结果",exact:true}).click();
      await expect.poll(()=>completionGets).toBeGreaterThan(readsBeforeSecond);
      await expect(card).toContainText("SVG 已交付");
      await expect(card.getByRole("button",{name:"下载已验证 SVG"})).toBeVisible();
    }
    expect(modelPosts).toBe(0);
    await page.screenshot({path:info.outputPath(`vision-svg-${initialStatus}.png`)});
    await page.reload();
    await expect(page.getByRole("region",{name:"视觉 SVG 结果"})).toContainText(status==="ready"?"SVG 已交付":"未通过安全或格式校验");
  });
}

test("GIF result details show actual encoding and requested differences without rerunning", async ({page},info)=>{
  await fixtures(page);
  const gif=Buffer.from("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7","base64");
  let posts=0;
  await page.route("**/api/images/actual-gif/binary",route=>route.fulfill({status:200,contentType:"image/gif",body:gif}));
  await page.route("**/api/conversations/*/messages**",route=>{
    if(route.request().method()==="POST"){posts++;return route.fulfill({status:500,body:"unexpected"});}
    return route.fallback();
  });
  const historyReady = page.waitForResponse(r => r.request().method() === "GET" && new URL(r.url()).pathname === "/api/conversations/studio-conversation-1/messages");
  await page.goto("/?conversationId=studio-conversation-1");
  await historyReady;
  await page.evaluate(()=>window.dispatchEvent(new CustomEvent("lumen:open-lightbox",{detail:{
    items:[{id:"actual-gif",mime:"image/gif",url:"/api/images/actual-gif/binary",prompt:"GIF actual output fixture",width:512,height:512,
      metadata:{generation_diagnostics:{native_output:{output_format:"gif",result_count:1,requested:{size:"512x512",frame_count:9,frame_duration_ms:20},
        actual:[{width:512,height:512,frame_count:4,durations_ms:[40,40,40,40],total_duration_ms:160,loop:0}],mismatches:["frame_count","frame_duration_ms"]}}}}],
    initialId:"actual-gif",
  }})));
  if((page.viewportSize()?.width??0)<768){
    await page.getByRole("button",{name:"更多操作",exact:true}).last().click();
    await page.getByRole("menuitem",{name:"图片信息",exact:true}).click();
  }else await page.getByRole("button",{name:/图片信息/}).click();
  const fps=page.getByText("25 FPS（40 ms/帧；不保证播放器帧率）",{exact:true});
  await fps.scrollIntoViewIfNeeded();await expect(fps).toBeVisible();
  await expect(page.getByText("0.16 秒",{exact:true})).toBeVisible();
  const mismatch=page.getByText("帧数、每帧时长；以实际文件为准，不会自动重新生成",{exact:true});
  await mismatch.scrollIntoViewIfNeeded();await expect(mismatch).toBeVisible();
  await page.screenshot({path:info.outputPath("gif-actual-metadata.png")});
  expect(posts).toBe(0);
});
