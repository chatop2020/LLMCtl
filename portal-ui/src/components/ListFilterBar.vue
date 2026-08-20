<script setup>
/**
 * 列表筛选栏统一搜索、状态、分类和结果数量的交互外观。
 * 组件不解释业务字段，只把用户选择回传给对应页面。
 */
defineProps({
  modelValue: { type: String, default: "" },
  status: { type: String, default: "all" },
  statusLabel: { type: String, default: "全部状态" },
  statusOptions: { type: Array, default: () => [] },
  category: { type: String, default: "all" },
  categoryLabel: { type: String, default: "全部分类" },
  categoryOptions: { type: Array, default: () => [] },
  placeholder: { type: String, default: "搜索当前列表" },
  count: { type: Number, default: 0 },
});
defineEmits(["update:modelValue", "update:status", "update:category"]);
</script>

<template>
  <div class="list-filter" role="search">
    <input
      type="search"
      :value="modelValue"
      :placeholder="placeholder"
      :aria-label="placeholder"
      @input="$emit('update:modelValue', $event.target.value)"
    />
    <select
      v-if="statusOptions.length"
      :value="status"
      :aria-label="statusLabel"
      @change="$emit('update:status', $event.target.value)"
    >
      <option value="all">{{ statusLabel }}</option>
      <option v-for="option in statusOptions" :key="option.value" :value="option.value">
        {{ option.label }}
      </option>
    </select>
    <select
      v-if="categoryOptions.length"
      :value="category"
      :aria-label="categoryLabel"
      @change="$emit('update:category', $event.target.value)"
    >
      <option value="all">{{ categoryLabel }}</option>
      <option v-for="option in categoryOptions" :key="option.value" :value="option.value">
        {{ option.label }}
      </option>
    </select>
    <span class="filter-count">{{ count }} 条</span>
  </div>
</template>
