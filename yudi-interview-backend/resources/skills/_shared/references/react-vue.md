# React/Vue面试重点（框架核心+工程化）

## React核心机制
- Fiber架构：链表结构替代递归、可中断渲染、时间切片（Time Slicing）与优先级调度。
- Virtual DOM：Diff算法（同层比较、key的作用）、Reconciliation过程。
- Hooks规则：调用顺序一致（不能在条件/循环中调用）、闭包陷阱（stale closure）与解决方案。
- 常用Hooks：`useState`/`useEffect`（依赖数组）/`useCallback`/`useMemo`/`useRef`/`useContext`。
- React 18并发特性：`useTransition`（非紧急更新降级）、`useDeferredValue`（延迟重渲染）、Suspense数据获取。

## Vue核心机制
- 响应式原理：Vue 2（`Object.defineProperty`拦截getter/setter）vs Vue 3（`Proxy`代理全对象）。
- Composition API：`ref`/`reactive`/`computed`/`watch`/`watchEffect`，逻辑组合 vs Options API逻辑分散。
- 模板编译：模板→AST→渲染函数→Virtual DOM，静态提升与补丁标记（Patch Flag）。
- 组件更新粒度：Vue 3基于依赖追踪的精确更新 vs React整体重渲染（需memo/shouldComponentUpdate）。

## 状态管理
- React：Context API（轻量）、Redux（单向数据流/reducer/middleware）、Zustand（极简Hook式）、Jotai/Recoil（原子化）。
- Vue：Pinia（Vue 3官方推荐，TS友好、模块化）、Vuex（Vue 2时代，mutation/action分离）。

## 组件设计
- 组件通信：props/emit、provide/inject（跨层级）、事件总线、状态管理。
- 性能优化：`React.memo`/`useMemo`/`useCallback`、Vue `v-once`/`v-memo`、虚拟列表（react-window/vue-virtual-scroller）。

## 路由与SSR
- React Router：声明式路由、嵌套路由、路由守卫、懒加载（`React.lazy` + `Suspense`）。
- SSR vs CSR vs SSG：SEO、首屏速度、服务器成本权衡。

## 工程化
- 构建工具：Vite（ESM开发服务器 + Rollup构建）vs Webpack（loader/plugin生态）。
- 代码规范：ESLint + Prettier、Husky + lint-staged（提交前检查）。
- 测试：Jest/Vitest（单元测试）、React Testing Library/Vue Test Utils（组件测试）、Playwright/Cypress（E2E）。
