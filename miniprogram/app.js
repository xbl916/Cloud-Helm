const config = require("./config")

const TOKEN_KEY = "cloudhelm_access_token"
const EXPIRES_KEY = "cloudhelm_access_expires"
const USER_KEY = "cloudhelm_user"

App({
  globalData: {
    user: null,
    loginPromise: null
  },

  onLaunch() {
    this.globalData.user = wx.getStorageSync(USER_KEY) || null
  },

  clearSession() {
    wx.removeStorageSync(TOKEN_KEY)
    wx.removeStorageSync(EXPIRES_KEY)
    wx.removeStorageSync(USER_KEY)
    this.globalData.user = null
  },

  getStoredToken() {
    const token = wx.getStorageSync(TOKEN_KEY)
    const expiresAt = Number(wx.getStorageSync(EXPIRES_KEY) || 0)
    if (token && expiresAt > Date.now() + 60000) return token
    this.clearSession()
    return ""
  },

  ensureLogin(force = false) {
    if (force) this.clearSession()
    const stored = this.getStoredToken()
    if (stored) return Promise.resolve(stored)
    if (this.globalData.loginPromise) return this.globalData.loginPromise
    this.globalData.loginPromise = this.login().finally(() => {
      this.globalData.loginPromise = null
    })
    return this.globalData.loginPromise
  },

  login() {
    if (!/^https:\/\/[^/]+$/.test(config.baseUrl) || config.baseUrl.includes("example.com")) {
      return Promise.reject(new Error("请先在 miniprogram/config.js 配置云舵 HTTPS 地址"))
    }
    if (!wx.qy || !wx.qy.login) {
      return Promise.reject(new Error("请从企业微信打开小程序，或在开发者工具中启用企业微信模拟"))
    }
    return new Promise((resolve, reject) => {
      wx.qy.login({
        timeout: 10000,
        success: result => {
          if (!result.code) {
            reject(new Error("企业微信没有返回登录凭证"))
            return
          }
          wx.request({
            url: `${config.baseUrl}/api/v1/auth/wecom-mini/login`,
            method: "POST",
            timeout: 15000,
            header: {"content-type": "application/json"},
            data: {code: result.code},
            success: response => {
              if (response.statusCode !== 200 || !response.data.access_token) {
                reject(new Error((response.data && response.data.detail) || `登录失败 (${response.statusCode})`))
                return
              }
              const expiresAt = Date.now() + Number(response.data.expires_in || 0) * 1000
              wx.setStorageSync(TOKEN_KEY, response.data.access_token)
              wx.setStorageSync(EXPIRES_KEY, expiresAt)
              wx.setStorageSync(USER_KEY, response.data.user)
              this.globalData.user = response.data.user
              resolve(response.data.access_token)
            },
            fail: error => reject(new Error(error.errMsg || "无法连接云舵 Server"))
          })
        },
        fail: error => reject(new Error(error.errMsg || "企业微信登录失败"))
      })
    })
  }
})
