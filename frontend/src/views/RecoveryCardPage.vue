<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRoute } from 'vue-router';
import { api, errorMessage } from '../api';
import RecoveryCard from '../components/RecoveryCard.vue';
import type { IncidentReview, Probe } from '../types';

const route = useRoute();
const review = ref<IncidentReview | null>(null);
const afterSample = ref<Probe[]>([]);
const error = ref('');

onMounted(async () => {
  const id = String(route.params.id ?? '');
  try {
    const [summary, incident] = await Promise.all([api.incidentReview(id), api.incident(id)]);
    review.value = summary;
    afterSample.value = incident.detail.verification?.samples.at(-1) ?? [];
  } catch (cause) {
    error.value = errorMessage(cause);
  }
});
</script>

<template>
  <div class="card-page">
    <RecoveryCard v-if="review" :review="review" :after-sample="afterSample" />
    <p v-else-if="error" class="error">{{ error }}</p>
    <p v-else class="muted">正在读取已保存的复盘记录…</p>
  </div>
</template>

<style scoped>
.card-page {
  max-width: 920px;
  margin: 0 auto;
}
.error {
  color: var(--red-dark);
}
</style>
