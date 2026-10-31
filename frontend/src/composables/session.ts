import { ref } from 'vue';

// 管理令牌只保留在当前页面内存中，刷新后重新输入。
export const adminToken = ref('');
