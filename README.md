# Klaksvík → Kalsoy 余票监控

监控 2026 年 7 月 2 日以下目标船班：

- 去程 Klaksvík → Kalsoy：08:00、09:00
- 回程 Kalsoy → Klaksvík：15:10、16:30、17:35

按法罗群岛当地时间自动调整检查频率；当余票从 0 变为大于 0 时，通过
Server酱推送微信通知：

- 2026 年 6 月 29 日之前：每 2 小时
- 2026 年 6 月 29 日至 7 月 1 日：每 1 小时
- 2026 年 7 月 2 日：每 30 分钟
- 2026 年 7 月 2 日之后：停止访问订票网站

工作流使用分段 Cron，只在对应阶段需要检查时才启动。程序内部仍保留
2026 年日期校验，避免 GitHub 的年度 Cron 在以后年份再次访问订票网站。
GitHub 定时任务可能延迟几十分钟启动；只要任务已经被 GitHub 触发，脚本会继续检查，不再按实际启动分钟二次跳过。

## 1. 获取 Server酱 SendKey

1. 打开 <https://sct.ftqq.com/sendkey>。
2. 使用微信登录。
3. 按页面提示完成消息通道设置。
4. 复制页面显示的 `SendKey`，不要公开或写进代码。
5. 建议先在 Server酱页面发送一条测试消息，确认微信能收到。

## 2. 创建 GitHub 仓库

1. 登录 GitHub，点击右上角 `+` → `New repository`。
2. 仓库名可填 `kalsoy-ticket-monitor`。
3. 建议选择 `Private`，然后创建仓库。
4. 将本目录内的全部内容上传到仓库根目录。必须保留：
   - `monitor.py`
   - `.github/workflows/monitor.yml`

如果网页上传时看不到 `.github` 隐藏目录，可以在仓库内逐级创建：
`.github` → `workflows` → `monitor.yml`。

## 3. 保存 SendKey

在 GitHub 仓库中进入：

`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

- Name：`SERVERCHAN_SENDKEY`
- Secret：粘贴 Server酱的 SendKey

保存后，密钥不会显示在代码或运行日志里。

## 4. 手动测试

1. 打开仓库的 `Actions` 页面。
2. 左侧选择 `Monitor Kalsoy ferry tickets`。
3. 点击 `Run workflow` → `Run workflow`。
4. 等待运行结束，点进运行记录查看日志。

目前没有余票时，正常日志应类似：

```text
02. Jul. 2026 Klaksvík → Kalsoy: {'08:00': 0, '09:00': 0}
02. Jul. 2026 Kalsoy → Klaksvík: {'15:10': 0, '16:30': 0, '17:35': 0}
No target availability.
```

因为当前无票，首次测试不会发送微信。这是正常行为。

## 5. 测试微信推送是否配置正确

最简单且安全的方法是在 Server酱网站提供的测试功能中发送测试消息。
不要为了测试而修改监控程序中的余票判断。

## 行为说明

- 去程 08:00/09:00 或回程 15:10/16:30/17:35 任意目标班次有票，就会推送。
- 每个方向独立记录状态：去程已通知过，不会阻止回程第一次有票时通知。
- 持续有票期间只推送一次，避免重复刷屏。
- 如果某个方向的目标余票重新变为 0，之后再次放票会再次推送。
- GitHub 定时任务可能因平台负载延迟几分钟，不保证精确到秒。
- 本程序只监控和通知，不会自动下单或付款。

## 停止监控

进入 GitHub 仓库的 `Actions` 页面，选择对应工作流，点击右上角菜单并选择
`Disable workflow`。成功订票后建议立即停用。
