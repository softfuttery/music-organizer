<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Check, Monitor, Moon, Sun } from '@lucide/vue'

const props = defineProps({
  modelValue: {
    type: String,
    required: true,
  },
})
const emit = defineEmits(['update:modelValue', 'change'])

const root = ref(null)
const open = ref(false)
const options = [
  { value: 'system', label: '跟随系统', icon: Monitor },
  { value: 'light', label: '浅色', icon: Sun },
  { value: 'dark', label: '深色', icon: Moon },
]
const selected = computed(() => options.find((option) => option.value === props.modelValue) || options[0])

function choose(value) {
  emit('update:modelValue', value)
  emit('change', value)
  open.value = false
}

function closeFromOutside(event) {
  if (!root.value?.contains(event.target)) open.value = false
}

function closeFromKeyboard(event) {
  if (event.key === 'Escape') open.value = false
}

onMounted(() => {
  document.addEventListener('pointerdown', closeFromOutside)
  document.addEventListener('keydown', closeFromKeyboard)
})

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', closeFromOutside)
  document.removeEventListener('keydown', closeFromKeyboard)
})
</script>

<template>
  <div ref="root" class="theme-picker" :class="{ open }">
    <button
      class="theme-picker-trigger"
      type="button"
      aria-label="外观模式"
      aria-haspopup="menu"
      :aria-expanded="open"
      @click="open = !open"
    >
      <component :is="selected.icon" :size="15" />
      <span>{{ selected.label }}</span>
      <svg class="theme-picker-chevron" viewBox="0 0 12 12" aria-hidden="true"><path d="m3 4.5 3 3 3-3" /></svg>
    </button>
    <div v-if="open" class="theme-picker-menu" role="menu" aria-label="选择外观模式">
      <button
        v-for="option in options"
        :key="option.value"
        type="button"
        role="menuitemradio"
        :aria-checked="modelValue === option.value"
        :class="{ active: modelValue === option.value }"
        @click="choose(option.value)"
      >
        <component :is="option.icon" :size="15" />
        <span>{{ option.label }}</span>
        <Check v-if="modelValue === option.value" :size="14" />
      </button>
    </div>
  </div>
</template>
