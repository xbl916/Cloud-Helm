const api = require("../../utils/api")

const roles = [
  {value: "viewer", label: "只读：状态和日志"},
  {value: "operator", label: "运维：可启停和重启"},
  {value: "admin", label: "管理员：全部权限"}
]
const roleNames = {admin: "管理员", operator: "运维", viewer: "只读"}

Page({
  data: {loading: true, saving: false, error: "", users: [], roles, roleIndex: 0, canCreateUsers: false, form: {wecom_userid: "", display_name: ""}},
  onShow() { this.load() },
  onPullDownRefresh() { this.load().finally(() => wx.stopPullDownRefresh()) },
  async load() {
    this.setData({loading: true, error: ""})
    try {
      const [current, usersResponse] = await Promise.all([api.get("/auth/me"), api.get("/users")])
      const users = usersResponse.map(item => ({...item, roleName: roleNames[item.role] || item.role}))
      this.setData({users, canCreateUsers: current.role === "admin"})
    } catch (error) { this.setData({error: error.message}) }
    finally { this.setData({loading: false}) }
  },
  input(event) { this.setData({[`form.${event.currentTarget.dataset.field}`]: event.detail.value}) },
  chooseRole(event) { this.setData({roleIndex: Number(event.detail.value)}) },
  async addUser() {
    const wecom_userid = this.data.form.wecom_userid.trim()
    const display_name = this.data.form.display_name.trim()
    if (!wecom_userid || !display_name) { this.setData({error: "请填写准确 UserId 和显示名称"}); return }
    this.setData({saving: true, error: ""})
    try {
      await api.post("/users", {wecom_userid, display_name, role: roles[this.data.roleIndex].value})
      this.setData({form: {wecom_userid: "", display_name: ""}, roleIndex: 0})
      wx.showToast({title: "账号已创建", icon: "success"})
      await this.load()
    } catch (error) { this.setData({error: error.message}) }
    finally { this.setData({saving: false}) }
  },
  openAccess(event) {
    const {id, name, role} = event.currentTarget.dataset
    wx.navigateTo({url: `/pages/access/index?id=${id}&name=${encodeURIComponent(name)}&role=${role}`})
  },
  toggleUser(event) {
    const {id, active} = event.currentTarget.dataset
    wx.showModal({title: active ? "停用账号？" : "启用账号？", content: active ? "现有登录会话也会被撤销。" : "成员将可以重新登录。", success: async result => {
      if (!result.confirm) return
      try { await api.patch(`/users/${id}`, {is_active: !active}); await this.load() }
      catch (error) { this.setData({error: error.message}) }
    }})
  },
  async revoke(event) {
    try { await api.post(`/users/${event.currentTarget.dataset.id}/sessions/revoke`, {}); wx.showToast({title: "已下线", icon: "success"}) }
    catch (error) { this.setData({error: error.message}) }
  }
})
