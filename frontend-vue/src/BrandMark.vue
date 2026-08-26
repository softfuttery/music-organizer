<script setup>
defineProps({
  size: {
    type: Number,
    default: 34,
  },
})
</script>

<template>
  <span class="brand-symbol" :style="{ '--brand-symbol-size': `${size}px` }" aria-hidden="true">
    <svg class="brand-symbol-light" viewBox="0 0 48 48" role="img">
      <rect x="1" y="1" width="46" height="46" rx="13" fill="#171717" />
      <path d="M31 10.5v21.8a6.3 6.3 0 1 1-3-5.4V17.7l-11 2.5v14.4a6.3 6.3 0 1 1-3-5.4V17.1L31 13.3z" fill="#fff" />
      <path d="m17 20.2 14-3.2v3.7l-14 3.2z" fill="#a3a3a3" />
    </svg>
    <svg class="brand-symbol-dark" viewBox="0 0 48 48" role="img">
      <defs>
        <linearGradient id="dark-mark-bg" x1="7" y1="4" x2="42" y2="45" gradientUnits="userSpaceOnUse">
          <stop stop-color="#273039" />
          <stop offset="1" stop-color="#111519" />
        </linearGradient>
        <linearGradient id="dark-mark-note" x1="14" y1="11" x2="35" y2="37" gradientUnits="userSpaceOnUse">
          <stop stop-color="#d9ffe7" />
          <stop offset="1" stop-color="#5ee39a" />
        </linearGradient>
      </defs>
      <rect x="1" y="1" width="46" height="46" rx="13" fill="url(#dark-mark-bg)" stroke="#424b54" />
      <path d="M31.5 10.5v21.8a6.3 6.3 0 1 1-3-5.4v-9.2l-11 2.5v14.4a6.3 6.3 0 1 1-3-5.4V17.1l17-3.8z" fill="url(#dark-mark-note)" />
      <path d="m17.5 20.2 14-3.2v3.7l-14 3.2z" fill="#91f0b6" opacity=".58" />
      <circle cx="38" cy="10" r="2" fill="#f6d98d" />
    </svg>
  </span>
</template>
