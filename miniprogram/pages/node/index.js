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
        system: raw.system_metrics_status === "ok" ? {
          cpu: format.metric(raw.system_metrics.cpu_percent, "%", 1),
          memory: `${format.bytes(raw.system_metrics.memory_used_bytes)} / ${format.bytes(raw.system_metrics.memory_total_bytes)}`,
          memoryPercent: format.metric(raw.system_metrics.memory_percent, "%", 1),
          swap: `${format.bytes(raw.system_metrics.swap_used_bytes)} / ${format.bytes(raw.system_metrics.swap_total_bytes)}`,
          load: `${format.metric(raw.system_metrics.load_1, "", 2)} / ${format.metric(raw.system_metrics.load_5, "", 2)} / ${format.metric(raw.system_metrics.load_15, "", 2)}`,
          uptime: format.duration(raw.system_metrics.uptime_seconds),
          disk: `${format.bytes(raw.system_metrics.disk_used_bytes)} / ${format.bytes(raw.system_metrics.disk_total_bytes)}`,
          diskFree: format.bytes(raw.system_metrics.disk_free_bytes),
          inodes: raw.system_metrics.disk_inodes_total ? `${(raw.system_metrics.disk_inodes_used / raw.system_metrics.disk_inodes_total * 100).toFixed(1)}%` : "—",
          receive: format.rate(raw.system_metrics.network_rx_bps),
          transmit: format.rate(raw.system_metrics.network_tx_bps),
          receiveTotal: format.bytes(raw.system_metrics.network_rx_bytes),
          transmitTotal: format.bytes(raw.system_metrics.network_tx_bytes)
        } : null,
        gpus: (raw.gpus || []).map(gpu => ({
          ...gpu,
          utilization: format.metric(gpu.utilization_gpu, "%", 1),
          memory: `${format.metric(gpu.memory_used_mib, " MiB")} / ${format.metric(gpu.memory_total_mib, " MiB")}`,
          temperature: format.metric(gpu.temperature_c, "°C"),
          power: format.metric(gpu.power_draw_w, " W", 1)
        }))
      }
      const prepared = containers.map(item => ({
        ...item,
        memory: format.bytes(item.memory_usage),
        network: `↓ ${format.rate(item.network_rx_bps)} · ↑ ${format.rate(item.network_tx_bps)}`,
        block: `读 ${format.rate(item.block_read_bps)} · 写 ${format.rate(item.block_write_bps)}`,
        disk: format.bytes(item.writable_layer_bytes),
        gpu: item.gpu_all ? "全部 GPU" : (item.gpu_devices || []).join(", "),
        alert: item.oom_killed ? "OOM Kill" : (item.health === "unhealthy" ? "健康检查失败" : "")
      }))
      this.setData({node, containers: prepared})
    } catch (error) { this.setData({error: error.message}) }
    finally { this.setData({loading: false}) }
  },
  openContainer(event) { wx.navigateTo({url: `/pages/container/index?id=${event.currentTarget.dataset.id}`}) }
})
