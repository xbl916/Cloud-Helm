function bytes(value) {
  const size = Number(value || 0)
  if (!size) return "0 B"
  const units = ["B", "KB", "MB", "GB", "TB"]
  const index = Math.min(Math.floor(Math.log(size) / Math.log(1024)), units.length - 1)
  return `${(size / Math.pow(1024, index)).toFixed(index > 1 ? 1 : 0)} ${units[index]}`
}

function time(value) {
  if (!value) return "从未上报"
  return new Date(value).toLocaleString("zh-CN", {hour12: false})
}

function metric(value, suffix = "", digits = 0) {
  return value === null || value === undefined ? "—" : `${Number(value).toFixed(digits)}${suffix}`
}

function rate(value) { return `${bytes(value)}/s` }

function duration(value) {
  const seconds = Math.max(0, Number(value || 0))
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor(seconds % 86400 / 3600)
  const minutes = Math.floor(seconds % 3600 / 60)
  if (days) return `${days} 天 ${hours} 小时`
  if (hours) return `${hours} 小时 ${minutes} 分钟`
  return `${minutes} 分钟`
}

module.exports = {bytes, time, metric, rate, duration}
