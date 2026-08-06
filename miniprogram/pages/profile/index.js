const api = require("../../utils/api")

const roleNames = {admin: "管理员", operator: "运维", viewer: "只读"}

Page({
  data: {loading: true, error: "", user: null, roleName: "", signedOut: false},
  onShow() { if (!this.data.signedOut) this.load() },
  async load() {
    this.setData({loading: true, error: ""})
    try {
      const user = await api.get("/auth/me")
      getApp().globalData.user = user
      this.setData({
        user: {...user, avatar: user.display_name.slice(0, 1)},
        roleName: `${roleNames[user.role] || user.role}${user.role !== "admin" && user.can_manage_access ? " · 资源管理员" : ""}`
      })
    } catch (error) { this.setData({error: error.message}) }
    finally { this.setData({loading: false}) }
  },
  openAudit() { wx.navigateTo({url: "/pages/audit/index"}) },
  openUsers() { wx.navigateTo({url: "/pages/users/index"}) },
  async logout() {
    try { await api.post("/auth/logout", {}) } catch (_) {}
    getApp().clearSession()
    this.setData({user: null, signedOut: true, loading: false})
    wx.showToast({title: "已退出", icon: "success"})
  },
  loginAgain() { this.setData({signedOut: false}); this.load() }
})
