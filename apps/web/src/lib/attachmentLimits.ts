// 条数上限与单图上传上限独立；提高这里会线性放大最坏总上传体积。
// 调整前需要同时确认网关、API 上传、Worker 拉取和存储写入的容量。
export const MAX_COMPOSER_ATTACHMENTS = 16;
