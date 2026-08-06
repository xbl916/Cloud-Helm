const api = require("../../utils/api")

function key(rule) {
  return [rule.scope_type, rule.environment || "", rule.node_id || "", rule.project || "", rule.container_id || ""].join("|")
}

function level(rule) {
  if (rule.can_manage) return "manage"
  if (rule.can_operate) return "operate"
  if (rule.can_logs) return "logs"
  return "view"
}

Page({
  data: {loading: true, saving: false, error: "", name: "", role: "viewer", restricted: true, allowUnrestricted: false, partial: false, rows: []},
  onLoad(options) {
    this.userId = options.id
    this.setData({name: decodeURIComponent(options.name || "成员"), role: options.role || "viewer"})
    this.load()
  },
  async load() {
    this.setData({loading: true, error: ""})
    try {
      const [config, resources] = await Promise.all([api.get(`/users/${this.userId}/access`), api.get("/access/resources")])
      const existing = new Map(config.rules.map(rule => [key(rule), rule]))
      const levels = this.data.role === "operator" ? ["仅查看", "查看 + 日志", "查看 + 日志 + 运维", "资源管理员"] : ["仅查看", "查看 + 日志"]
      this.editable = new Set(resources.editable_scope_keys || [])
      const rows = []
      resources.environments.forEach(environment => {
        this.pushRow(rows, existing, levels, {scope_type: "environment", environment}, environment, "该环境全部节点", 0)
        resources.nodes.filter(node => node.environment === environment).forEach(node => {
          this.pushRow(rows, existing, levels, {scope_type: "node", node_id: node.id}, node.name, node.hostname || "节点", 1)
          const projects = [...new Set(node.containers.map(container => container.project || "独立容器"))]
          projects.forEach(project => {
            if (project !== "独立容器") this.pushRow(rows, existing, levels, {scope_type: "project", node_id: node.id, project}, project, "Compose 项目", 2)
            node.containers.filter(container => (container.project || "独立容器") === project).forEach(container => {
              this.pushRow(rows, existing, levels, {scope_type: "container", node_id: node.id, container_id: container.id}, container.name, `${project} · ${container.status}`, 3)
            })
          })
        })
      })
      this.setData({restricted: resources.allow_unrestricted ? config.restricted : true, allowUnrestricted: Boolean(resources.allow_unrestricted), partial: Boolean(resources.partial), rows})
    } catch (error) { this.setData({error: error.message}) }
    finally { this.setData({loading: false}) }
  },
  pushRow(rows, existing, levels, rule, label, note, depth) {
    const found = existing.get(key(rule))
    const values = ["view", "logs", ...(this.data.role === "operator" ? ["operate", "manage"] : [])]
    rows.push({scopeKey: key(rule), ...rule, label, note, depth, editable: this.editable.has(key(rule)), checked: Boolean(found), levels, values, levelIndex: found ? Math.max(0, values.indexOf(level(found))) : 0})
  },
  changeMode(event) { this.setData({restricted: this.data.allowUnrestricted ? event.detail.value : true}) },
  toggleRule(event) { this.setData({[`rows[${event.currentTarget.dataset.index}].checked`]: event.detail.value}) },
  changeLevel(event) { this.setData({[`rows[${event.currentTarget.dataset.index}].levelIndex`]: Number(event.detail.value)}) },
  async save() {
    this.setData({saving: true, error: ""})
    const rules = this.data.restricted ? this.data.rows.filter(row => row.editable && row.checked).map(row => {
      const selected = row.values[row.levelIndex]
      return {
        scope_type: row.scope_type,
        environment: row.environment || null,
        node_id: row.node_id || null,
        project: row.project || null,
        container_id: row.container_id || null,
        can_view: true,
        can_logs: ["logs", "operate", "manage"].includes(selected),
        can_operate: ["operate", "manage"].includes(selected),
        can_manage: selected === "manage"
      }
    }) : []
    try {
      await api.put(`/users/${this.userId}/access`, {restricted: this.data.restricted, rules})
      wx.showToast({title: "权限已保存", icon: "success"})
      setTimeout(() => wx.navigateBack(), 700)
    } catch (error) { this.setData({error: error.message}) }
    finally { this.setData({saving: false}) }
  }
})
