import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import wasm from 'vite-plugin-wasm';
import topLevelAwait from 'vite-plugin-top-level-await';
// https://vitejs.dev/config/
export default defineConfig(function (_a) {
    var mode = _a.mode;
    // 加载环境变量
    var env = loadEnv(mode, process.cwd(), '');
    // 从环境变量或默认值获取 API 地址
    var apiBaseUrl = env.VITE_API_BASE_URL || 'http://localhost:8000';
    return {
        plugins: [
            wasm(),
            topLevelAwait(),
            react(),
        ],
        build: {
            rollupOptions: {
                output: {
                    manualChunks: {
                        'react-vendor': ['react', 'react-dom', 'react-router-dom'],
                        'ui-vendor': ['framer-motion', 'lucide-react'],
                        'syntax-highlighter': ['react-syntax-highlighter'],
                    },
                },
            },
        },
        server: {
            host: '0.0.0.0',
            port: parseInt(env.VITE_PORT || '5173', 10),
            proxy: {
                '/api': {
                    target: apiBaseUrl,
                    changeOrigin: true,
                    ws: true,
                    configure: function (proxy) {
                        proxy.on('proxyReq', function (proxyReq) {
                            if (proxyReq.path.includes('/ws/')) {
                                // 提取目标主机用于 WebSocket 升级
                                var targetHost = apiBaseUrl.replace(/^https?:\/\//, '');
                                proxyReq.setHeader('origin', "http://".concat(targetHost));
                            }
                        });
                    },
                },
            },
            // 忽略 @ricky0123/vad-web 的 sourcemap 警告
            sourcemapIgnoreList: function (relativeSourcePath) {
                return relativeSourcePath.includes('node_modules/.pnpm/@ricky0123+vad-web');
            },
        },
        optimizeDeps: {
        // No need to optimize vad-web since we load it via script tag
        },
    };
});
