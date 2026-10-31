import { ref } from 'vue';
import { api, errorMessage } from '../api';
import type { IncidentSummary } from '../types';

export const recent = ref<IncidentSummary[]>([]);
export const recentError = ref('');
export const recentLoading = ref(false);

export async function loadRecent() {
  if (recentLoading.value) return;
  recentLoading.value = true;
  recentError.value = '';
  try {
    recent.value = await api.incidents({ limit: 6 });
  } catch (cause) {
    recentError.value = errorMessage(cause);
  } finally {
    recentLoading.value = false;
  }
}

export function updateRecent(item: IncidentSummary) {
  const index = recent.value.findIndex((entry) => entry.id === item.id);
  if (index >= 0) recent.value[index] = item;
}
