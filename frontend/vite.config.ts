import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端地址默认取本文件里写的端口，起服务时可以用 VITE_PROXY_TARGET 覆盖，
// 这样换端口调试或做启动探针时前端不用改代码。
const proxyTarget = process.env.VITE_PROXY_TARGET ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    // 关掉自动打开页面：起服务时只打印地址，不拉起浏览器
    open: false,
    strictPort: false,
    proxy: {
      // 用正则把匹配收窄到真正的 /api/ 接口：单纯前缀 '/api' 会误伤前端路由
      // '/apiclient'（它以 '/api' 四个字符开头），导致页面导航被代理到后端拿到 404。
      // '^/api(/|$)' 只匹配 /api、/api/... ，不再命中 /apiclient。
      '^/api(/|$)': {
        target: proxyTarget,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
