const api = require("../../utils/api")
const format = require("../../utils/format")

Page({
  data: {loading: true, error: "", node: null, containers: []},
  onLoad(options) { this.nodeId = options.id },
  onShow() { if (this.nodeId) this.load() },
  onPullDownRefresh() { this.load().finally(() => wx.stopPullDownRefresh()) },
  async load() {
    this.setData({loading: true, error: ""})
    try {
      const [nodes, containers] = await Promise.all([api.get("/nodes"), api.get(`/nodes/${this.nodeId}/containers`)])
      const raw = nodes.find(item => item.id === this.nodeId)
      if (!raw) throw new Error("节点不存在或当前账号无权查看")
      const node = {
        ...raw,
        lastSeen: format.time(raw.last_seen_at),
        gpus: (raw.gpus || []).map(gpu => ({
          ...gpu,
          utilization: format.metric(gpu.utilization_gpu, "%", 1),
          memory: `${format.metric(gpu.memory_used_mib, " MiB")} / ${format.metric(gpu.memory_total_mib, " MiB")}`,
          temperature: format.metric(gpu.temperature_c, "°C"),
          power: format.metric(gpu.power_draw_w, " W", 1)
        }))
      }
      const prepared = containers.map(item => ({...item, memory: format.bytes(item.memory_usage), gpu: item.gpu_all ? "全部 GPU" : (item.gpu_devices || []).join(", ")}))
      this.setData({node, containers: prepared})
    } catch (error) { this.setData({error: error.message}) }
    finally { this.setData({loading: false}) }
  },
  openContainer(event) { wx.navigateTo({url: `/pages/container/index?id=${event.currentTarget.dataset.id}`}) }
})
