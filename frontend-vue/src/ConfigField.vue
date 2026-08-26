<script setup>
const props = defineProps({
  field: { type: Object, required: true },
  modelValue: { type: [String, Number, Boolean], default: '' },
  saved: { type: Boolean, default: false },
})
const emit = defineEmits(['update:modelValue'])

function updateValue(event) {
  if (props.field.type === 'checkbox') {
    emit('update:modelValue', event.target.checked)
    return
  }
  const value = event.target.value
  emit('update:modelValue', props.field.type === 'number' && value !== '' ? Number(value) : value)
}
</script>

<template>
  <label class="config-field" :class="[`field-${field.type || 'text'}`, { wide: field.wide }]">
    <span class="config-field-label">
      {{ field.label }}
      <small v-if="field.secret" :class="saved ? 'saved' : 'missing'">{{ saved ? '已保存' : '未保存' }}</small>
    </span>
    <span v-if="field.type === 'checkbox'" class="config-switch">
      <input type="checkbox" :name="field.name" :checked="Boolean(modelValue)" @change="updateValue">
      <i></i>
      <span>{{ field.checkboxLabel || '启用' }}</span>
    </span>
    <textarea
      v-else-if="field.type === 'textarea'"
      :name="field.name"
      :rows="field.rows || 4"
      :value="modelValue"
      :placeholder="field.placeholder"
      @input="updateValue"
    ></textarea>
    <select v-else-if="field.type === 'select'" :name="field.name" :value="modelValue" @change="updateValue">
      <option v-for="option in field.options" :key="option.value" :value="option.value">{{ option.label }}</option>
    </select>
    <input
      v-else
      :name="field.name"
      :type="field.type || 'text'"
      :value="modelValue"
      :min="field.min"
      :max="field.max"
      :placeholder="field.secret && saved ? '已保存；留空保持不变' : field.placeholder"
      :autocomplete="field.secret ? 'new-password' : 'off'"
      @input="updateValue"
    >
    <small v-if="field.help" class="config-field-help">{{ field.help }}</small>
  </label>
</template>
