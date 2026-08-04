const api = require("../../utils/api")
const format = require("../../utils/format")

Page({
  data: {loading: true, error: "", dashboard: null, nodes: []},
  onShow() { this.load() },
  onPullDownRefresh() { this.load().finally(() => wx.stopPullDownRefresh()) },
  async load() {
    this.setData({loading: true, error: ""})
    try {
      const [dashboard, nodes] = await Promise.all([api.get("/dashboard"), api.get("/nodes")])
      const prepared = nodes.slice(0, 4).map(node => ({...node, lastSeen: format.time(node.last_seen_at)}))
      this.setData({dashboard, nodes: prepared})
    } catch (error) {
      this.setData({error: error.message})
    } finally {
      this.setData({loading: false})
    }
  },
  openNode(event) {
    wx.navigateTo({url: `/pages/node/index?id=${event.currentTarget.dataset.id}`})
  }
})
