// 品牌与运行时配置（前缀统一 lasso-，替换参考前端的 ask-）
export const APP_CONFIG = {
  name: '拉锁 Lasso',
  shortName: '拉锁',
  subtitle: '企业级通用智能体平台',
  /** 开发态经 vite 代理到后端，生产可注入绝对地址 */
  apiBaseUrl: (import.meta.env.VITE_API_BASE_URL as string) || '/api',
}

export const STORAGE_KEYS = {
  TOKEN: 'lasso_token',
  REFRESH: 'lasso_refresh',
  USER: 'lasso_user',
  RAIL: 'lasso_rail_collapsed',
  THEME: 'lasso_theme',
  SKIN: 'lasso_skin',
}
