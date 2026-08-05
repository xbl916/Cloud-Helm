const api = require("../../utils/api")
const format = require("../../utils/format")

Page({
  data: {loading: true, error: "", nodes: [], query: ""},
  onShow() { this.load() },
  onPullDownRefresh() { this.load().finally(() => wx.stopPullDownRefresh()) },
  async load() {
    this.setData({loading: true, error: ""})
    try {
      const nodes = (await api.get("/nodes")).map(item => {
        const system = item.system_metrics || {}
        const diskPercent = system.disk_total_bytes ? system.disk_used_bytes / system.disk_total_bytes * 100 : null
        return {
          ...item,
          lastSeen: format.time(item.last_seen_at),
          network: item.system_metrics_status === "ok" ? `↓ ${format.rate(system.network_rx_bps)} · ↑ ${format.rate(system.network_tx_bps)}` : "网络 —",
          disk: diskPercent === null ? "磁盘 —" : `磁盘 ${diskPercent.toFixed(1)}%`
        }
      })
      this.allNodes = nodes
      this.applyFilter()
    } catch (error) { this.setData({error: error.message}) }
    finally { this.setData({loading: false}) }
  },
  search(event) { this.setData({query: event.detail.value}); this.applyFilter() },
  applyFilter() {
    const query = this.data.query.trim().toLowerCase()
    const nodes = (this.allNodes || []).filter(item => !query || `${item.name} ${item.hostname} ${item.environment}`.toLowerCase().includes(query))
    this.setData({nodes})
  },
  openNode(event) { wx.navigateTo({url: `/pages/node/index?id=${event.currentTarget.dataset.id}`}) }
})
