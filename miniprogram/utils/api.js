const config = require("../config")

function rawRequest(path, options, token) {
  return new Promise((resolve, reject) => {
    wx.request({
      url: `${config.baseUrl}/api/v1${path}`,
      method: options.method || "GET",
      data: options.data,
      timeout: options.timeout || 20000,
      header: {
        "content-type": "application/json",
        Authorization: `Bearer ${token}`
      },
      success: response => {
        if (response.statusCode >= 200 && response.statusCode < 300) {
          resolve(response.data)
          return
        }
        const error = new Error((response.data && response.data.detail) || `请求失败 (${response.statusCode})`)
        error.statusCode = response.statusCode
        reject(error)
      },
      fail: error => reject(new Error(error.errMsg || "网络请求失败"))
    })
  })
}

async function request(path, options = {}, retried = false) {
  const app = getApp()
  const token = await app.ensureLogin(retried)
  try {
    return await rawRequest(path, options, token)
  } catch (error) {
    if (error.statusCode === 401 && !retried) return request(path, options, true)
    throw error
  }
}

module.exports = {
  get: path => request(path),
  post: (path, data) => request(path, {method: "POST", data}),
  put: (path, data) => request(path, {method: "PUT", data}),
  patch: (path, data) => request(path, {method: "PATCH", data})
}
