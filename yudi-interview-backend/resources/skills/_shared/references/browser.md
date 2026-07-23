# 浏览器原理面试重点（渲染+缓存+安全+性能）

## 渲染流水线
- 关键渲染路径：HTML→DOM树、CSS→CSSOM树、合并→Render树→布局→绘制→合成。
- DOMContentLoaded vs load：DOM解析完成 vs 所有资源加载完成，CSS阻塞DOM渲染但不阻塞解析。
- `<script>`阻塞：默认阻塞DOM解析，`async`（下载完立即执行）vs`defer`（DOM解析后执行）。
- 合成层优化：`transform`/`opacity`触发GPU合成，跳过布局与绘制。

## 浏览器缓存
- 强缓存：`Cache-Control`（max-age/no-cache/no-store）优先于`Expires`。
- 协商缓存：`ETag`/`If-None-Match` vs `Last-Modified`/`If-Modified-Since`，304响应。
- 缓存位置优先级：Service Worker → Memory Cache → Disk Cache → Push Cache。
- CDN缓存策略：回源机制、缓存命中率、预热与刷新。

## 网络与协议
- HTTP/1.1：持久连接（`Connection: keep-alive`）、管线化缺陷。
- HTTP/2：多路复用、头部压缩（HPACK）、服务器推送、二进制分帧。
- HTTP/3（QUIC）：基于UDP、0-RTT连接、解决队头阻塞。
- HTTPS：TLS握手流程（证书校验→密钥协商→对称加密通信），会话复用。

## 安全
- XSS：存储型/反射型/DOM型，防御（转义、CSP、HttpOnly Cookie）。
- CSRF：伪造请求利用已认证身份，防御（SameSite Cookie、CSRF Token、Referer校验）。
- CORS：简单请求 vs 预检请求（OPTIONS），`Access-Control-Allow-Origin`等响应头。
- CSP（Content-Security-Policy）：白名单限制资源加载，`nonce`/`hash`策略。

## 性能监控与优化
- Core Web Vitals：LCP（最大内容绘制≤2.5s）、FID/INP（交互延迟≤100ms）、CLS（布局偏移≤0.1）。
- 性能API：`PerformanceObserver`、`performance.timing`（导航计时）、`PerformanceResourceTiming`。
- 优化手段：代码分割、图片懒加载、预加载（`<link rel="preload">`）、骨架屏。
- 内存泄漏排查：Chrome DevTools Memory面板，堆快照对比、分配时间线。
