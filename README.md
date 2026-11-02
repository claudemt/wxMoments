<p align="center">
  <img src="https://img.shields.io/badge/Windows-支持-blue?logo=windows" alt="Windows">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT">
  <img src="https://img.shields.io/badge/PR-welcome-brightgreen" alt="PR welcome">
</p>

<h1 align="center">📱 wxMoments</h1>
<p align="center"><b>微信数据导出备份工具</b></p>
<p align="center">朋友圈导出 · 公众号导出 · 好友列表导出，全部保存到本地</p>

---

## ✨ 功能

| 功能 | 说明 |
|------|------|
| 📄 **朋友圈导出** | 按好友 / 微信标签 / 时间筛选，导出 Markdown · HTML · PDF，图片、位置、点赞评论完整还原 |
| 🏷️ **微信标签分组** | 自动读取微信通讯录里已有的标签，与好友名单一样直接用；也能把几位好友存成自己的分组 |
| 📰 **公众号导出** | 单篇下载，或收割某公众号已点开过的全部文章后批量导出 |
| 👤 **好友列表导出** | 备注名 / 昵称 / 微信号 / 地区 / 签名，勾选列导出 CSV |

## 🚀 快速上手

```bash
git clone https://github.com/claudemt/wxMoments.git
cd wxMoments
run.bat
```

双击启动即可：首次会自动准备 `runtime/.venv` 并安装依赖，服务就绪后打开 `http://127.0.0.1:8756/`。再次双击会先停旧服务再启动新代码。

**特别提醒**：工具只能识别**这台电脑上曾经浏览过**的朋友圈：想补全就在微信里把朋友圈一路翻到底。同理，工具只能识别**这台电脑上曾经浏览过**的公众号文章：想补全就去把该作者下面的所有文章都点一遍。

## 🤝 贡献

欢迎提 Issue 和 PR：期待的新功能、Mac 上提取 db_key 的方案、新的微信版本兼容问题……