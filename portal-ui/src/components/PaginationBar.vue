<script setup>
/**
 * 分页栏只负责显示当前位置并发出前后翻页意图。
 * 数据切片和页码有效性仍由页面控制器统一管理。
 */
defineProps({
  page: { type: Number, required: true },
  pages: { type: Number, required: true },
  total: { type: Number, required: true },
});
defineEmits(["previous", "next"]);
</script>

<template>
  <nav
    v-if="pages > 1"
    aria-label="分页"
    style="display: flex; justify-content: flex-end; align-items: center; gap: 10px; margin-top: 16px"
  >
    <span style="color: var(--muted); font-size: 12px; margin-right: 4px">
      第 {{ page }} / {{ pages }} 页 · 共 {{ total }} 条
    </span>
    <button type="button" class="ghost" :disabled="page <= 1" @click="$emit('previous')">
      上一页
    </button>
    <button type="button" class="ghost" :disabled="page >= pages" @click="$emit('next')">
      下一页
    </button>
  </nav>
</template>
