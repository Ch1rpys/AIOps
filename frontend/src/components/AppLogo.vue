<script setup lang="ts">
withDefaults(
  defineProps<{
    size?: number;
  }>(),
  {
    size: 34,
  },
);
</script>

<template>
  <div class="brand-logo-container" :style="{ width: `${size}px`, height: `${size}px` }">
    <svg viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
      <rect width="100" height="100" rx="24" fill="#2563EB" />
      <!-- AI Curved Modern Mark like prototype -->
      <path
        d="M26 74C26 50 44 26 68 26C80 26 82 36 74 44C66 52 46 54 36 60C28 65 30 74 42 74C56 74 68 62 74 54"
        stroke="#FFFFFF"
        stroke-width="10"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
      <circle cx="68" cy="26" r="6" fill="#60A5FA" />
      <circle cx="34" cy="74" r="5" fill="#93C5FD" />
    </svg>
  </div>
</template>

<style scoped>
.brand-logo-container {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  border-radius: 9px;
  overflow: hidden;
  box-shadow: 0 4px 10px rgba(37, 99, 235, 0.25);
  transition:
    transform 0.2s cubic-bezier(0.16, 1, 0.3, 1),
    box-shadow 0.2s;
}

.brand-logo-container:hover {
  transform: translateY(-1px) scale(1.04);
  box-shadow: 0 6px 14px rgba(37, 99, 235, 0.35);
}

.brand-logo-container svg {
  width: 100%;
  height: 100%;
  display: block;
}
</style>
