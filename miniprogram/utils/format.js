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

function padDatePart(value) { return String(value).padStart(2, "0") }

function logTimestamp(date, fraction = "") {
  const offsetMinutes = -date.getTimezoneOffset()
  const sign = offsetMinutes >= 0 ? "+" : "-"
  const absoluteOffset = Math.abs(offsetMinutes)
  return `${date.getFullYear()}-${padDatePart(date.getMonth() + 1)}-${padDatePart(date.getDate())}T${padDatePart(date.getHours())}:${padDatePart(date.getMinutes())}:${padDatePart(date.getSeconds())}${fraction}${sign}${padDatePart(Math.floor(absoluteOffset / 60))}:${padDatePart(absoluteOffset % 60)}`
}

function logs(value) {
  return String(value || "").replace(
    /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})(?=\s)/gm,
    (original, seconds, fraction = "", zone) => {
      const parsed = new Date(`${seconds}${fraction ? fraction.slice(0, 4) : ""}${zone}`)
      return Number.isNaN(parsed.getTime()) ? original : logTimestamp(parsed, fraction)
    }
  )
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

module.exports = {bytes, time, logs, metric, rate, duration}
