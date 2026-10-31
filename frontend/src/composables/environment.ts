import { onMounted, onUnmounted, ref } from 'vue';
import { api, errorMessage } from '../api';
import type { Health } from '../types';

export function useEnvironment(pollInterval = 15_000) {
  const health = ref<Health | null>(null);
  const loading = ref(false);
  const error = ref('');
  let timer: ReturnType<typeof setTimeout> | undefined;
  let disposed = false;

  async function refresh() {
    if (loading.value || disposed) return;
    clearTimeout(timer);
    loading.value = true;
    try {
      const result = await api.health();
      if (!disposed) {
        health.value = result;
        error.value = '';
      }
    } catch (cause) {
      if (!disposed) {
        // 连通性丢失后不保留绿色旧状态冒充当前环境。
        health.value = null;
        error.value = errorMessage(cause);
      }
    } finally {
      loading.value = false;
      if (!disposed && pollInterval > 0) timer = setTimeout(refresh, pollInterval);
    }
  }

  onMounted(refresh);
  onUnmounted(() => {
    disposed = true;
    clearTimeout(timer);
  });
  return { health, loading, error, refresh };
}
