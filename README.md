<p align="center">
  <img src="https://img.shields.io/badge/Windows-支持-blue?logo=windows" alt="Windows">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT">
  <img src="https://img.shields.io/badge/PR-welcome-brightgreen" alt="PR welcome">
</p>

<h1 align="center">📱 wxMoments</h1>
<p align="center"><b>微信数据导出备份工具</b></p>
<p align="center">朋友圈导出 · 公众号导出 · 好友列表导出，全部保存到本地</p>

<p align="center">
  <a href="#-features">功能</a> ·
  <a href="#-quick-start">快速上手</a> ·
  <a href="#-tech">技术说明</a>
</p>

---

## ✨ 功能

| 功能 | 说明 |
|------|------|
| 📄 **朋友圈导出** | 解密本机微信数据库，按好友 / 时间任意组合筛选后批量导出 Markdown · HTML · PDF，文案、九宫格图片、位置、点赞评论完整还原 |
| 📰 **公众号导出** | 单篇下载，或收割某公众号已点开过的全部文章后批量导出（HTML / MD / 图片） |
| 👤 **好友列表导出** | 展示全部好友的备注名 / 昵称 / 微信号 / 地区 / 签名等字段，勾选列后导出 CSV |

---

## 🚀 快速上手

**Windows：**

```bash
git clone https://github.com/claudemt/wxMoments.git
cd wxMoments
run.bat          # 直接双击启动！
```

首次启动会自动创建 `runtime/.venv` 并安装依赖。启动后自动打开网页 `http://127.0.0.1:8756/`，顶部三个功能页签：

- **朋友圈导出**：点「初始化解密」（要求电脑版微信已登录）→ 自动定位账号、获取密钥、解密数据库（首次约 1 分钟，页面实时显示进度）→ 展示好友列表与朋友圈列表 → 按好友 / 时间筛选 → 「下载全部」；好友列表只显示本机有朋友圈数据且仍是好友的人，非好友的历史朋友圈数据不导出
- **公众号导出**：粘贴文章链接 → 「解析」→ 单篇下载，或「关于作者」→ 收割该公众号已点开过的文章 → 勾选批量下载
- **好友列表导出**：展示全部好友的备注名 / 实际名称 / 微信号 / 地区 / 个性签名等字段，勾选导出列后「导出」CSV

> 公众号：微信官方列表接口对本机账号不可用，采用**缓存收割**方案——把该作者的每一篇文章都**点开一遍**（点开即可，可立刻按 **Ctrl+W** 关掉），后台会实时收录 URL 与标题，列表自动变多，点完直接勾选下载。

---

## 🛠️ 技术说明

### 项目结构

```
wxMoments/
├── run.bat              # 双击启动网页服务
├── config/              # 配置与依赖清单
├── src/
│   ├── server.py        # 统一网页后端（朋友圈 + 公众号，FastAPI）
│   ├── wxmoments.py     # 朋友圈导出管线
│   ├── wechat_download/ # 公众号收割与导出模块
│   ├── wechat_decrypt_tool/  # WCDB / ISAAC-64 解密库
│   └── web/             # 统一前端（三功能页签）
├── output/              # 导出产物（朋友圈-<时间>/、公众号文章、export_meta.json）
└── runtime/             # 运行环境、解密产物、日志
```

### 朋友圈

- 从本机微信数据库（sns.db / contact.db）读取，不联网、不窃取隐私
- 发布者已不在好友列表中的朋友圈数据直接丢弃；好友筛选项只列出「发过朋友圈 ∩ 当前好友」的人
- Windows 自动获取数据库密钥与图片解密密钥；密钥保存一次后复用（`runtime/output/account_keys.json`）
- 解密库自动解析并合并 WAL 中当前世代的数据，避免最新内容漏导
- 原图从微信 CDN（`*.qpic.cn`）下载后用 ISAAC-64 密钥流解密；每次导出生成 `download_report.json` 记录成功数、各类失败数与样本链接
- 微信内置表情 shortcode 统一转换为 emoji；PDF 默认用 Chrome / Edge 无头渲染

### 公众号

- 收割源：微信本地缓存（Favicons / Share Data / History 等 SQLite + 磁盘 `Cache_Data/f_*`）
- 导出成品：HTML + Markdown + `figures/` 图片，html 与微信阅读体验一致，md 保留核心内容
- 批量导出后生成 `export_meta.json`（导出时间、总数量、每篇标题 / 链接 / 发表时间）
- 原文链接为带 chksm 的正式链接，不含个人会话参数

### 前置条件

1. 这台电脑登录过微信，并且**在手机上打开过朋友圈**。朋友圈数据要微信自己从服务器同步下来。
2. 微信文件目录能被找到。默认自动检测常见位置；找不到可在 `config/config.json` 的 `wechat_data_root` 手动填写。
3. 想导出**原图**需要联网。程序会从微信 CDN 下载并解密；失败时自动回退缩略图，并在输出目录生成 `download_report.json` 记录失败原因。
4. 朋友圈数据保留在 `cache/<年-月>/Sns/Img/`，微信按月滚动缓存；更早内容依赖联网从 CDN 获取（年代太久远的内容，微信自己也可能已下架）。

---

## 🤝 贡献

欢迎提 Issue 和 PR！如果你有想法，欢迎来聊：

- 找到了 Mac 系统自动提取 db_key 的方案？
- 发现了新的微信版本兼容问题？
