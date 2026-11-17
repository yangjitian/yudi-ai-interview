# Career智面 前端

Career智面的 Web 端：React 18 + TypeScript + Vite 5，包含简历与知识库管理、模拟面试、实时语音面试页和评估报告。

## 本地开发

```bash
npm install
npm run dev        # 默认 http://localhost:5173
```

开发服务器把 `/api` 代理到后端，并且 `ws: true`——语音面试的 WebSocket 走同一条代理。代理目标取环境变量 `VITE_API_BASE_URL`，默认 `http://localhost:8000`；开发端口取 `VITE_PORT`，默认 5173。所以后端按默认 8000 端口启动即可，不需要额外配置。

## 构建

```bash
npm run build      # tsc 类型检查 + vite build，产物在 dist/
npm run preview    # 本地预览构建产物
```

构建产物按依赖拆分了 chunk（React、UI 组件、语法高亮各一组），避免单个包过大。

## 关于包管理器

仓库里同时存在 `package-lock.json` 与 `pnpm-lock.yaml`，而 `Dockerfile` 的构建阶段固定使用 **pnpm**。本地用哪个都能跑，但**改完依赖后请同步更新 `pnpm-lock.yaml`**，否则容器构建会退回旧版本。

## 语音相关说明

- 麦克风采集用 AudioWorklet，把 PCM 16k 单声道按帧编码后经 WebSocket 发送。
- 回答提交由按钮触发，不是由前端 VAD 自动断句。前端会加载 VAD 模型实例，但其事件回调目前不参与业务判断，断句以服务端 VAD 为准。
- VAD 的脚本与 WASM 从 CDN 加载；加载失败不影响录音与提交，因为业务链路不依赖前端 VAD。
