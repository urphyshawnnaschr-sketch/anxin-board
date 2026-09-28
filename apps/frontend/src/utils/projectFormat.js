// 纯展示函数：不访问网络、路由或 Vue 状态。

export function statusLabel(status) {
  return ({
    draft: '准备中',
    active: '进行中',
    archived: '已归档'
  })[status] || '状态未知'
}

export function formatTime(iso) {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
}
