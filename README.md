# Klaksvík ↔ Kalsoy 余票监控

按设定频率检查目标船班；当余票从 0 变为大于 0 时，通过 Server酱发送微信通知。

仓库可以公开：SendKey、出行日期、目标班次和含车辆参数的订票 URL 均通过
GitHub Actions 加密 Secrets 注入，源码和正常运行日志不会显示这些值。

## 配置 GitHub Actions Secrets

进入仓库的 `Settings` → `Secrets and variables` → `Actions`，创建以下
Repository secrets：

- `SERVERCHAN_SENDKEY`：Server酱 SendKey。
- `BOOKING_URL`：在 SSL 订票页选好路线、乘客与车辆参数后得到的完整 URL。
- `TARGET_DATE_ISO`：目标日期，格式为 `YYYY-MM-DD`。
- `OUTBOUND_TARGET_TIMES`：去程目标班次，多个时间以英文逗号分隔。
- `RETURN_TARGET_TIMES`：回程目标班次，多个时间以英文逗号分隔。

不要把真实值写进源码、README、Issue、提交信息或 Actions 日志。GitHub Secret
一旦保存便不会再次显示；需要修改时直接覆盖同名 Secret。

## 运行方式

工作流每 30 分钟触发一次，但脚本会在内部控制实际访问频率：

- 目标日前 8 至 4 天：每 2 小时检查一次。
- 目标日前 3 至 1 天：每小时检查一次。
- 目标日当天：每 30 分钟检查一次。
- 其他日期：只启动工作流并立即退出，不访问订票网站。

GitHub 定时任务可能因平台负载延迟。每个方向独立记录通知状态；持续有票时不会
重复推送，重新变为无票后再放票会再次通知。本程序只监控和通知，不会自动下单
或付款。

## 手动测试

打开仓库 `Actions` 页面，选择 `Monitor Kalsoy ferry tickets`，点击
`Run workflow`。日志只会显示任务是否跳过以及各方向是否检查完成，不会输出日期、
具体班次、余票数量、订票 URL 或 SendKey。缓存中的通知状态也使用密钥派生
的 HMAC 保护，不以明文保存班次或余票状态。

## 停止监控

成功订票后，在仓库 `Actions` 页面停用该工作流，或删除相关 Repository secrets。

## 本地运行

在当前终端会话中设置与上述同名的环境变量，然后运行：

```bash
python monitor.py
```

`.env`、本地状态文件和常见敏感配置已加入 `.gitignore`。
