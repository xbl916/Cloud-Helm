const api = require("../../utils/api")
const format = require("../../utils/format")

function delay(ms) { return new Promise(resolve => setTimeout(resolve, ms)) }

Page({
  data: {loading: true, working: false, error: "", container: null, logs: "", logsVisible: false},
  onLoad(options) { this.containerId = options.id },
  onShow() { if (this.containerId) this.load() },
  onPullDownRefresh() { this.load().finally(() => wx.stopPullDownRefresh()) },
  async load() {
    this.setData({loading: true, error: ""})
    try {
      const raw = await api.get(`/containers/${this.containerId}`)
      const container = {
        ...raw,
        memory: format.bytes(raw.memory_usage),
        updated: format.time(raw.updated_at),
        gpuText: raw.gpu_all ? "全部 GPU" : ((raw.gpu_devices || []).join(", ") || "未分配"),
        assigned_gpus: (raw.assigned_gpus || []).map(gpu => ({
          ...gpu,
          utilization: format.metric(gpu.utilization_gpu, "%", 1),
          memory: `${format.metric(gpu.memory_used_mib, " MiB")} / ${format.metric(gpu.memory_total_mib, " MiB")}`,
          temperature: format.metric(gpu.temperature_c, "°C")
        }))
      }
      this.setData({container})
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
  async runAction(action) {
    if (this.data.working) return
    this.setData({working: true, error: ""})
    if (action === "logs") this.setData({logsVisible: true, logs: "正在向 Agent 请求最近 200 行日志…"})
    wx.showLoading({title: action === "logs" ? "读取日志" : "下发任务", mask: true})
    try {
      const queued = await api.post(`/containers/${this.containerId}/actions`, action === "logs" ? {action, tail: 200} : {action})
      const task = await this.waitTask(queued.id)
      if (task.status !== "success") throw new Error(task.error || "Agent 执行失败")
      if (action === "logs") this.setData({logs: task.result || "（日志为空）"})
      else { wx.showToast({title: "操作成功", icon: "success"}); await this.load() }
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
