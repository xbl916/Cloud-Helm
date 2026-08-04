const api = require("../../utils/api")
const format = require("../../utils/format")

Page({
  data: {loading: true, error: "", items: []},
  onShow() { this.load() },
  onPullDownRefresh() { this.load().finally(() => wx.stopPullDownRefresh()) },
  async load() {
    this.setData({loading: true, error: ""})
    try {
      const items = (await api.get("/audit?limit=100")).map(item => ({...item, time: format.time(item.created_at)}))
      this.setData({items})
    } catch (error) { this.setData({error: error.message}) }
    finally { this.setData({loading: false}) }
  }
})
