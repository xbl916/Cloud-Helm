const api = require("../../utils/api")
const format = require("../../utils/format")

function delay(ms) { return new Promise(resolve => setTimeout(resolve, ms)) }
function splitTaggedImage(image) {
  if (!image || image.includes("@")) return null
  const slash = image.lastIndexOf("/")
  const colon = image.lastIndexOf(":")
  if (colon <= slash) return null
  return {repository: image.slice(0, colon), tag: image.slice(colon + 1)}
}

Page({
  data: {loading: true, working: false, error: "", container: null, logs: "", logsVisible: false, targetTag: ""},
  onLoad(options) { this.containerId = options.id },
  onShow() { if (this.containerId) this.load() },
  onPullDownRefresh() { this.load().finally(() => wx.stopPullDownRefresh()) },
  async load() {
    this.setData({loading: true, error: ""})
    try {
      const raw = await api.get(`/containers/${this.containerId}`)
      const image = splitTaggedImage(raw.image)
      const container = {
        ...raw,
        memory: format.bytes(raw.memory_usage),
        updated: format.time(raw.updated_at),
        gpuText: raw.gpu_all ? "全部 GPU" : ((raw.gpu_devices || []).join(", ") || "未分配"),
        imageRepository: image ? image.repository : "",
        imageTag: image ? image.tag : "",
        assigned_gpus: (raw.assigned_gpus || []).map(gpu => ({
          ...gpu,
          utilization: format.metric(gpu.utilization_gpu, "%", 1),
          memory: `${format.metric(gpu.memory_used_mib, " MiB")} / ${format.metric(gpu.memory_total_mib, " MiB")}`,
          temperature: format.metric(gpu.temperature_c, "°C")
        }))
      }
      this.setData({container, targetTag: ""})
    } catch (error) { this.setData({error: error.message}) }
    finally { this.setData({loading: false}) }
  },
  async waitTask(id) {
    for (let attempt = 0; attempt < 35; attempt += 1) {
      const task = await api.get(`/tasks/${id}`)
      if (["success", "failed", "expired"].includes(task.status)) return task
      await delay(1000)
    }
    throw new Error("Agent 响应超时，请稍后查看审计记录")
  },
  readLogs() { this.runAction("logs") },
  confirmAction(event) {
    const action = event.currentTarget.dataset.action
    const names = {start: "启动", stop: "停止", restart: "重启"}
    wx.showModal({
      title: `确认${names[action]}容器？`,
      content: `${this.data.container.node_name} / ${this.data.container.name}\n操作将写入审计日志。`,
      success: result => { if (result.confirm) this.runAction(action) }
    })
  },
  onTargetTag(event) { this.setData({targetTag: event.detail.value.trim()}) },
  confirmImageUpdate() {
    const container = this.data.container
    const targetTag = this.data.targetTag
    if (!/^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/.test(targetTag)) {
      this.setData({error: "镜像 Tag 格式无效"})
      return
    }
    if (targetTag === container.imageTag) {
      this.setData({error: "新旧镜像 Tag 不能相同"})
      return
    }
    const targetImage = `${container.imageRepository}:${targetTag}`
    wx.showModal({
      title: "再次确认替换镜像？",
      content: `容器：${container.name}\n原镜像：${container.image}\n新镜像：${targetImage}\n\nAgent 将重建容器，失败时自动恢复。`,
      confirmText: "确认替换",
      confirmColor: "#d85050",
      success: result => { if (result.confirm) this.runAction("update_image", {target_image: targetImage}) }
    })
  },
  async runAction(action, extra = {}) {
    if (this.data.working) return
    this.setData({working: true, error: ""})
    if (action === "logs") this.setData({logsVisible: true, logs: "正在向 Agent 请求最近 200 行日志…"})
    wx.showLoading({title: action === "logs" ? "读取日志" : "下发任务", mask: true})
    try {
      const queued = await api.post(`/containers/${this.containerId}/actions`, action === "logs" ? {action, tail: 200} : {action, ...extra})
      const task = await this.waitTask(queued.id)
      if (task.status !== "success") throw new Error(task.error || "Agent 执行失败")
      if (action === "logs") this.setData({logs: task.result || "（日志为空）"})
      else { wx.showToast({title: action === "update_image" ? "镜像已更新" : "操作成功", icon: "success"}); await this.load() }
    } catch (error) {
      if (action === "logs") this.setData({logs: `读取失败：${error.message}`})
      else this.setData({error: error.message})
    } finally {
      wx.hideLoading()
      this.setData({working: false})
    }
  },
  closeLogs() { this.setData({logsVisible: false, logs: ""}) }
})
