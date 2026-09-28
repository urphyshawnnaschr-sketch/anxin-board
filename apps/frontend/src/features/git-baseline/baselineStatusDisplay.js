// Git 基线状态徽章展示映射：纯函数，供页面与测试共用。

export function lineageStatusText(status) {
  if (status === 'loading') return '正在读取'
  if (status === 'active') return '基线已建立'
  if (status === 'no_lineage') return '未建立基线'
  return '读取失败'
}

export function lineageStatusClass(status) {
  if (status === 'loading') return 'lineage-loading'
  if (status === 'active') return 'lineage-active'
  if (status === 'error') return 'status-error'
  return 'lineage-none'
}
