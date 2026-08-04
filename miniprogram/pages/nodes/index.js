const api = require("../../utils/api")
const format = require("../../utils/format")

Page({
  data: {loading: true, error: "", nodes: [], query: ""},
  onShow() { this.load() },
  onPullDownRefresh() { this.load().finally(() => wx.stopPullDownRefresh()) },
  async load() {
    this.setData({loading: true, error: ""})
    try {
      const nodes = (await api.get("/nodes")).map(item => ({...item, lastSeen: format.time(item.last_seen_at)}))
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
